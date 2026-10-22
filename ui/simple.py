"""MVP building blocks: compact check rows, area scores, and a focused check dialog.

Every page reads the same global filters (drawn in app.py): the time period, submission
type and organization narrow the records each check looks at; "kinds of problems", "impact"
and "only checks that found problems" decide which checks appear anywhere.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from dq.checks import CHECKS_BY_ID
from dq.checks.core import AREAS, KIND_WORDS, active_checks, area_of
from dq.engine import Filters, Summary
from dq.model import DIMENSION_ORDER, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, SEVERITIES, SYSTEMS, Check
from dq.settings import SETTINGS
from ui import data, sidebar
from ui.checks_view import _breakdown_tab, _trend_tab, excel_bytes, sort_by_priority, steward_frame
from ui.components import (
    banner,
    esc,
    fmt_int,
    fmt_pct,
    glass_row,
    md,
    meter_row,
    plain_chip,
    ring,
    status_icon,
    status_pill,
    systems_chips,
    tip,
)


def filters() -> Filters:
    return st.session_state["filters"]


# --------------------------------------------------------------------------- #
# Results, scoped by the global filters
# --------------------------------------------------------------------------- #


def load() -> tuple[list[Check], dict[str, Summary], datetime]:
    """Run the active checks under the current filters and return those the filters let through."""
    all_checks = active_checks()
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
    out = systems_chips(c.systems) if c.is_cross_system else ""
    out += plain_chip(KIND_WORDS[c.dimension])
    out += f'<span class="dq-chip dq-sev-{esc(c.severity)}" title="{esc(SEVERITIES[c.severity]["explain"])}">{esc(c.severity)} impact</span>'
    missing = unfiltered_by(c)
    if missing:
        out += (f'<span class="dq-chip" style="border-style:dashed" title="Not filtered by '
                f'{" or ".join(missing)}. This check covers all of them.">All {" & ".join(m + "s" for m in missing)}</span>')
    return out


def check_row(c: Check, s: Summary, key: str) -> None:
    with glass_row(f"{key}_{c.id}"):
        left, mid, right = st.columns([6, 2.2, 1.2], vertical_alignment="center")
        with left:
            md(
                f'<div style="display:flex;gap:10px;align-items:flex-start">'
                f'<span style="margin-top:2px">{status_icon(s.status(c), 18)}</span><div>'
                f'<div class="dq-title">{esc(c.title)}{tip(c.question + chr(10) + chr(10) + "Why it matters: " + c.why, "left")}</div>'
                f'<div style="margin-top:6px">{pills(c)}</div></div></div>'
            )
        with mid:
            if s.error:
                md('<div class="dq-small">Could not run</div>')
            elif not s.checked:
                md('<div class="dq-muted">No records for these filters</div>')
            else:
                md(
                    f'<div class="dq-title" style="font-variant-numeric:tabular-nums">{fmt_int(s.failed)} {esc(c.record_label)}</div>'
                    f'<div class="dq-muted">{fmt_pct(1 - (s.pass_rate or 0))} of {fmt_int(s.checked)} checked</div>'
                )
        with right:
            if st.button("Inspect", key=f"in_{key}_{c.id}", icon=":material/search:", width="stretch"):
                check_dialog(c.id, s)


def check_rows(checks: list[Check], sums: dict[str, Summary], key: str) -> None:
    if not checks:
        no_results_note()
        return
    for c in sort_by_priority(checks, sums):
        check_row(c, sums[c.id], key)


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
    c = CHECKS_BY_ID[check_id]
    f = filters()
    md(
        f'<div style="margin-bottom:6px">{plain_chip(AREAS[area_of(c)].name)}{pills(c)}</div>'
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
