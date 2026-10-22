"""CTP Data Quality Center — full v2 design (kept for a future build). Run: streamlit run app_v2.py"""

import streamlit as st

st.set_page_config(
    page_title="CTP Data Quality",
    page_icon=":material/verified:",
    layout="wide",
    initial_sidebar_state="auto",
)

from ui import sidebar  # noqa: E402
from ui.theme import inject_theme  # noqa: E402

inject_theme()
st.session_state["filters"] = sidebar.render()

pages = [
    st.Page("v2/views/home.py", title="Overview", icon=":material/space_dashboard:", default=True),
    st.Page("v2/views/systems.py", title="By system", icon=":material/hub:"),
    st.Page("v2/views/compare.py", title="Do systems agree?", icon=":material/compare_arrows:"),
    st.Page("v2/views/trace.py", title="Trace a submission", icon=":material/travel_explore:"),
    st.Page("v2/views/guide.py", title="Guide", icon=":material/menu_book:"),
]
st.navigation(pages, position="top").run()
