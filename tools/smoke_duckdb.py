"""Execute every check end-to-end in DuckDB against small synthetic tables.

This complements validate_checks.py (which only binds queries): it fills each
referenced table with random rows of the right types — with keys deliberately
overlapping between systems — and runs the app's summary, records, trend and
breakdown queries for every check, plus the trace queries. It catches runtime
failures (bad casts, division by zero, wrong aggregate nesting) and
contract violations (NULL is_issue, duplicate record_keys from join fan-out).

DuckDB is not Databricks, so a failure here is a strong hint rather than proof.

Usage (from ctp_dq_app):  python tools/smoke_duckdb.py [--module esub] [--rows 300]
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta
from importlib import import_module
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
os.environ["DQ_SOURCE"] = "live"  # validate the checks' own SQL, not the saved-results reader
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "tools"))

import duckdb  # noqa: E402
import sqlglot  # noqa: E402

import validate_checks as vc  # noqa: E402
from dq import engine  # noqa: E402
from dq.settings import SETTINGS  # noqa: E402

STNS = [f"{p}{n:07d}" for p in ("PM", "SE", "EX") for n in range(1, 101)]
KEYS = list(range(1, 301))
WORDS = ["Acme Tobacco Co.", "ACME TOBACCO CO", "Blue Ridge Vapor LLC", "Closed", "Open", "In Review", "Y", "N", "0", "1", "", None]


def fake(col: str, typ: str, r: random.Random):
    c = col.lower()
    if r.random() < 0.08:
        return None
    if typ == "BOOLEAN":
        return r.random() < 0.3
    if typ == "BYTE":
        return r.randint(0, 5)
    if typ in ("LONG", "INT", "SHORT", "DECIMAL"):
        return r.choice(KEYS)
    if typ == "DATE":
        return date(2023, 1, 1) + timedelta(days=r.randint(-30, 1200))
    if typ == "TIMESTAMP":
        return datetime(2023, 1, 1) + timedelta(hours=r.randint(-500, 30000))
    if typ == "BINARY":
        return None
    # STRING
    if "stn" in c or "tracking" in c:
        return r.choice(STNS) + (" " if r.random() < 0.05 else "")
    if c.endswith("_id") or c.endswith("id") or c.endswith("_key") or "uuid" in c:
        return str(r.choice(KEYS))
    if "date" in c or c.endswith("_ts") or "time" in c:
        return (datetime(2023, 1, 1) + timedelta(hours=r.randint(0, 30000))).strftime("%Y-%m-%d %H:%M:%S")
    return r.choice(WORDS)


def _unique(col: str, typ: str, i: int):
    """Sequential value for a table's first column (usually its natural key), so keys are unique per table."""
    if typ == "BYTE":
        return i % 100
    if typ in ("LONG", "INT", "SHORT", "DECIMAL"):
        return KEYS[i % len(KEYS)]
    if "stn" in col.lower() or "tracking" in col.lower():
        return STNS[i % len(STNS)]
    return str(KEYS[i % len(KEYS)])


def populate(con, tables, needed, n_rows: int) -> None:
    r = random.Random(7)
    for (schema, table) in needed:
        cols = tables[(schema, table)]
        names = list(cols)
        rows = []
        for i in range(n_rows):
            row = [fake(c, cols[c], r) for c in names]
            row[0] = _unique(names[0], cols[names[0]], i)
            rows.append(tuple(row))
        placeholders = ", ".join("?" for _ in names)
        quoted = ", ".join(f'"{c}"' for c in names)
        con.executemany(f"INSERT INTO {SETTINGS.catalog}.{schema}.{table} ({quoted}) VALUES ({placeholders})", rows)


def to_duck(sql: str) -> str:
    return sqlglot.transpile(sql, read="databricks", write="duckdb")[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module")
    ap.add_argument("--rows", type=int, default=250)
    args = ap.parse_args()

    if args.module:
        checks = list(import_module(f"dq.checks.{args.module}").CHECKS)
    else:
        from dq.checks import ALL_CHECKS
        checks = list(ALL_CHECKS)

    tables = vc.load_inventory()
    con = vc.make_duck(tables)
    plain = vc.plain_placeholders()
    needed = set()
    for c in checks:
        tree = sqlglot.parse_one(c.render_sql(plain), read="databricks")
        needed |= {(t.db.lower(), t.name.lower()) for t in vc.referenced_tables(tree) if (t.db.lower(), t.name.lower()) in tables}
    try:
        from dq.lookup import TRACE_QUERIES
    except Exception:
        TRACE_QUERIES = []
    for q in TRACE_QUERIES:
        tree = sqlglot.parse_one(q.render_sql(plain).replace(":stn", "'X'"), read="databricks")
        needed |= {(t.db.lower(), t.name.lower()) for t in vc.referenced_tables(tree) if (t.db.lower(), t.name.lower()) in tables}
    populate(con, tables, needed, args.rows)

    f = engine.Filters(start=date(2023, 6, 1), end=date(2025, 12, 31), submission_types=("PMTA",), offices=("OS",))
    problems = 0
    orig = SETTINGS.placeholders
    object.__setattr__(SETTINGS, "placeholders", lambda: plain)  # unquoted names for DuckDB
    try:
        for c in checks:
            base = c.render_sql(plain)
            try:
                rows = con.execute(f"SELECT record_key, is_issue FROM ({to_duck(base)}) AS b").fetchall()
                nulls = sum(1 for _, i in rows if i is None)
                keys = [k for k, _ in rows]
                dupes = len(keys) - len(set(keys))
                issues = sum(1 for _, i in rows if i)
                note = []
                if nulls:
                    note.append(f"{nulls} NULL is_issue")
                if any(k is None for k in keys):
                    note.append("NULL record_key")
                if dupes:
                    note.append(f"{dupes} duplicate record_key rows (fan-out?)")
                status = "WARN " if note else "ok   "
                problems += bool(note)
                print(f"{status}[{c.id}] rows={len(rows)} issues={issues} {'; '.join(note)}")
            except Exception as e:
                problems += 1
                print(f"FAIL [{c.id}] base query: {str(e).splitlines()[0][:300]}")
                continue
            for name, q in [("summary", engine.summary_query(c, f)), ("detail", engine.detail_query(c, f, 50)),
                            ("trend", engine.trend_query(c, f))] + [(f"by_{b}", engine.breakdown_query(c, f, b)) for b in engine.BREAKDOWNS]:
                try:
                    con.execute(to_duck(q.sql), q.params or None).fetchall()
                except Exception as e:
                    problems += 1
                    print(f"FAIL [{c.id}] {name}: {str(e).splitlines()[0][:300]}")
        for q in TRACE_QUERIES:
            try:
                con.execute(to_duck(q.render_sql(plain)), {"stn": STNS[0]}).fetchall()
                print(f"ok   [trace {q.id}]")
            except Exception as e:
                problems += 1
                print(f"FAIL [trace {q.id}]: {str(e).splitlines()[0][:300]}")
    finally:
        object.__setattr__(SETTINGS, "placeholders", orig)
    print(f"\n{len(checks)} checks, {len(TRACE_QUERIES)} trace queries executed; {problems} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
