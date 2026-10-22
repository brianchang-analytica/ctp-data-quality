"""About (MVP): what the app does, how to read it, and the rules still being confirmed."""

import io

import pandas as pd
import streamlit as st

from dq.checks.core import AREAS, KIND_WORDS, active_checks, area_of
from dq.model import SYSTEMS
from ui import simple
from ui.components import esc, glass, md, status_pill

md('<div class="dq-hero-title">About</div>'
   '<div class="dq-hero-sub">How to use this app.</div>')
st.write("")

with glass("about_how"):
    md(
        '<div class="dq-small" style="font-size:14.5px;line-height:1.7">'
        "<b>1.</b> Pick an area on <b>Overview</b> or <b>Checks</b>.<br>"
        "<b>2.</b> Open a check, download the records and fix them in the source system. "
        "Fixed records drop off after the next refresh.<br><br>"
        f"{status_pill('pass')}&nbsp; at least 98% of records pass &nbsp;&nbsp; "
        f"{status_pill('warn')}&nbsp; 90–98% pass &nbsp;&nbsp; "
        f"{status_pill('fail')}&nbsp; below 90% pass</div>"
    )

checks = simple.visible(active_checks(), {})
md('<div class="dq-section"><h3>Checks in this version</h3>'
   f'<span class="dq-section-sub">{len(checks)} checks match your filters</span></div>')
lib = pd.DataFrame([
    {
        "Area": AREAS[area_of(c)].name,
        "Systems": " ↔ ".join(SYSTEMS[k].name for k in c.systems),
        "Check": c.title,
        "Kind": KIND_WORDS[c.dimension],
        "Question it answers": c.question,
        "Why it matters": c.why,
        "How to fix": c.how_to_fix,
    }
    for c in checks
])
st.dataframe(lib[["Area", "Check", "Kind", "Question it answers"]], hide_index=True, width="stretch",
             height=min(560, 38 + 35 * len(lib)))
buf = io.BytesIO()
lib.to_excel(buf, index=False, sheet_name="Checks")
st.download_button("Download the check list (Excel)", buf.getvalue(), file_name="ctp_dq_checks.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   icon=":material/download:", on_click="ignore")

pending = [(c.title, a) for c in checks for a in c.assumptions]
if pending:
    md('<div class="dq-section"><h3>Assumptions to confirm</h3>'
       f'<span class="dq-section-sub">{len(pending)} items. Tell the data team if one looks wrong.</span></div>')
    with st.expander("Show assumptions"):
        st.dataframe(pd.DataFrame(pending, columns=["Check", "What we assumed"]), hide_index=True, width="stretch")

md(f'<div class="dq-muted" style="margin-top:24px">{esc("To report a problem with a check, send the check name and an example STN to the data team.")}</div>')
