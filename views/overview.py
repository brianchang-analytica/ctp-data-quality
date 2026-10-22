"""Overview: how each area is doing, and where to start."""

from pathlib import Path

import streamlit as st

from dq.checks.core import AREA_ORDER, AREAS, area_of
from ui import simple
from ui.checks_view import sort_by_priority
from ui.components import esc, glass, md, status_pill

md('<div class="dq-hero-title">CTP Data Quality</div>'
   '<div class="dq-hero-sub">Data problems found in eSUB, Rhapsody, PMS and Reporting.</div>')
st.write("")

checks, sums, ran_at = simple.load()

cols = st.columns(len(AREA_ORDER))
for col, key in zip(cols, AREA_ORDER):
    area = AREAS[key]
    in_area = [c for c in checks if area_of(c) == key]
    flagged = [c for c in in_area if simple.needs_attention(c, sums[c.id])]
    with col:
        with glass(f"area_{key}", lift=True):
            md(
                f'<div style="display:flex;align-items:center;gap:8px"><span class="dq-dot" style="width:10px;height:10px;background:{area.color}"></span>'
                f'<span class="dq-sys-name">{esc(area.name)}</span></div>'
                f'<div class="dq-muted" style="min-height:40px;margin:4px 0 12px">{esc(area.blurb)}</div>'
                + simple.area_ring(in_area, sums, 64)
                + f'<div class="dq-small" style="margin:10px 0 12px">{len(flagged)} of {len(in_area)} checks need attention</div>'
            )
            if st.button("Review", key=f"go_{key}", icon=":material/arrow_forward:", width="stretch"):
                st.session_state["area"] = key
                st.switch_page(str(Path(__file__).resolve().with_name("checks.py")))

md('<div class="dq-section"><h3>Start here</h3><span class="dq-section-sub">Most serious first</span></div>')
top = [c for c in checks if simple.needs_attention(c, sums[c.id])]
if not checks:
    simple.no_results_note()
elif not top:
    md(f'<div class="dq-title">{status_pill("pass")} Every check passed.</div>')
for c in sort_by_priority(top, sums)[:5]:
    simple.check_row(c, sums[c.id], "home")

simple.footer(ran_at, sums)
