"""Guide: how to use the app, what every word means, and the full list of checks."""

import io

import pandas as pd
import streamlit as st

from dq.checks import ALL_CHECKS
from dq.model import DIMENSIONS, ENTITIES, SEVERITIES, SYSTEM_ORDER, SYSTEMS
from dq.settings import SETTINGS
from ui import sidebar
from ui.components import esc, glass, hero, md, section, system_header

hero(
    "Guide",
    "How to use the Data Quality Center",
    "No technical background needed. This page explains the systems, the three kinds of problems we look for, "
    "how scores work, and lists every check in plain language.",
)

section("Three steps")
steps = [
    ("1", "Start on the Overview", "See which system or comparison needs attention. Red means act now, amber means keep an eye on it."),
    ("2", "Open a system or a comparison", "“By system” lists every check for one system. “Do systems agree?” lines up two systems side by side."),
    ("3", "Inspect, download, fix", "Open a check to see the exact records, download them to Excel, and correct them in the source system. "
                                    "Problems clear on the next refresh."),
]
cols = st.columns(3)
for col, (n, title, body) in zip(cols, steps):
    with col:
        with glass(f"step{n}"):
            md(f'<div class="dq-sys-badge" style="background:linear-gradient(145deg,#2a78d6,#6250d6);margin-bottom:10px">{n}</div>'
               f'<div class="dq-title">{esc(title)}</div><div class="dq-small" style="margin-top:4px">{esc(body)}</div>')

section("The systems")
cols = st.columns(4)
for col, key in zip(cols, SYSTEM_ORDER):
    with col:
        with glass(f"g_sys_{key}"):
            md(system_header(key) + f'<div class="dq-small" style="margin-top:10px">{esc(SYSTEMS[key].blurb)}</div>')

section("Three kinds of problems")
cols = st.columns(3)
for col, (dim, info) in zip(cols, DIMENSIONS.items()):
    with col:
        with glass(f"g_dim_{dim}"):
            md(f'<div class="dq-title">{esc(dim)} — {esc(info["question"])}</div><div class="dq-small" style="margin-top:4px">{esc(info["explain"])}</div>')

section("How scores work")
with glass("g_scores"):
    md(
        '<div class="dq-small" style="font-size:14px;line-height:1.65">'
        "<b>Each check</b> looks at a set of records (for example, every submission received in the period) and counts how "
        "many have the problem. Its <b>pass rate</b> is the share without the problem.<br>"
        "<b>Healthy</b> means at least 98% pass, <b>Needs attention</b> means 90–98%, and <b>Action needed</b> means below 90%. "
        "A few critical checks (like a data feed that stopped) use stricter limits.<br>"
        "<b>System and overall scores</b> average the pass rates of their checks, with "
        + ", ".join(f"{k.lower()}-impact checks counting {v['weight']}×" for k, v in SEVERITIES.items())
        + ".<br><b>Filters</b> in the sidebar change which records are checked. Each check notes which filters apply to it.</div>"
    )

section("Every check", f"{len(ALL_CHECKS)} checks")
lib = pd.DataFrame([
    {
        "Systems": " ↔ ".join(SYSTEMS[k].name for k in c.systems),
        "Business area": c.entity,
        "Kind": c.dimension,
        "Impact": c.severity,
        "Check": c.title,
        "Question it answers": c.question,
        "Why it matters": c.why,
        "How to fix": c.how_to_fix,
        **({"Check ID": c.id, "Technical note": c.technical_note} if sidebar.view_prefs()["technical"] else {}),
    }
    for c in ALL_CHECKS
])
q = st.text_input("Search checks", placeholder="Search by word, e.g. “company”, “received date”, “PMS”…",
                  icon=":material/search:", label_visibility="collapsed")
view = lib[lib.apply(lambda r: r.astype(str).str.contains(q, case=False, regex=False).any(), axis=1)] if q else lib
st.dataframe(view, hide_index=True, width="stretch", height=min(520, 38 + 35 * max(1, len(view))),
             column_config={"Check": st.column_config.TextColumn(width="large"),
                            "Why it matters": st.column_config.TextColumn(width="large"),
                            "How to fix": st.column_config.TextColumn(width="large")})
buf = io.BytesIO()
lib.to_excel(buf, index=False, sheet_name="Checks")
st.download_button("Download the check list (Excel)", buf.getvalue(), file_name="ctp_dq_check_library.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   icon=":material/download:", on_click="ignore")

pending = [(c.title, a) for c in ALL_CHECKS for a in c.assumptions]
if pending:
    section("Rules still being confirmed", f"{len(pending)} items",
            "We could not confirm these from the data alone. If you know the answer, please tell the data team — "
            "it makes the checks more accurate.")
    st.dataframe(pd.DataFrame(pending, columns=["Check", "What we assumed"]), hide_index=True, width="stretch")

section("Glossary")
with glass("g_gloss"):
    terms = {
        "STN": "Submission tracking number — the ID of one submission (for example PM0001234).",
        "Bundle": "A group of submissions or products that Rhapsody reviews together.",
        "Log-in list (iTRAC)": "eSUB's record of each submission as it arrives and is logged in.",
        "RHPM": "Regulatory health project manager — the CTP staff member who manages a submission in Rhapsody.",
        "Reporting warehouse (EDW)": "The consolidated copy of the data that official dashboards use.",
        "Data feed": "The automated copy of a source system's data into the lakehouse, usually daily.",
        **{f"{k} (business area)": v for k, v in ENTITIES.items()},
    }
    md("".join(f'<div class="dq-small" style="padding:6px 0;border-top:1px solid var(--hairline)"><b>{esc(t)}</b> — {esc(d)}</div>'
               for t, d in terms.items()))

if sidebar.view_prefs()["technical"]:
    section("Technical setup")
    st.json({
        "catalog": SETTINGS.catalog,
        "schemas": SETTINGS.schemas,
        "warehouse_id": SETTINGS.warehouse_id or "(not set — demo mode)",
        "auth_mode": SETTINGS.auth_mode,
        "cache_minutes": SETTINGS.cache_minutes,
        "detail_row_limit": SETTINGS.detail_row_limit,
    })
    st.markdown("To add a check, see `dq/checks/AUTHORING.md` and run `python tools/validate_checks.py`.")
