"""Overview: one card per system plus the data feeds. Each card leads to that area's checks."""

from pathlib import Path

import streamlit as st

from dq.checks.core import AREA_ORDER, AREAS, areas_of
from ui import simple
from ui.components import esc, glass, md

md('<div class="dq-hero-title">CTP Data Quality</div>'
   '<div class="dq-hero-sub">Pick a system to see its checks and the records to fix.</div>')
st.write("")

checks, sums, ran_at = simple.load()

cols = st.columns(len(AREA_ORDER))
for col, key in zip(cols, AREA_ORDER):
    area = AREAS[key]
    in_area = [c for c in checks if key in areas_of(c)]
    flagged = [c for c in in_area if simple.needs_attention(c, sums[c.id])]
    with col:
        with glass(f"area_{key}", lift=True):
            md(
                f'<div style="display:flex;align-items:center;gap:8px"><span class="dq-dot" style="width:10px;height:10px;background:{area.color}"></span>'
                f'<span class="dq-sys-name">{esc(area.name)}</span></div>'
                f'<div class="dq-muted" style="min-height:40px;margin:4px 0 16px">{esc(area.blurb)}</div>'
                + simple.area_ring(in_area, sums, 72)
                + f'<div class="dq-small" style="margin:14px 0 14px">{len(flagged)} of {len(in_area)} checks need attention</div>'
            )
            if st.button("Review", key=f"go_{key}", icon=":material/arrow_forward:", width="stretch"):
                st.session_state["area"] = key
                st.switch_page(str(Path(__file__).resolve().with_name("checks.py")))

if not checks:
    simple.no_results_note()
simple.footer(ran_at, sums)
