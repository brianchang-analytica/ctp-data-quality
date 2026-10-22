"""CTP Data Quality — MVP. The fuller v2 design is kept in app_v2.py (streamlit run app_v2.py)."""

from pathlib import Path

import streamlit as st

st.set_page_config(page_title="CTP Data Quality", page_icon=":material/verified:", layout="wide")

# Pages are found relative to this file, so the app also runs when the single-file bundle
# (tools/build_app_bundle.py) unpacks it into another folder.
VIEWS = Path(__file__).resolve().parent / "views"

from dq.checks.core import KIND_WORDS  # noqa: E402
from ui import sidebar  # noqa: E402
from ui.theme import inject_theme  # noqa: E402

inject_theme()

# The filter panel is drawn here, before any page runs, so one set of choices drives every page.
st.session_state["filters"] = sidebar.render(show_technical=False, kind_labels=KIND_WORDS)

pages = [
    st.Page(VIEWS / "overview.py", title="Overview", icon=":material/space_dashboard:", default=True),
    st.Page(VIEWS / "checks.py", title="Checks", icon=":material/fact_check:"),
    st.Page(VIEWS / "trace.py", title="Trace a submission", icon=":material/travel_explore:"),
    st.Page(VIEWS / "about.py", title="About", icon=":material/help:"),
]
st.navigation(pages, position="top").run()
