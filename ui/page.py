"""Helpers every page uses: load results once, show the demo notice, scope by the sidebar."""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from dq.checks import ALL_CHECKS
from dq.engine import Filters, Summary
from dq.model import Check
from dq.settings import SETTINGS
from ui import data, sidebar
from ui.components import banner, esc, md


def filters() -> Filters:
    return st.session_state["filters"]


def load() -> tuple[dict[str, Summary], datetime]:
    f = filters()
    with st.spinner(f"Running {len(ALL_CHECKS)} checks across eSUB, Rhapsody, PMS and Reporting…"):
        sums, ran_at = data.summaries(f)
    if SETTINGS.demo_mode:
        banner(
            "<b>You are looking at demo data.</b> The numbers and records below are made up so the layout can be "
            "reviewed. Connect the app to a SQL warehouse (set <code>DATABRICKS_WAREHOUSE_ID</code>) to see real results."
        )
    errors = [cid for cid, s in sums.items() if s.error]
    if errors and len(errors) == len(sums):
        st.error(
            "None of the checks could run. The app may not have access to the SQL warehouse or the CTP catalog. "
            "Open any check to see the technical message, and share it with the data team.",
            icon=":material/cloud_off:",
        )
    return sums, ran_at


def scoped(checks: list[Check] | None = None) -> list[Check]:
    """Checks matching the sidebar's 'kinds of problems' and 'impact' choices."""
    prefs = sidebar.view_prefs()
    return [c for c in (ALL_CHECKS if checks is None else checks) if c.dimension in prefs["dims"] and c.severity in prefs["sev"]]


def footer(ran_at: datetime) -> None:
    md(
        f'<div class="dq-muted" style="margin-top:28px;text-align:center">Results from {esc(ran_at.strftime("%b %d, %Y %I:%M %p"))} · '
        f"kept for {SETTINGS.cache_minutes} minutes · use “Re-run all checks” in the sidebar for fresh results</div>"
    )
