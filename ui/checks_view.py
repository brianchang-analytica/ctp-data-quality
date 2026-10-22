"""Check list rows and the 'inspect a check' dialog shared by every page."""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dq.checks import CHECKS_BY_ID
from dq.engine import BREAKDOWNS, Filters, Summary
from dq.model import (
    CONTRACT_COLUMNS,
    DIMENSIONS,
    ENTITIES,
    FILTER_OFFICE,
    FILTER_SUBMISSION_TYPE,
    SEVERITIES,
    SYSTEMS,
    Check,
)
from dq.settings import SETTINGS
from ui import data, sidebar
from ui.components import (
    dimension_chip,
    esc,
    fmt_int,
    fmt_pct,
    glass_row,
    md,
    plain_chip,
    severity_chip,
    status_icon,
    status_pill,
    systems_chips,
    tip,
)

ACCENT = "#2a78d6"


# --------------------------------------------------------------------------- #
# Filtering what is listed
# --------------------------------------------------------------------------- #


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


def sort_by_priority(checks: list[Check], sums: dict[str, Summary]) -> list[Check]:
    return sorted(checks, key=lambda c: -data.priority(c, sums[c.id]) if c.id in sums else 0)


# --------------------------------------------------------------------------- #
# Row
# --------------------------------------------------------------------------- #


def count_text(c: Check, s: Summary) -> str:
    if s.error:
        return (
            f'<div class="dq-title" style="color:var(--ink-2)">Could not run'
            f'{tip("This check hit an error. Open it to see the message, or ask the data team. " + s.error[:240], "right")}</div>'
        )
    if not s.checked:
        return '<div class="dq-title" style="color:var(--ink-2)">Nothing to check</div><div class="dq-muted">No records in this period</div>'
    return (
        f'<div class="dq-title" style="font-variant-numeric:tabular-nums">{fmt_int(s.failed)} {esc(c.record_label)}</div>'
        f'<div class="dq-muted">of {fmt_int(s.checked)} checked · {fmt_pct(1 - (s.pass_rate or 0))} with a problem</div>'
    )


def check_row(c: Check, s: Summary, filters: Filters, key: str) -> None:
    with glass_row(f"{key}_{c.id}"):
        left, mid, right = st.columns([6.2, 2.4, 1.3], vertical_alignment="center")
        with left:
            md(
                f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">{status_pill(s.status(c))}</div>'
                f'<div class="dq-title">{esc(c.title)}{tip(c.question + chr(10) + chr(10) + "Why it matters: " + c.why, "left")}</div>'
                f'<div style="margin-top:6px">{systems_chips(c.systems)}{plain_chip(c.entity)}'
                f"{dimension_chip(c.dimension)}{severity_chip(c.severity)}</div>"
            )
        with mid:
            md(count_text(c, s))
        with right:
            if st.button("Inspect", key=f"btn_{key}_{c.id}", icon=":material/search:", width="stretch",
                         help="See the records to fix and how to fix them."):
                check_dialog(c.id)


def check_list(checks: list[Check], sums: dict[str, Summary], filters: Filters, key: str,
               empty_text: str = "No checks match the current filters.") -> None:
    shown = visible(checks, sums)
    if not shown:
        md(f'<div class="dq-muted" style="padding:10px 4px">{esc(empty_text)}</div>')
        return
    for c in sort_by_priority(shown, sums):
        check_row(c, sums[c.id], filters, key)


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif", size=12.5, color="#4a4d57"),
    margin=dict(l=8, r=12, t=8, b=8),
    hoverlabel=dict(bgcolor="rgba(22,24,30,0.92)", font=dict(color="#f4f5f7", size=12.5), bordercolor="rgba(0,0,0,0)"),
    showlegend=False,
)
_AXIS = dict(showgrid=True, gridcolor="rgba(21,22,26,0.07)", gridwidth=1, zeroline=False, linecolor="rgba(21,22,26,0.12)", ticks="")


def trend_chart(df: pd.DataFrame, c: Check, grain: str) -> go.Figure:
    df = df.copy()
    df["rate"] = df["failed"] / df["checked"].where(df["checked"] > 0)
    label_fmt = {"MONTH": "%b %Y", "QUARTER": "Q%q %Y", "YEAR": "%Y", "WEEK": "Week of %b %d, %Y"}[grain]
    df["label"] = pd.to_datetime(df["period"]).dt.strftime(label_fmt.replace("%q", "{q}"))
    if grain == "QUARTER":
        q = pd.to_datetime(df["period"]).dt.quarter.astype(str)
        df["label"] = [lbl.replace("{q}", qq) for lbl, qq in zip(df["label"], q)]
    custom = list(zip(df["label"], df["failed"].map(fmt_int), df["checked"].map(fmt_int)))
    fig = go.Figure(
        go.Scatter(
            x=pd.to_datetime(df["period"]),
            y=df["rate"],
            mode="lines+markers",
            line=dict(color=ACCENT, width=2, shape="linear"),
            marker=dict(size=8, color=ACCENT, line=dict(color="#ffffff", width=2)),
            customdata=custom,
            hovertemplate="<b>%{customdata[0]}</b><br>%{y:.1%} with a problem<br>%{customdata[1]} of %{customdata[2]} "
            + esc(c.record_label) + "<extra></extra>",
            fill="tozeroy",
            fillcolor="rgba(42,120,214,0.08)",
        )
    )
    fig.update_layout(**_LAYOUT, height=290, hovermode="x unified" if len(df) > 30 else "closest")
    fig.update_xaxes(**{**_AXIS, "showgrid": False})
    fig.update_yaxes(**_AXIS, tickformat=".0%", rangemode="tozero", title=None)
    return fig


def breakdown_chart(df: pd.DataFrame, c: Check, by: str) -> go.Figure:
    df = df.copy().head(15)
    if by == "issue_category":
        df["value"] = df["failed"]
        hover = "<b>%{y}</b><br>%{x:,} " + esc(c.record_label) + "<extra></extra>"
        fmt = ","
    else:
        df["value"] = df["failed"] / df["checked"].where(df["checked"] > 0)
        df["n"] = list(zip(df["failed"].map(fmt_int), df["checked"].map(fmt_int)))
        hover = "<b>%{y}</b><br>%{x:.1%} with a problem<br>%{customdata[0]} of %{customdata[1]}<extra></extra>"
        fmt = ".0%"
    df = df.sort_values("value")
    fig = go.Figure(
        go.Bar(
            x=df["value"], y=df["bucket"], orientation="h",
            marker=dict(color=ACCENT, cornerradius=4, line=dict(width=0)),
            customdata=df["n"] if "n" in df else None,
            hovertemplate=hover,
            texttemplate="%{x:" + fmt + "}", textposition="outside", cliponaxis=False,
            textfont=dict(color="#4a4d57"),
        )
    )
    fig.update_layout(**_LAYOUT, height=max(160, 38 * len(df) + 40), bargap=0.35)
    fig.update_xaxes(**{**_AXIS, "showgrid": False}, tickformat=fmt, rangemode="tozero", showticklabels=False)
    fig.update_yaxes(showgrid=False, linecolor="rgba(0,0,0,0)", ticks="", automargin=True)
    return fig


_CHART_CONFIG = {"displayModeBar": False, "responsive": True}


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #


def _about_frame(c: Check, s: Summary, filters: Filters) -> pd.DataFrame:
    rows = [
        ("Check", c.title),
        ("Question", c.question),
        ("Systems", " ↔ ".join(SYSTEMS[k].name for k in c.systems)),
        ("Business area", c.entity),
        ("Kind of problem", c.dimension),
        ("Impact", c.severity),
        ("Why it matters", c.why),
        ("How to fix", c.how_to_fix),
        ("Records with a problem", s.failed),
        ("Records checked", s.checked),
        ("Date filter", f"{c.date_label}: {filters.start or 'any'} to {filters.end or 'any'}"),
        ("Submission types", ", ".join(filters.submission_types) or "All"),
        ("Organizations", ", ".join(filters.offices) or "All"),
        ("Exported", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Data", "DEMO DATA, not real CTP data" if SETTINGS.demo_mode else f"Live, catalog {SETTINGS.catalog}"),
    ]
    rows += [("Assumption", a) for a in c.assumptions]
    return pd.DataFrame(rows, columns=["Item", "Value"])


def excel_bytes(c: Check, s: Summary, filters: Filters, records: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        records.to_excel(xw, sheet_name="Records to fix", index=False)
        _about_frame(c, s, filters).to_excel(xw, sheet_name="About this check", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max(len(str(cell.value or "")) for cell in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(70, max(12, width + 2))
            ws.freeze_panes = "A2"
    return buf.getvalue()


def steward_frame(df: pd.DataFrame, technical: bool) -> pd.DataFrame:
    """Records as a steward sees them: the problem first, plumbing columns hidden."""
    df = df.copy()
    rename = {"issue_category": "Problem", "issue_reason": "What's wrong"}
    keep_hidden = [] if technical else [c for c in ("record_key", "record_date", "submission_type", "office") if c in df]
    if technical:
        rename.update({"record_key": "Record ID", "record_date": "Filter date", "submission_type": "Submission type (filter)", "office": "Organization (filter)"})
    df = df.drop(columns=keep_hidden).rename(columns=rename)
    front = [c for c in ("Problem", "What's wrong") if c in df]
    return df[front + [c for c in df.columns if c not in front]]


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #


@st.dialog("Check details", width="large")
def check_dialog(check_id: str) -> None:
    c = CHECKS_BY_ID[check_id]
    filters: Filters = st.session_state["filters"]
    sums, _ = data.summaries(filters)
    s = sums[c.id]
    technical = sidebar.view_prefs()["technical"]

    md(
        f'<div style="margin-bottom:6px">{systems_chips(c.systems)}{plain_chip(c.entity)}'
        f"{dimension_chip(c.dimension)}{severity_chip(c.severity)}</div>"
        f'<div style="font-size:21px;font-weight:650;letter-spacing:-.02em;line-height:1.25">{esc(c.title)}</div>'
        f'<div class="dq-small" style="margin-top:4px">{esc(c.question)}</div>'
    )

    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("With a problem", fmt_int(s.failed) if not s.error else "—",
                  help=f"Number of {c.record_label} that failed this check in the selected period.")
    with k2:
        st.metric("Checked", fmt_int(s.checked) if not s.error else "—",
                  help=f"Number of {c.record_label} this check looked at, after your filters.")
    with k3:
        md(
            '<div class="dq-kpi-label" style="margin-top:2px">Result'
            + tip(f"Healthy: at least {c.warn_below:.1%} pass. Needs attention: {c.fail_below:.0%}–{c.warn_below:.1%}. "
                  f"Action needed: below {c.fail_below:.0%}.", "right")
            + f'</div><div style="margin-top:10px">{status_pill(s.status(c))}'
            + (f'<span class="dq-muted">&nbsp; {fmt_pct(s.pass_rate)} pass</span>' if s.pass_rate is not None else "")
            + "</div>"
        )

    applied = filters.applied_to(c)
    ignored = []
    if FILTER_SUBMISSION_TYPE not in c.filters and filters.submission_types:
        ignored.append("submission type")
    if FILTER_OFFICE not in c.filters and filters.offices:
        ignored.append("organization")
    note = f"Dates used: {c.date_label.lower()}." if "date" in c.filters else "This check always looks at the latest data; the time period does not apply."
    if applied:
        note += " Filtered by: " + ", ".join(applied) + "."
    if ignored:
        note += " Your " + " and ".join(ignored) + " filter does not apply to this check."
    md(f'<div class="dq-muted" style="margin:2px 0 8px">{esc(note)}</div>')

    if s.error:
        st.error(s.error, icon=":material/error:")

    # A segmented control instead of st.tabs, so only the open view runs its query.
    views = {
        "Records to fix": lambda: _records_tab(c, s, filters, technical),
        "Over time": lambda: _trend_tab(c, filters),
        "Where it happens": lambda: _breakdown_tab(c, filters),
        "About this check": lambda: _about_tab(c),
    }
    if technical:
        views["Technical"] = lambda: _technical_tab(c, filters)
    view = st.segmented_control("View", list(views), default="Records to fix", key=f"view_{c.id}",
                                label_visibility="collapsed") or "Records to fix"
    try:
        views[view]()
    except Exception as exc:
        from dq.db import friendly_error

        st.error(friendly_error(exc), icon=":material/error:")


def _records_tab(c: Check, s: Summary, filters: Filters, technical: bool) -> None:
    if s.error:
        st.info("Records are not available because the check could not run.")
        return
    if not s.failed:
        md(f'<div class="dq-small">{status_icon("pass")} Nothing to fix — every {esc(c.record_label[:-1] if c.record_label.endswith("s") else c.record_label)} passed.</div>')
        return
    with st.spinner("Loading the records…"):
        try:
            raw = data.detail(c, filters)
        except Exception as exc:
            from dq.db import friendly_error

            st.error(friendly_error(exc))
            return
    df = steward_frame(raw, technical)
    q = st.text_input("Search these records", key=f"q_{c.id}", placeholder="Search records",
                      icon=":material/search:", label_visibility="collapsed")
    if q:
        mask = df.astype(str).apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)
        df = df[mask]
    shown = len(df)
    capped = len(raw) >= SETTINGS.detail_row_limit
    md(
        f'<div class="dq-muted" style="margin:2px 0 6px">Showing {fmt_int(shown)} of {fmt_int(s.failed)} {esc(c.record_label)} with a problem'
        + (f" (first {fmt_int(SETTINGS.detail_row_limit)} only. Narrow the filters to see more)" if capped else "")
        + ".</div>"
    )
    st.dataframe(
        df,
        hide_index=True,
        height=min(420, 38 + 35 * max(1, shown)),
        column_config={
            "Problem": st.column_config.TextColumn("Problem", help="The kind of problem found.", width="medium"),
            "What's wrong": st.column_config.TextColumn("What's wrong", help="Exactly what is wrong with this record.", width="large"),
        },
    )
    d1, d2, _ = st.columns([1.3, 1.3, 3])
    stamp = datetime.now().strftime("%Y%m%d")
    with d1:
        st.download_button("Download Excel", excel_bytes(c, s, filters, df), file_name=f"{c.id.lower()}_{stamp}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           icon=":material/download:", on_click="ignore", type="primary", width="stretch",
                           help="Includes a second sheet that explains the check and the filters used.")
    with d2:
        st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"), file_name=f"{c.id.lower()}_{stamp}.csv",
                           mime="text/csv", icon=":material/table:", on_click="ignore", width="stretch")


def _trend_tab(c: Check, filters: Filters) -> None:
    if "date" not in c.filters:
        md('<div class="dq-small">No trend for this check. It uses the latest data only.</div>')
        return
    grain = st.segmented_control("Group by", ["Month", "Quarter", "Year"], default="Month", key=f"grain_{c.id}",
                                 help="How to group records by their date.") or "Month"
    try:
        df = data.trend(c, filters, grain.upper())
    except Exception as exc:
        from dq.db import friendly_error

        st.error(friendly_error(exc))
        return
    if df.empty:
        st.info("No dated records in this period.")
        return
    md(f'<div class="dq-small" style="margin-bottom:4px">Share of {esc(c.record_label)} with this problem, by {esc(c.date_label.lower())}. '
       "A rising line means it is getting worse.</div>")
    st.plotly_chart(trend_chart(df, c, grain.upper()), config=_CHART_CONFIG, width="stretch")
    with st.expander("Show numbers"):
        t = df.copy()
        t["With a problem (%)"] = (t["failed"] / t["checked"].where(t["checked"] > 0) * 100).round(2)
        st.dataframe(t.rename(columns={"period": "Period", "checked": "Checked", "failed": "With a problem"}), hide_index=True)


def _breakdown_tab(c: Check, filters: Filters) -> None:
    options = ["issue_category"]
    if FILTER_SUBMISSION_TYPE in c.filters:
        options.append("submission_type")
    if FILTER_OFFICE in c.filters:
        options.append("office")
    by = st.segmented_control("Break down by", options, default="issue_category", format_func=BREAKDOWNS.get,
                              key=f"by_{c.id}", help="See where the problems are concentrated.") or "issue_category"
    try:
        df = data.breakdown(c, filters, by)
    except Exception as exc:
        from dq.db import friendly_error

        st.error(friendly_error(exc))
        return
    if df.empty or df["failed"].sum() == 0:
        st.info("No problems in this period.")
        return
    caption = (f"Number of {c.record_label} by type of problem." if by == "issue_category"
               else f"Share of {c.record_label} with a problem, by {BREAKDOWNS[by].lower()}.")
    md(f'<div class="dq-small" style="margin-bottom:4px">{esc(caption)}</div>')
    st.plotly_chart(breakdown_chart(df, c, by), config=_CHART_CONFIG, width="stretch")


def _about_tab(c: Check) -> None:
    md(
        f'<div class="dq-small" style="font-size:14px;line-height:1.6">'
        f'<b>What this checks</b><br>{esc(c.question)}<br><br>'
        f'<b>Why it matters</b><br>{esc(c.why)}<br><br>'
        f'<b>How to fix a flagged record</b><br>{esc(c.how_to_fix)}<br><br>'
        f'<b>Kind of problem</b> — {esc(c.dimension)}: {esc(DIMENSIONS[c.dimension]["explain"])}<br>'
        f'<b>Impact</b> — {esc(c.severity)}: {esc(SEVERITIES[c.severity]["explain"])}<br>'
        f'<b>Business area</b> — {esc(c.entity)}: {esc(ENTITIES[c.entity])}</div>'
    )
    if c.assumptions:
        md('<div class="dq-small" style="margin-top:14px"><b>Still being confirmed with the data owners</b>'
           + tip("These are rules we could not yet confirm from the data. If one looks wrong to you, tell the data team. "
                 "it may explain unexpected results.", "left") + "</div>")
        for a in c.assumptions:
            st.markdown(f"- {a}")


def _technical_tab(c: Check, filters: Filters) -> None:
    from dq import engine

    if c.technical_note:
        st.markdown(c.technical_note)
    st.caption(f"Check ID: {c.id} · thresholds: green ≥ {c.warn_below:.1%}, red < {c.fail_below:.1%}")
    st.markdown("**Base query** (one row per record checked)")
    st.code(c.render_sql(SETTINGS.placeholders()), language="sql")
    q = engine.summary_query(c, filters)
    with st.expander("Summary query as run, with parameters"):
        st.code(q.sql, language="sql")
        st.json({k: str(v) for k, v in q.params.items()})
    md(f'<div class="dq-muted">Contract columns: {", ".join(CONTRACT_COLUMNS)}</div>')
