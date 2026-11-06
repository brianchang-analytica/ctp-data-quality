"""About (MVP): what the app does and how to read it. The rules themselves are on the Rule list page."""

from pathlib import Path

import streamlit as st

from ui.components import esc, glass, md, status_pill

md('<div class="dq-hero-title">About</div>'
   '<div class="dq-hero-sub">How to use this app.</div>')
st.write("")

with glass("about_how"):
    md(
        '<div class="dq-small" style="font-size:14.5px;line-height:1.7">'
        "<b>1.</b> Pick a system on <b>Overview</b> or <b>Checks</b>.<br>"
        "<b>2.</b> Open a check, download the records and fix them in the source system. "
        "Fixed records drop off after the next refresh.<br>"
        "<b>3.</b> To see what a check looks for, why it matters and how to fix it, use <b>Rule list</b>.<br><br>"
        f"{status_pill('pass')}&nbsp; at least 98% of records pass &nbsp;&nbsp; "
        f"{status_pill('warn')}&nbsp; 90–98% pass &nbsp;&nbsp; "
        f"{status_pill('fail')}&nbsp; below 90% pass<br>"
        '<span class="dq-muted">These are the usual limits. A rule can set its own, shown on the Rule list.</span></div>'
    )
st.write("")
if st.button("Open the rule list", icon=":material/rule:"):
    st.switch_page(str(Path(__file__).resolve().with_name("rules.py")))

md(f'<div class="dq-muted" style="margin-top:24px">{esc("To report a problem with a check, send the check name and an example STN to the data team.")}</div>')
