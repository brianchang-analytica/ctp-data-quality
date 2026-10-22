"""End-to-end test of snapshot mode, in DuckDB on synthetic rows.

1. Fill the source tables with synthetic rows (same generator as smoke_duckdb.py).
2. Emulate jobs/refresh_snapshot.py: run each check's SQL, save grouped counts to
   dq_app_check_counts and only the problem records (details as JSON) to dq_app_check_rows.
3. Switch the engine to snapshot mode and run the app's queries against the saved rows.
4. Assert snapshot totals, trends and breakdowns equal live ones under the same filters, and
   that the records view expands back into the check's own detail columns.

Usage (from ctp_dq_app):  python tools/smoke_snapshot.py [--all]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
os.environ["DQ_SOURCE"] = "live"
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "tools"))

import pandas as pd  # noqa: E402
import sqlglot  # noqa: E402

import smoke_duckdb as sd  # noqa: E402
import validate_checks as vc  # noqa: E402
from dq import engine  # noqa: E402
from dq.model import CONTRACT_COLUMNS  # noqa: E402
from dq.settings import SETTINGS  # noqa: E402


def duck(sql: str) -> str:
    return sqlglot.transpile(sql, read="databricks", write="duckdb")[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every check, not just the MVP core set")
    args = ap.parse_args()
    if args.all:
        from dq.checks import ALL_CHECKS
        checks = list(ALL_CHECKS)
    else:
        from dq.checks.core import active_checks
        checks = active_checks()

    tables = vc.load_inventory()
    con = vc.make_duck(tables)
    plain = vc.plain_placeholders()
    needed = set()
    for c in checks:
        tree = sqlglot.parse_one(c.render_sql(plain), read="databricks")
        needed |= {(t.db.lower(), t.name.lower()) for t in vc.referenced_tables(tree) if (t.db.lower(), t.name.lower()) in tables}
    sd.populate(con, tables, needed, 250)

    rows_t = f"{SETTINGS.catalog}.{SETTINGS.results_schema}.{SETTINGS.rows_table_name}"
    counts_t = f"{SETTINGS.catalog}.{SETTINGS.results_schema}.{SETTINGS.counts_table_name}"
    runs_t = f"{SETTINGS.catalog}.{SETTINGS.results_schema}.{SETTINGS.runs_table_name}"
    con.execute(f"""CREATE TABLE {rows_t} (check_id VARCHAR, run_id VARCHAR, refreshed_at TIMESTAMP,
        record_key VARCHAR, record_date DATE, submission_type VARCHAR, office VARCHAR, is_issue BOOLEAN,
        issue_category VARCHAR, issue_reason VARCHAR, details VARCHAR)""")
    con.execute(f"""CREATE TABLE {counts_t} (check_id VARCHAR, run_id VARCHAR, refreshed_at TIMESTAMP,
        record_date DATE, submission_type VARCHAR, office VARCHAR, issue_category VARCHAR,
        checked BIGINT, failed BIGINT)""")
    con.execute(f"""CREATE TABLE {runs_t} (run_id VARCHAR, check_id VARCHAR, check_title VARCHAR,
        started_at TIMESTAMP, finished_at TIMESTAMP, status VARCHAR, records_checked BIGINT, issues BIGINT,
        detail_columns VARCHAR, error VARCHAR)""")

    # 2. emulate jobs/refresh_snapshot.py: grouped counts + problem records only
    now = datetime(2026, 10, 21, 5, 0)
    expected_details: dict[str, list[str]] = {}
    for c in checks:
        df = con.execute(duck(c.render_sql(plain))).df()
        details = list(df.columns[len(CONTRACT_COLUMNS):])
        expected_details[c.id] = details
        out = pd.DataFrame({
            "check_id": c.id, "run_id": "r1", "refreshed_at": now,
            **{col: df[col] for col in CONTRACT_COLUMNS},
            "details": [json.dumps({k: (None if pd.isna(v) else str(v)) for k, v in zip(details, row)})
                        for row in df[details].itertuples(index=False)] if details else "{}",
        })
        out["is_issue"] = out["is_issue"].fillna(False).astype(bool)
        con.register("staged", out)
        con.execute(f"""INSERT INTO {counts_t}
            SELECT check_id, run_id, refreshed_at, record_date, submission_type, office, issue_category,
                   COUNT(*), SUM(CAST(is_issue AS BIGINT))
            FROM staged GROUP BY ALL""")
        con.execute(f"INSERT INTO {rows_t} SELECT * FROM staged WHERE is_issue")
        con.unregister("staged")
        con.execute(f"INSERT INTO {runs_t} VALUES ('r1', ?, ?, ?, ?, 'ok', ?, ?, ?, NULL)",
                    [c.id, c.title, now, now, len(df), int(out['is_issue'].sum()), json.dumps(details)])

    f = engine.Filters(start=date(2023, 6, 1), end=date(2025, 12, 31), submission_types=("PMTA", engine.NOT_RECORDED))
    # live counts (engine in live mode, unquoted names for DuckDB)
    object.__setattr__(SETTINGS, "placeholders", lambda: plain)
    def views(c):
        return [("trend", engine.trend_query(c, f))] + [(f"by_{b}", engine.breakdown_query(c, f, b)) for b in engine.BREAKDOWNS]

    def frame(qq):
        res = con.execute(duck(qq.sql), qq.params or None).df()
        res = res.astype({"checked": "int64", "failed": "int64"})
        return res.sort_values(list(res.columns)).reset_index(drop=True)

    live, live_views = {}, {}
    for c in checks:
        q = engine.summary_query(c, f)
        live[c.id] = tuple(con.execute(duck(q.sql), q.params or None).fetchone())
        live_views[c.id] = {name: frame(qq) for name, qq in views(c)}

    # 3. snapshot mode
    object.__setattr__(SETTINGS, "source", "snapshot")
    object.__setattr__(SETTINGS, "warehouse_id", "test")
    assert SETTINGS.snapshot_mode
    problems = 0
    q = engine.snapshot_summary_query(checks, f)
    snap = {r[0]: (r[1], r[2]) for r in con.execute(duck(q.sql.replace("`", "")), q.params or None).fetchall()}
    status = con.execute(duck(engine.snapshot_status_query(checks).sql.replace("`", ""))).fetchall()
    assert len(status) == len(checks), "status query should return one row per check"
    for c in checks:
        lv = live[c.id]
        sv = snap.get(c.id, (0, 0))
        if tuple(int(x or 0) for x in lv) != tuple(int(x or 0) for x in sv):
            problems += 1
            print(f"MISMATCH [{c.id}] live={lv} snapshot={sv}")
        for name, qq in views(c):
            try:
                got = frame(qq)
            except Exception as e:
                problems += 1
                print(f"FAIL [{c.id}] snapshot {name}: {str(e).splitlines()[0][:200]}")
                continue
            if not got.equals(live_views[c.id][name]):
                problems += 1
                print(f"MISMATCH [{c.id}] {name}: snapshot differs from live")
        qq = engine.detail_query(c, f, 50)
        try:
            res = con.execute(duck(qq.sql), qq.params or None).df()
        except Exception as e:
            problems += 1
            print(f"FAIL [{c.id}] snapshot detail: {str(e).splitlines()[0][:200]}")
            continue
        cols = list(engine.expand_details(res).columns)
        want = [x for x in CONTRACT_COLUMNS if x != "is_issue"] + expected_details[c.id]
        if res.shape[0] and cols != want:
            problems += 1
            print(f"COLUMNS [{c.id}] expanded {cols} != {want}")
    print(f"\n{len(checks)} checks: snapshot totals, trends and breakdowns {'match' if not problems else 'DO NOT match'} live; {problems} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
