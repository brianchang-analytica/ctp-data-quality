"""Checks: every check in one area, most serious first."""

import streamlit as st

from dq.checks.core import AREA_ORDER, AREAS, area_of
from ui import simple
from ui.components import esc, glass, md

md('<div class="dq-hero-title">Checks</div>'
   '<div class="dq-hero-sub">Open a check to see the records to fix and download them to Excel.</div>')
st.write("")

checks, sums, ran_at = simple.load()

wanted = st.session_state.pop("area", None)
if wanted in AREAS:
    st.session_state["area_choice"] = wanted
elif st.session_state.get("area_choice") not in AREAS:
    st.session_state["area_choice"] = AREA_ORDER[0]


def _label(key: str) -> str:
    n = sum(1 for c in checks if area_of(c) == key and simple.needs_attention(c, sums[c.id]))
    return f"{AREAS[key].name} ({n})" if n else AREAS[key].name


key = st.segmented_control("Area", AREA_ORDER, format_func=_label, key="area_choice",
                           label_visibility="collapsed",
                           help="The number shows how many checks in each area need attention.") or AREA_ORDER[0]
in_area = [c for c in checks if area_of(c) == key]

st.write("")
with glass(f"areahead_{key}"):
    left, right = st.columns([1.3, 2], vertical_alignment="center")
    with left:
        md(f'<div class="dq-sys-name">{esc(AREAS[key].name)}</div>'
           f'<div class="dq-muted" style="margin:2px 0 12px">{esc(AREAS[key].blurb)}</div>'
           + simple.area_ring(in_area, sums, 72))
    with right:
        md(simple.kind_meters(in_area, sums) or '<div class="dq-muted">No checks match the filters.</div>')

st.write("")
simple.check_rows(in_area, sums, f"area_{key}")
simple.footer(ran_at, sums)
