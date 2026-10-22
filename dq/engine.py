"""Turns a Check's base query into the summary / detail / trend / breakdown
queries the app needs, and runs them (or their demo stand-ins).

Every check's base query returns one row per evaluated record with the
contract columns (see model.CONTRACT_COLUMNS), so a single set of wrappers
works for all of them.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from dq import db, demo
from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check, status_for
from dq.settings import SETTINGS


# Filter choice that matches records whose submission type / organization is blank.
NOT_RECORDED = "(not recorded)"


@dataclass(frozen=True)
class Filters:
    start: date | None = None
    end: date | None = None
    submission_types: tuple[str, ...] = ()
    offices: tuple[str, ...] = ()

    def applied_to(self, check: Check) -> list[str]:
        """Human-readable list of the filters that actually narrow this check."""
        out = []
        if FILTER_DATE in check.filters and (self.start or self.end):
            out.append(check.date_label)
        if FILTER_SUBMISSION_TYPE in check.filters and self.submission_types:
            out.append("Submission type")
        if FILTER_OFFICE in check.filters and self.offices:
            out.append("Organization")
        return out


@dataclass
class Summary:
    check_id: str
    checked: int = 0
    failed: int = 0
    error: str | None = None
    seconds: float = 0.0
    as_of: datetime | None = None  # snapshot mode: when this check's data was last refreshed
    note: str | None = None  # e.g. "the latest refresh failed; showing the previous one"

    @property
    def pass_rate(self) -> float | None:
        if self.error or not self.checked:
            return None
        return 1 - self.failed / self.checked

    def status(self, check: Check) -> str:
        return "error" if self.error else status_for(self.pass_rate, check)


@dataclass
class _Query:
    sql: str
    params: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Query builders
# --------------------------------------------------------------------------- #


def _where(check: Check, f: Filters, params: dict) -> str:
    clauses: list[str] = []
    if FILTER_DATE in check.filters:
        if f.start:
            params["start_date"] = f.start
            clauses.append("record_date >= :start_date")
        if f.end:
            params["end_date"] = f.end
            clauses.append("record_date <= :end_date")
        if clauses:
            # A record with no date at all is itself often the problem; never hide it.
            clauses = [f"(record_date IS NULL OR ({' AND '.join(clauses)}))"]
    for flt, values, prefix, col in (
        (FILTER_SUBMISSION_TYPE, f.submission_types, "st", "submission_type"),
        (FILTER_OFFICE, f.offices, "of", "office"),
    ):
        if flt in check.filters and values:
            names = []
            for i, v in enumerate(v for v in values if v != NOT_RECORDED):
                params[f"{prefix}_{i}"] = v
                names.append(f":{prefix}_{i}")
            parts = [f"{col} IN ({', '.join(names)})"] if names else []
            if NOT_RECORDED in values:
                parts.append(f"nullif(trim({col}), '') IS NULL")
            clauses.append("(" + " OR ".join(parts) + ")")
    return " AND ".join(clauses) if clauses else "TRUE"


SNAPSHOT_COLUMNS = (
    "record_key, record_date, submission_type, office, is_issue, issue_category, issue_reason, details"
)


def _base(check: Check) -> str:
    """Record-level rows: the check's own SQL (live) or its saved problem records (snapshot)."""
    if SETTINGS.snapshot_mode:
        # check.id is validated as UPPER_SNAKE_CASE in Check.__post_init__, so it is safe to inline.
        return f"SELECT {SNAPSHOT_COLUMNS} FROM {SETTINGS.rows_table} WHERE check_id = '{check.id}'"
    return check.render_sql(SETTINGS.placeholders())


def _measured(check: Check) -> tuple[str, str, str]:
    """(source, checked/failed measures, 'is a problem' predicate) for the count queries.

    Live mode counts the check's own rows. Snapshot mode sums the saved counts, which the refresh
    job groups by the same columns the filters use, so both give identical numbers."""
    if SETTINGS.snapshot_mode:
        src = (f"SELECT record_date, submission_type, office, issue_category, checked, failed "
               f"FROM {SETTINGS.counts_table} WHERE check_id = '{check.id}'")
        return src, "SUM(checked) AS checked, SUM(failed) AS failed", "failed > 0"
    return _base(check), "COUNT(*) AS checked, COUNT_IF(is_issue) AS failed", "is_issue"


def expand_details(df: pd.DataFrame) -> pd.DataFrame:
    """Snapshot rows keep a check's detail columns as one JSON object; turn it back into columns."""
    if "details" not in df.columns:
        return df
    parsed = [json.loads(d) if isinstance(d, str) and d else {} for d in df["details"]]
    cols: list[str] = []
    for p in parsed:
        cols += [k for k in p if k not in cols]
    extra = pd.DataFrame([[p.get(k) for k in cols] for p in parsed], columns=cols, index=df.index)
    return pd.concat([df.drop(columns=["details"]), extra], axis=1)


def summary_query(check: Check, f: Filters) -> _Query:
    params: dict = {}
    where = _where(check, f, params)
    src, measures, _ = _measured(check)
    sql = (
        f"SELECT {measures}\n"
        f"FROM (\n{src}\n) AS base\nWHERE {where}"
    )
    return _Query(sql, params)


def detail_query(check: Check, f: Filters, limit: int | None = None) -> _Query:
    params: dict = {}
    where = _where(check, f, params)
    limit = int(limit or SETTINGS.detail_row_limit)
    sql = (
        "SELECT * EXCEPT (is_issue)\n"
        f"FROM (\n{_base(check)}\n) AS base\n"
        f"WHERE is_issue AND {where}\n"
        f"ORDER BY record_date DESC NULLS LAST, record_key\nLIMIT {limit}"
    )
    return _Query(sql, params)


def trend_query(check: Check, f: Filters, grain: str = "MONTH") -> _Query:
    if grain not in {"WEEK", "MONTH", "QUARTER", "YEAR"}:
        raise ValueError(grain)
    params: dict = {}
    where = _where(check, f, params)
    src, measures, _ = _measured(check)
    sql = (
        f"SELECT CAST(date_trunc('{grain}', record_date) AS DATE) AS period,\n"
        f"       {measures}\n"
        f"FROM (\n{src}\n) AS base\n"
        f"WHERE record_date IS NOT NULL AND {where}\n"
        "GROUP BY 1 ORDER BY 1"
    )
    return _Query(sql, params)


BREAKDOWNS = {
    "issue_category": "Type of problem",
    "submission_type": "Submission type",
    "office": "Organization",
}


def breakdown_query(check: Check, f: Filters, by: str) -> _Query:
    if by not in BREAKDOWNS:
        raise ValueError(by)
    params: dict = {}
    where = _where(check, f, params)
    src, measures, is_problem = _measured(check)
    sql = (
        f"SELECT COALESCE(CAST({by} AS STRING), '(not recorded)') AS bucket,\n"
        f"       {measures}\n"
        f"FROM (\n{src}\n) AS base\n"
        f"WHERE {where}{' AND ' + is_problem if by == 'issue_category' else ''}\n"
        "GROUP BY 1 ORDER BY failed DESC, checked DESC LIMIT 25"
    )
    return _Query(sql, params)


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #

_POOL = ThreadPoolExecutor(max_workers=max(1, SETTINGS.max_parallel_queries), thread_name_prefix="dq")


def run_summary(check: Check, f: Filters, user_token: str | None = None) -> Summary:
    started = time.perf_counter()
    try:
        if SETTINGS.demo_mode:
            checked, failed = demo.summary(check, f)
        else:
            q = summary_query(check, f)
            row = db.query(q.sql, q.params, user_token).iloc[0]
            checked, failed = int(row["checked"] or 0), int(row["failed"] or 0)
        return Summary(check.id, checked, failed, seconds=time.perf_counter() - started)
    except Exception as exc:  # surfaced to the steward as "Could not run"
        return Summary(check.id, error=db.friendly_error(exc), seconds=time.perf_counter() - started)


def run_summaries(checks: list[Check], f: Filters, user_token: str | None = None) -> dict[str, Summary]:
    if SETTINGS.snapshot_mode:
        return _snapshot_summaries(checks, f, user_token)
    futures = {c.id: _POOL.submit(run_summary, c, f, user_token) for c in checks}
    return {cid: fut.result() for cid, fut in futures.items()}


def snapshot_summary_query(checks: list[Check], f: Filters) -> _Query:
    """One grouped query over the saved counts, each check with its own filters."""
    params: dict = {}
    branches = [f"(check_id = '{c.id}' AND {_where(c, f, params)})" for c in checks]
    sql = (
        "SELECT check_id, SUM(checked) AS checked, SUM(failed) AS failed\n"
        f"FROM {SETTINGS.counts_table}\nWHERE " + "\n   OR ".join(branches) + "\nGROUP BY check_id"
    )
    return _Query(sql, params)


def snapshot_status_query(checks: list[Check]) -> _Query:
    ids = ", ".join(f"'{c.id}'" for c in checks)
    sql = (
        "SELECT check_id,\n"
        "       max(CASE WHEN status = 'ok' THEN finished_at END) AS last_ok,\n"
        "       max_by(status, finished_at) AS last_status,\n"
        "       max_by(error, finished_at) AS last_error\n"
        f"FROM {SETTINGS.runs_table}\nWHERE check_id IN ({ids})\nGROUP BY check_id"
    )
    return _Query(sql)


def _snapshot_summaries(checks: list[Check], f: Filters, user_token: str | None) -> dict[str, Summary]:
    started = time.perf_counter()
    try:
        status_q = snapshot_status_query(checks)
        counts_q = snapshot_summary_query(checks, f)
        status_f = _POOL.submit(db.query, status_q.sql, None, user_token)
        counts = db.query(counts_q.sql, counts_q.params, user_token)
        status = status_f.result()
    except Exception as exc:
        msg = db.friendly_error(exc)
        if "not found" in msg.lower():
            msg = ("The saved results have not been created yet. Run the “CTP data quality refresh” job once. "
                   "See README, Connecting to the data.\n\n" + msg)
        return {c.id: Summary(c.id, error=msg) for c in checks}
    took = time.perf_counter() - started
    by_count = {r.check_id: r for r in counts.itertuples()}
    by_status = {r.check_id: r for r in status.itertuples()}
    out: dict[str, Summary] = {}
    for c in checks:
        st_row = by_status.get(c.id)
        last_ok = getattr(st_row, "last_ok", None) if st_row is not None else None
        if last_ok is None or pd.isna(last_ok):
            err = getattr(st_row, "last_error", None) if st_row is not None else None
            out[c.id] = Summary(c.id, error="This check has not been refreshed successfully yet."
                                + (f"\n\nLast error: {err}" if err else ""), seconds=took)
            continue
        cnt = by_count.get(c.id)
        note = None
        if getattr(st_row, "last_status", "ok") != "ok":
            note = "The last refresh failed. Showing results from the previous refresh."
        out[c.id] = Summary(c.id, int(cnt.checked) if cnt is not None else 0, int(cnt.failed) if cnt is not None else 0,
                            seconds=took, as_of=pd.Timestamp(last_ok).to_pydatetime(), note=note)
    return out


def run_detail(check: Check, f: Filters, user_token: str | None = None) -> pd.DataFrame:
    if SETTINGS.demo_mode:
        return demo.detail(check, f)
    q = detail_query(check, f)
    df = db.query(q.sql, q.params, user_token)
    return expand_details(df) if SETTINGS.snapshot_mode else df


def run_trend(check: Check, f: Filters, user_token: str | None = None, grain: str = "MONTH") -> pd.DataFrame:
    if SETTINGS.demo_mode:
        return demo.trend(check, f, grain)
    q = trend_query(check, f, grain)
    return db.query(q.sql, q.params, user_token)


def run_breakdown(check: Check, f: Filters, by: str, user_token: str | None = None) -> pd.DataFrame:
    if SETTINGS.demo_mode:
        return demo.breakdown(check, f, by)
    q = breakdown_query(check, f, by)
    return db.query(q.sql, q.params, user_token)
