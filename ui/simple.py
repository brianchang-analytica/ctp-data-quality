"""MVP building blocks: compact check rows, area scores, and a focused check dialog.

Every page reads the same global filters (drawn in app.py): the time period, submission
type and organization narrow the records each check looks at; "kinds of problems", "impact"
and "only checks that found problems" decide which checks appear anywhere.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from dq.checks.core import KIND_WORDS
from dq.engine import Filters, Summary
from dq.model import (
    DIMENSION_ORDER,
    FILTER_OFFICE,
    FILTER_SUBMISSION_TYPE,
    STATUS_LABEL,
    Check,
)
from dq.settings import SETTINGS
from ui import data, sidebar
from ui.checks_view import _breakdown_tab, _trend_tab, excel_bytes, sort_by_priority, steward_frame
from ui.components import (
    banner,
    esc,
    fmt_int,
    fmt_pct,
    glass,
    md,
    meter_row,
    plain_chip,
    ring,
    status_icon,
    status_pill,
    systems_chips,
)


def filters() -> Filters:
    return st.session_state["filters"]


# --------------------------------------------------------------------------- #
# Results, scoped by the global filters
# --------------------------------------------------------------------------- #


def load() -> tuple[list[Check], dict[str, Summary], datetime]:
    """Run the active checks under the current filters and return those the filters let through."""
    all_checks = data.checks_in_app()
    with st.spinner("Running the checks…"):
        sums, ran_at = data.summaries(filters(), all_checks)
    if SETTINGS.demo_mode:
        banner("<b>Demo data.</b> Numbers are samples for layout review.")
    if sums and all(s.error for s in sums.values()):
        st.error("None of the checks could run. The app may not have access to the SQL warehouse or the CTP catalog.",
                 icon=":material/cloud_off:")
    return visible(all_checks, sums), sums, ran_at


def visible(checks: list[Check], sums: dict[str, Summary]) -> list[Check]:
    prefs = sidebar.view_prefs()
    out = []
    for c in checks:
        if c.dimension not in prefs["dims"] or c.severity not in prefs["sev"]:
            continue
        s = sums.get(c.id)
        if prefs["only_problems"] and s is not None and not s.error and not s.failed:
            continue
        out.append(c)
    return out


def needs_attention(c: Check, s: Summary) -> bool:
    return s.status(c) in ("fail", "warn", "error")


def unfiltered_by(c: Check) -> list[str]:
    """Active filters this check cannot apply (it is not tied to a submission type or organization)."""
    f = filters()
    out = []
    if f.submission_types and FILTER_SUBMISSION_TYPE not in c.filters:
        out.append("submission type")
    if f.offices and FILTER_OFFICE not in c.filters:
        out.append("organization")
    return out


def no_results_note() -> None:
    md('<div class="dq-muted" style="padding:10px 2px">No checks match your filters.</div>')


def footer(ran_at: datetime, sums: dict[str, Summary] | None = None) -> None:
    if SETTINGS.snapshot_mode:
        stamps = [s.as_of for s in (sums or {}).values() if s.as_of]
        when = max(stamps).strftime("%b %d, %Y %I:%M %p") if stamps else "not yet refreshed"
        text = f"Data as of {when}"
    elif SETTINGS.demo_mode:
        text = "Demo data"
    else:
        text = f"Live results from {ran_at:%b %d, %Y %I:%M %p}. Cached for {SETTINGS.cache_minutes} minutes."
    md(f'<div class="dq-muted" style="margin-top:24px">{esc(text)}</div>')


# --------------------------------------------------------------------------- #
# Area summary visuals
# --------------------------------------------------------------------------- #


def area_ring(checks: list[Check], sums: dict[str, Summary], size: int = 76) -> str:
    score = data.weighted_score(checks, sums)
    status = data.score_status(score) if checks else "none"
    return (
        f'<div class="dq-ring-wrap">{ring(score, status, size)}<div>'
        f'<div class="dq-ring-num">{fmt_pct(score)}</div>{status_pill(status)}</div></div>'
    )


def kind_meters(checks: list[Check], sums: dict[str, Summary]) -> str:
    out = ""
    for dim in DIMENSION_ORDER:
        d = [c for c in checks if c.dimension == dim]
        if not d:
            continue
        sc = data.weighted_score(d, sums)
        out += meter_row(KIND_WORDS[dim], sc, data.score_status(sc),
                         f"Share of records that pass, across {len(d)} checks.")
    return out


# --------------------------------------------------------------------------- #
# Check row
# --------------------------------------------------------------------------- #


def pills(c: Check) -> str:
    out = systems_chips(c.systems) + plain_chip(KIND_WORDS[c.dimension])
    out += f'<span class="dq-chip dq-sev-{esc(c.severity)}">{esc(c.severity)} impact</span>'
    missing = unfiltered_by(c)
    if missing:
        out += f'<span class="dq-chip" style="border-style:dashed">All {" & ".join(m + "s" for m in missing)}</span>'
    return out


# One panel per list. Every row uses the same grid as the header, so each detail sits in a
# labeled column the eye can scan down. The grid collapses to a stacked layout when narrow.
# Rule wording lives on the Rule list page, so rows carry no hover text.

_IMPACT_DOT = {"High": "var(--crit)", "Medium": "var(--warn)", "Low": "var(--muted)"}


def _list_header() -> str:
    return (
        '<div class="dq-cq"><div class="dq-grid dq-head">'
        "<span>Check</span><span>Problem type</span><span>Impact</span><span>Records affected</span>"
        "</div></div>"
    )


def _records_cell(c: Check, s: Summary) -> str:
    if s.error:
        return '<div class="dq-num">Could not run</div>'
    if not s.checked:
        return '<div class="dq-num dq-quiet">No records for these filters</div>'
    if not s.failed:
        return '<div class="dq-num dq-quiet">None</div>'
    return (f'<div class="dq-num"><b>{fmt_int(s.failed)}</b> {esc(c.record_label)}'
            f'<span class="dq-pct">{fmt_pct(1 - (s.pass_rate or 0))}</span></div>')


def _row_cells(c: Check, s: Summary) -> str:
    status = s.status(c)
    missing = unfiltered_by(c)
    note = f'<div class="dq-note">Covers all {" and ".join(m + "s" for m in missing)}</div>' if missing else ""
    return (
        '<div class="dq-cq"><div class="dq-grid">'
        f'<div class="dq-c-check"><span class="dq-c-icon" role="img" aria-label="{esc(STATUS_LABEL.get(status, status))}">'
        f'{status_icon(status, 18)}</span><div><div class="dq-c-title">{esc(c.title)}</div>{note}</div></div>'
        f'<div class="dq-c-kind">{esc(KIND_WORDS[c.dimension])}</div>'
        f'<div class="dq-c-imp"><span class="dq-dot" style="background:{_IMPACT_DOT[c.severity]}"></span>'
        f'<span>{esc(c.severity)}<span class="dq-narrow"> impact</span></span></div>'
        f'<div class="dq-c-rec">{_records_cell(c, s)}</div>'
        "</div></div>"
    )


def _row(c: Check, s: Summary, key: str) -> None:
    with st.container(key=f"dqrow_{key}_{c.id}"):
        cells, action = st.columns([12, 1], vertical_alignment="center")
        with cells:
            md(_row_cells(c, s))
        with action:
            if st.button("Open", key=f"in_{key}_{c.id}", width="stretch",
                         help="See the records to fix and download them to Excel."):
                check_dialog(c.id, s)


def check_rows(checks: list[Check], sums: dict[str, Summary], key: str, grouped: bool = True) -> None:
    """All checks in one panel, most serious first. Grouped splits those that need attention from the rest."""
    if not checks:
        no_results_note()
        return
    ordered = sort_by_priority(checks, sums)
    groups = [("", ordered)]
    if grouped:
        attention = [c for c in ordered if needs_attention(c, sums[c.id])]
        groups = [("Needs attention", attention), ("Healthy", [c for c in ordered if c not in attention])]
    with glass(f"list_{key}"):
        with st.container(key=f"dqhead_{key}"):
            head, _ = st.columns([12, 1], vertical_alignment="center")
            with head:
                md(_list_header())
        for title, rows in groups:
            if not rows:
                continue
            if title:
                md(f'<div class="dq-group">{esc(title)}<span>{len(rows)}</span></div>')
            for c in rows:
                _row(c, sums[c.id], key)


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #


def _filter_note(c: Check, f: Filters) -> str:
    applied = f.applied_to(c)
    note = (f"Date used: {c.date_label.lower()}." if "date" in c.filters
            else "Uses the latest data. The time period does not apply.")
    if applied:
        note += " Filtered by " + ", ".join(applied).lower() + "."
    missing = unfiltered_by(c)
    if missing:
        note += f" Not filtered by {' or '.join(missing)}."
    return note


@st.dialog("Check details", width="large")
def check_dialog(check_id: str, s: Summary) -> None:
    c = data.check_by_id(check_id)
    f = filters()
    md(
        f'<div style="margin-bottom:6px">{pills(c)}</div>'
        f'<div style="font-size:21px;font-weight:650;letter-spacing:-.02em;line-height:1.25">{esc(c.title)}</div>'
        f'<div class="dq-small" style="margin-top:4px">{esc(c.question)}</div>'
    )
    if s.error:
        st.error(s.error, icon=":material/error:")
        return
    if s.note:
        st.warning(s.note, icon=":material/history:")
    pct = s.pass_rate
    md(
        f'<div style="display:flex;gap:18px;align-items:center;margin:12px 0 2px">'
        f'{ring(pct, s.status(c), 58, 7)}'
        f'<div><div>{status_pill(s.status(c))} <span class="dq-muted">&nbsp;{fmt_pct(pct)} pass</span></div>'
        f'<div class="dq-small" style="margin-top:4px"><b>{fmt_int(s.failed)}</b> of {fmt_int(s.checked)} '
        f'{esc(c.record_label)} have this problem</div></div></div>'
        f'<div class="dq-muted" style="margin:8px 0 6px">{esc(_filter_note(c, f))}</div>'
    )

    with st.expander("About this check"):
        md(f'<div class="dq-small" style="line-height:1.6"><b>Why it matters:</b> {esc(c.why)}<br><br>'
           f"<b>How to fix:</b> {esc(c.how_to_fix)}</div>")
        if c.assumptions:
            items = "".join(f'<li style="margin:2px 0">{esc(a)}</li>' for a in c.assumptions)
            md('<div class="dq-small" style="margin-top:10px;line-height:1.6"><b>Assumptions to confirm</b>'
               f'<ul style="margin:4px 0 0 18px;padding:0">{items}</ul></div>')

    views = ["Records", "Trend", "Breakdown"]
    view = st.segmented_control("View", views, default=views[0], key=f"mv_{c.id}",
                                label_visibility="collapsed") or views[0]
    try:
        if view == "Trend":
            _trend_tab(c, f)
        elif view == "Breakdown":
            _breakdown_tab(c, f)
        else:
            _records(c, s, f)
    except Exception as exc:
        from dq.db import friendly_error

        st.error(friendly_error(exc), icon=":material/error:")


def _records(c: Check, s: Summary, f: Filters) -> None:
    if not s.failed:
        md(f'<div class="dq-small" style="margin-top:8px">{status_icon("pass")} Nothing to fix.</div>')
        return
    with st.spinner("Loading the records…"):
        raw = data.detail(c, f)
    df = steward_frame(raw, technical=False)
    q = st.text_input("Search", key=f"q_{c.id}", placeholder="Search records",
                      icon=":material/search:", label_visibility="collapsed")
    if q:
        df = df[df.astype(str).apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)]
    capped = len(raw) >= SETTINGS.detail_row_limit
    md(f'<div class="dq-muted" style="margin:2px 0 6px">Showing {fmt_int(len(df))} of {fmt_int(s.failed)}'
       + (f" (first {fmt_int(SETTINGS.detail_row_limit)} only)" if capped else "") + ".</div>")
    st.dataframe(df, hide_index=True, height=min(420, 38 + 35 * max(1, len(df))),
                 column_config={"Problem": st.column_config.TextColumn(width="medium"),
                                "What's wrong": st.column_config.TextColumn(width="large")})
    st.download_button(
        "Download to Excel", excel_bytes(c, s, f, df),
        file_name=f"{c.id.lower()}_{datetime.now():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:", on_click="ignore", type="primary",
    )
