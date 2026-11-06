"""Cached data access for the pages, plus health-score math.

Results are cached per (filters, viewer) so stewards switching pages do not
re-run queries. In on-behalf-of-user mode the viewer's identity is part of the
cache key, because two stewards may be allowed to see different data.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime

import pandas as pd
import streamlit as st

from dq import db, engine, rules
from dq.checks import ALL_CHECKS, CHECKS_BY_ID
from dq.checks.core import active_checks
from dq.engine import Filters, Summary
from dq.model import SEVERITIES, Check, status_for
from dq.settings import SETTINGS

_TTL = SETTINGS.cache_minutes * 60


# --------------------------------------------------------------------------- #
# Viewer identity
# --------------------------------------------------------------------------- #


def _headers() -> dict:
    try:
        return dict(st.context.headers)
    except Exception:
        return {}


def user_token() -> str | None:
    if SETTINGS.auth_mode != "user":
        return None
    h = {k.lower(): v for k, v in _headers().items()}
    return h.get("x-forwarded-access-token")


def viewer_email() -> str | None:
    h = {k.lower(): v for k, v in _headers().items()}
    return h.get("x-forwarded-email") or h.get("x-forwarded-preferred-username")


def identity() -> str:
    tok = user_token()
    if not tok:
        return "app"
    return viewer_email() or hashlib.sha256(tok.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Rules: wording and limits from the workspace rule file, when DQ_RULES_PATH is set
# --------------------------------------------------------------------------- #

RULES_TTL_MINUTES = 10


@st.cache_data(ttl=RULES_TTL_MINUTES * 60, show_spinner=False)
def _rule_source(path: str, nonce: int) -> rules.RuleSource:
    return rules.load(path)


def rule_source() -> rules.RuleSource:
    """Read again every few minutes, and on "Re-run all checks", so workspace edits show without a redeploy."""
    return _rule_source(SETTINGS.rules_path, st.session_state.get("refresh_nonce", 0))


def checks_in_app() -> list[Check]:
    """The checks the app shows, with any wording and limits from the rule file."""
    source = rule_source()
    return [rules.apply(c, source) for c in active_checks()]


def check_by_id(check_id: str) -> Check:
    return rules.apply(CHECKS_BY_ID[check_id], rule_source())


# --------------------------------------------------------------------------- #
# Cached queries
# --------------------------------------------------------------------------- #


@st.cache_data(ttl=_TTL, show_spinner=False)
def _summaries(filters: Filters, check_ids: tuple[str, ...], ident: str, _token: str | None, nonce: int) -> dict:
    results = engine.run_summaries([CHECKS_BY_ID[i] for i in check_ids], filters, _token)
    return {"ran_at": datetime.now(), "rows": {cid: asdict(s) for cid, s in results.items()}}


def summaries(filters: Filters, checks: list[Check] | None = None) -> tuple[dict[str, Summary], datetime]:
    """Run (or reuse) the summary of each check. Defaults to every check in the catalog."""
    ids = tuple(c.id for c in (ALL_CHECKS if checks is None else checks))
    payload = _summaries(filters, ids, identity(), user_token(), st.session_state.get("refresh_nonce", 0))
    return {cid: Summary(**row) for cid, row in payload["rows"].items()}, payload["ran_at"]


@st.cache_data(ttl=_TTL, show_spinner=False)
def _detail(check_id: str, filters: Filters, ident: str, _token: str | None, nonce: int) -> pd.DataFrame:
    return engine.run_detail(CHECKS_BY_ID[check_id], filters, _token)


def detail(check: Check, filters: Filters) -> pd.DataFrame:
    return _detail(check.id, filters, identity(), user_token(), st.session_state.get("refresh_nonce", 0))


@st.cache_data(ttl=_TTL, show_spinner=False)
def _trend(check_id: str, filters: Filters, grain: str, ident: str, _token: str | None, nonce: int) -> pd.DataFrame:
    return engine.run_trend(CHECKS_BY_ID[check_id], filters, _token, grain)


def trend(check: Check, filters: Filters, grain: str = "MONTH") -> pd.DataFrame:
    return _trend(check.id, filters, grain, identity(), user_token(), st.session_state.get("refresh_nonce", 0))


@st.cache_data(ttl=_TTL, show_spinner=False)
def _breakdown(check_id: str, filters: Filters, by: str, ident: str, _token: str | None, nonce: int) -> pd.DataFrame:
    return engine.run_breakdown(CHECKS_BY_ID[check_id], filters, by, _token)


def breakdown(check: Check, filters: Filters, by: str) -> pd.DataFrame:
    return _breakdown(check.id, filters, by, identity(), user_token(), st.session_state.get("refresh_nonce", 0))


_OPTIONS_SQL = {
    "submission_type": """
        SELECT DISTINCT trim(stdz_submission_type) AS v FROM ${core}.rhapsody_submissions
        WHERE coalesce(halo_is_deleted, false) = false AND nullif(trim(stdz_submission_type), '') IS NOT NULL
        ORDER BY 1 LIMIT 200""",
    "office": """
        SELECT DISTINCT trim(organization) AS v FROM ${core}.rhapsody_submissions
        WHERE coalesce(halo_is_deleted, false) = false AND nullif(trim(organization), '') IS NOT NULL
        ORDER BY 1 LIMIT 200""",
}
_DEMO_OPTIONS = {
    "submission_type": ["EX REQ", "General Correspondence", "Meeting Request", "MRTPA", "PMTA", "SE", "TPMF"],
    "office": ["Acme Tobacco Co.", "Atlas E-Liquids", "Blue Ridge Vapor LLC", "Harbor Leaf Ltd.", "Northfield Cigar Inc."],
}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _filter_options(kind: str, ident: str, _token: str | None) -> list[str]:
    if SETTINGS.demo_mode:
        return _DEMO_OPTIONS[kind]
    sql = _OPTIONS_SQL[kind]
    for name, value in SETTINGS.placeholders().items():
        sql = sql.replace("${" + name + "}", value)
    try:
        return [str(v) for v in db.query(sql, None, _token)["v"].tolist()]
    except Exception:
        return []


def filter_options(kind: str) -> list[str]:
    return _filter_options(kind, identity(), user_token()) + [engine.NOT_RECORDED]


def refresh() -> None:
    st.session_state["refresh_nonce"] = st.session_state.get("refresh_nonce", 0) + 1


# --------------------------------------------------------------------------- #
# Scores
# --------------------------------------------------------------------------- #


def weighted_score(checks: list[Check], sums: dict[str, Summary]) -> float | None:
    """Severity-weighted average pass rate over checks that returned data."""
    num = den = 0.0
    for c in checks:
        s = sums.get(c.id)
        if s is None or s.pass_rate is None:
            continue
        w = SEVERITIES[c.severity]["weight"]
        num += w * s.pass_rate
        den += w
    return num / den if den else None


def score_status(score: float | None) -> str:
    """Status for an aggregate score, using the default thresholds."""
    if score is None:
        return "none"
    if score >= 0.98:
        return "pass"
    if score >= 0.90:
        return "warn"
    return "fail"


def priority(check: Check, s: Summary) -> float:
    """Rank for 'fix first': failing high-impact checks with many records float to the top."""
    if s.error:
        return 0.5
    if not s.failed:
        return 0.0
    tier = {"fail": 3, "warn": 2, "pass": 1}.get(status_for(s.pass_rate, check), 0)
    return tier * 10 + SEVERITIES[check.severity]["weight"] * 2 + min(5.0, (s.failed or 0) ** 0.25)
