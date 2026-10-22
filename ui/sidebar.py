"""Global, self-service filters shown on every page."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from dq.engine import Filters
from dq.model import DIMENSION_ORDER, DIMENSIONS, SEVERITIES
from dq.settings import SETTINGS
from ui import data
from ui.components import md

PERIODS = {
    "Last 90 days": 90,
    "Last 12 months": 365,
    "Last 2 years": 730,
    "Last 5 years": 1826,
    "All time": None,
    "Custom range": "custom",
}


def render(show_technical: bool = True, kind_labels: dict[str, str] | None = None) -> Filters:
    """Draw the filter panel and return the record filters. Called once per run from the app
    entry point, before the page runs, so the same choices apply on every page."""
    kind = (lambda d: kind_labels.get(d, d)) if kind_labels else (lambda d: d)
    with st.sidebar:
        md(
            '<div style="display:flex;align-items:center;gap:10px;margin:4px 0 14px">'
            '<span class="dq-sys-badge" style="width:36px;height:36px;border-radius:12px;'
            'background:linear-gradient(145deg,#2a78d6,#6250d6)">DQ</span>'
            '<div><div style="font-weight:700;font-size:15px;letter-spacing:-.01em">CTP Data Quality</div>'
            '<div class="dq-muted" style="font-size:12px">eSUB · Rhapsody · PMS · Reporting</div></div></div>'
        )

        st.markdown("**Filters**")
        md('<div class="dq-muted" style="margin:-10px 0 10px">Applied on every page.</div>')
        period = st.selectbox(
            "Time period",
            list(PERIODS),
            index=2,
            key="f_period",
            help="Limits checks to records dated in this period. Each check names the date it uses. "
            "Records with no date are always included.",
        )
        today = date.today()
        if PERIODS[period] == "custom":
            picked = st.date_input(
                "From / to",
                value=(today - timedelta(days=365), today),
                key="f_range",
                format="MM/DD/YYYY",
            )
            start, end = (picked if isinstance(picked, (list, tuple)) and len(picked) == 2 else (None, None))
        elif PERIODS[period] is None:
            start, end = None, None
        else:
            start, end = today - timedelta(days=PERIODS[period]), today

        types = st.multiselect(
            "Submission type",
            data.filter_options("submission_type"),
            key="f_types",
            placeholder="All submission types",
            help="Limit results to these submission types. Data feed checks ignore this filter.",
        )
        offices = st.multiselect(
            "Organization",
            data.filter_options("office"),
            key="f_offices",
            placeholder="All organizations",
            help="The applicant company, as recorded in the Organization field in Rhapsody.",
        )

        st.markdown("**What to show**")
        st.pills(
            "Problem type",
            DIMENSION_ORDER,
            selection_mode="multi",
            default=DIMENSION_ORDER,
            key="f_dims",
            format_func=kind,
            help="\n".join(f"{kind(d)} — {v['question']} {v['explain']}" for d, v in DIMENSIONS.items()),
        )
        st.pills(
            "Impact",
            list(SEVERITIES),
            selection_mode="multi",
            default=list(SEVERITIES),
            key="f_sev",
            help="\n".join(f"{k}: {v['explain']}" for k, v in SEVERITIES.items()),
        )
        st.toggle(
            "Hide passing checks",
            key="f_only_problems",
            help="Show only checks that found at least one problem.",
        )
        if show_technical:
            st.toggle(
                "Show technical details",
                key="f_technical",
                help="For data engineers: shows the tables and SQL behind each check.",
            )

        st.divider()
        if SETTINGS.snapshot_mode:
            label, help_text = ("Reload results", "Load the latest scheduled refresh.")
        else:
            label, help_text = ("Re-run all checks", "Run every check again now.")
        if st.button(label, icon=":material/refresh:", width="stretch", help=help_text):
            data.refresh()
        mode = ("Demo data (not real CTP data)" if SETTINGS.demo_mode
                else "Saved results from the scheduled refresh" if SETTINGS.snapshot_mode
                else "Live from the SQL warehouse")
        who = data.viewer_email()
        md(
            f'<div class="dq-muted" style="margin-top:8px;line-height:1.6">Data: <b>{mode}</b><br>'
            f'Catalog: {SETTINGS.catalog}'
            + (f"<br>Signed in as {who}" if who else "")
            + "</div>"
        )

    return Filters(start=start, end=end, submission_types=tuple(types), offices=tuple(offices))


def view_prefs() -> dict:
    ss = st.session_state
    return {
        "dims": set(ss.get("f_dims") or DIMENSION_ORDER),
        "sev": set(ss.get("f_sev") or SEVERITIES),
        "only_problems": bool(ss.get("f_only_problems")),
        "technical": bool(ss.get("f_technical")),
    }
