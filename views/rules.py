"""Rule list: every rule the app checks, in plain language.

The wording comes from the workspace rule file when DQ_RULES_PATH is set (see dq/rules.py),
otherwise from the app itself. The Problem type and Impact filters in the side panel apply here.
"""

import io

import pandas as pd
import streamlit as st

from dq import rules
from dq.checks import ALL_CHECKS
from dq.checks.core import AREA_ORDER, AREAS, KIND_WORDS, area_of, areas_of
from dq.model import FILTER_DATE, SEVERITIES, SYSTEMS, Check
from dq.settings import SETTINGS
from ui import data, simple
from ui.components import esc, md

md('<div class="dq-hero-title">Rule list</div>'
   '<div class="dq-hero-sub">Every rule the app checks, in plain language. Each rule is one check.</div>')
st.write("")

source = data.rule_source()
if source.from_file:
    changed = f", last changed {source.changed_at:%b %d, %Y}" if source.changed_at else ""
    md(f'<div class="dq-muted">Wording from the workspace file <b>{esc(source.path)}</b>{changed}.</div>')
elif not source.path:
    md('<div class="dq-muted">Wording built into the app. To manage it in the Databricks workspace, '
       "see the end of this page.</div>")
if source.problems:
    shown = source.problems[:5]
    more = len(source.problems) - len(shown)
    st.warning("\n\n".join(shown) + (f"\n\nAnd {more} more." if more > 0 else ""), icon=":material/rule:")
st.write("")

every = simple.visible(data.checks_in_app(), {})

left, right = st.columns([1.2, 2], vertical_alignment="center")
with left:
    query = st.text_input("Search", key="rule_q", placeholder="Search rules", icon=":material/search:",
                          label_visibility="collapsed")
with right:
    choice = st.segmented_control("System", ["all"] + AREA_ORDER, default="all", key="rule_area",
                                  format_func=lambda k: "All" if k == "all" else AREAS[k].name,
                                  label_visibility="collapsed") or "all"


def _matches(c: Check, q: str) -> bool:
    text = " ".join([c.id, c.title, c.question, c.why, c.how_to_fix]).lower()
    return all(word in text for word in q.lower().split())


shown = [c for c in every if (choice == "all" or choice in areas_of(c)) and (not query or _matches(c, query))]


def _limits(c: Check) -> str:
    warn, fail = rules.as_pct(c.warn_below), rules.as_pct(c.fail_below)
    return (f"Healthy at {warn}% passing or more. Needs attention from {fail}% up to {warn}%. "
            f"Action needed below {fail}%.")


def _details(c: Check) -> str:
    systems = "".join(f'<span class="dq-def-sys"><span class="dq-dot" style="background:{SYSTEMS[s].color}"></span>'
                      f"{esc(SYSTEMS[s].name)}</span>" for s in c.systems)
    date = esc(c.date_label) if FILTER_DATE in c.filters else "Latest data. The time period does not apply."
    rows = [
        ("What it checks", esc(c.question)),
        ("Why it matters", esc(c.why)),
        ("How to fix", esc(c.how_to_fix)),
        ("Systems", systems),
        ("Problem type", esc(KIND_WORDS[c.dimension])),
        ("Impact", f"{esc(c.severity)}. {esc(SEVERITIES[c.severity]['explain'])}"),
        ("Limits", esc(_limits(c))),
        ("Counts", esc(c.record_label.capitalize())),
        ("Date used", date),
    ]
    if c.assumptions:
        rows.append(("To confirm", "<ul>" + "".join(f"<li>{esc(a)}</li>" for a in c.assumptions) + "</ul>"))
    rows.append(("Rule ID", f'<span class="dq-def-id">{esc(c.id)}</span>'))
    return '<div class="dq-def">' + "".join(f'<div class="dq-def-k">{k}</div><div>{v}</div>' for k, v in rows) + "</div>"


def _rule(c: Check) -> None:
    gap = "\u00a0" * 3  # Markdown collapses plain spaces in the label
    label = f"{c.title}{gap}:gray[{KIND_WORDS[c.dimension]} · {c.severity} impact]"
    if c.id in source.changes:
        label += f"{gap}:blue[Edited in the workspace]"
    with st.expander(label):
        md(_details(c))


if not shown:
    simple.no_results_note()
elif choice == "all":
    for key in AREA_ORDER:
        group = [c for c in shown if area_of(c) == key]
        if group:
            md(f'<div class="dq-group dq-group-page">{esc(AREAS[key].name)}<span>{len(group)}</span></div>')
            for c in group:
                _rule(c)
else:
    for c in shown:
        _rule(c)

# --------------------------------------------------------------------------- #
# Downloads and how to manage the rules in the workspace
# --------------------------------------------------------------------------- #

st.write("")
sheet = pd.DataFrame([
    {
        "Rule": c.title,
        "System": ", ".join(AREAS[a].name for a in areas_of(c)),
        "What it checks": c.question,
        "Why it matters": c.why,
        "How to fix": c.how_to_fix,
        "Problem type": KIND_WORDS[c.dimension],
        "Impact": c.severity,
        "Needs attention below (%)": rules.as_pct(c.warn_below),
        "Action needed below (%)": rules.as_pct(c.fail_below),
        "Counts": c.record_label,
        "To confirm": "\n".join(c.assumptions),
        "Rule ID": c.id,
    }
    for c in shown
])
buf = io.BytesIO()
sheet.to_excel(buf, index=False, sheet_name="Rules")
st.download_button("Download the rule list (Excel)", buf.getvalue(), file_name="ctp_dq_rules.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   icon=":material/download:", on_click="ignore", disabled=not shown)

with st.expander("Manage these rules in Databricks"):
    md(
        '<div class="dq-small" style="font-size:14px;line-height:1.7">'
        "The app can read the rules' wording and limits from a file in the Databricks workspace. "
        "Edit the file there, and the app shows the change within "
        f"{data.RULES_TTL_MINUTES} minutes, or right away after <b>Re-run all checks</b>. "
        "No redeploy is needed.<br><br>"
        "<b>Set it up once</b>"
        '<ol style="margin:4px 0 12px 18px;padding:0">'
        "<li>Download the rule file below.</li>"
        "<li>In the workspace, open a folder the data team owns, such as <b>Shared &gt; ctp_dq</b>. "
        "Click <b>⋮ &gt; Import</b>, choose <b>File</b> and pick the rule file. If Import refuses it, "
        "click <b>Create &gt; File</b>, name it <b>ctp_dq_rules.json</b> and paste the contents.</li>"
        "<li>Open the file's <b>⋮</b> menu and choose <b>Share</b>. Give the app's service principal "
        "<b>Can read</b>. The app page shows the service principal under <b>Authorization</b>.</li>"
        "<li>In the app's <b>app.yaml</b>, set <b>DQ_RULES_PATH</b> to the file's path, for example "
        "<b>/Workspace/Shared/ctp_dq/ctp_dq_rules.json</b>. Then deploy the app again.</li>"
        "</ol>"
        "<b>What you can change</b><br>"
        "The title, what it checks, why it matters, how to fix, the impact, the two limits and the "
        "items to confirm. The logic of each check stays in the app, so a typo in the file cannot "
        "break a check. If the file has a problem, this page says so and the app keeps the built-in wording."
        "</div>"
    )
    st.write("")
    shown_ids = {c.id for c in data.checks_in_app()}
    st.download_button("Download the rule file", rules.to_json([rules.apply(c, source) for c in ALL_CHECKS], shown_ids),
                       file_name=rules.FILE_NAME, mime="application/json", icon=":material/download:",
                       on_click="ignore")
    if SETTINGS.rules_path:
        md(f'<div class="dq-muted" style="margin-top:8px">This app reads <b>{esc(SETTINGS.rules_path)}</b>.</div>')
