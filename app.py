"""CTP Data Quality — MVP. The fuller v2 design is kept in app_v2.py (streamlit run app_v2.py)."""

import streamlit as st

st.set_page_config(page_title="CTP Data Quality", page_icon=":material/verified:", layout="wide")

from dq.checks.core import KIND_WORDS  # noqa: E402
from ui import sidebar  # noqa: E402
from ui.theme import inject_theme  # noqa: E402

inject_theme()

# The filter panel is drawn here, before any page runs, so one set of choices drives every page.
st.session_state["filters"] = sidebar.render(show_technical=False, kind_labels=KIND_WORDS)

pages = [
    st.Page("views/overview.py", title="Overview", icon=":material/space_dashboard:", default=True),
    st.Page("views/checks.py", title="Checks", icon=":material/fact_check:"),
    st.Page("views/trace.py", title="Trace a submission", icon=":material/travel_explore:"),
    st.Page("views/about.py", title="About", icon=":material/help:"),
]
st.navigation(pages, position="top").run()
