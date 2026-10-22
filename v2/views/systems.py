"""By system: everything we check about one system, grouped by kind of problem."""

import streamlit as st

from dq.checks import checks_for_system
from dq.model import DIMENSION_ORDER, DIMENSIONS, ENTITIES, SYSTEM_ORDER, SYSTEMS
from ui import checks_view, data, page, sidebar
from ui.components import esc, fmt_int, fmt_pct, glass, hero, md, meter_row, ring, section, status_pill, system_header, tip

filters = page.filters()
sums, ran_at = page.load()

hero("By system", "Pick a system to see its data-quality checks",
     "Checks that compare a system with another one are included too, marked with ↔.")

default = st.session_state.pop("sys_pick", None) or st.session_state.get("sys_choice") or "esub"
if st.session_state.get("sys_choice") != default:
    st.session_state["sys_choice"] = default
key = st.segmented_control(
    "System",
    SYSTEM_ORDER,
    format_func=lambda k: SYSTEMS[k].name,
    key="sys_choice",
    label_visibility="collapsed",
) or "esub"
sys = SYSTEMS[key]

checks_all = checks_for_system(key)
checks = page.scoped(checks_all)
score = data.weighted_score(checks, sums)
status = data.score_status(score)
flagged = sum(sums[c.id].failed for c in checks if not sums[c.id].error)
internal = [c for c in checks if not c.is_cross_system]
cross = [c for c in checks if c.is_cross_system]

st.write("")
with glass(f"syshead_{key}"):
    c1, c2, c3 = st.columns([2.2, 1.6, 2.4], vertical_alignment="center")
    with c1:
        md(system_header(key) + f'<div class="dq-small" style="margin-top:10px">{esc(sys.blurb)}</div>')
    with c2:
        md(
            f'<div class="dq-ring-wrap">{ring(score, status, 92, 9)}<div>'
            f'<div class="dq-ring-num">{fmt_pct(score)}</div>{status_pill(status)}'
            f'<div class="dq-muted" style="margin-top:4px">{len(internal)} own · {len(cross)} shared checks'
            f'{tip("Own checks look only at this system. Shared checks compare it with another system.", "right")}</div>'
            f'<div class="dq-muted">{fmt_int(flagged)} records flagged</div></div></div>'
        )
    with c3:
        meters = ""
        for dim in DIMENSION_ORDER:
            d = [c for c in checks if c.dimension == dim]
            d_score = data.weighted_score(d, sums)
            meters += meter_row(dim, d_score, data.score_status(d_score),
                                f"{DIMENSIONS[dim]['question']} {DIMENSIONS[dim]['explain']} ({len(d)} checks)")
        md(meters)

# Business-area filter, specific to this system
areas = [e for e in ENTITIES if any(c.entity == e for c in checks_all)]
picked = st.pills(
    "Business area",
    areas,
    selection_mode="multi",
    default=areas,
    key=f"areas_{key}",
    help="\n".join(f"{e}: {ENTITIES[e]}" for e in areas),
) or areas
checks = [c for c in checks if c.entity in picked]

prefs = sidebar.view_prefs()
dims = [d for d in DIMENSION_ORDER if d in prefs["dims"]]
if not dims:
    st.info("Choose at least one kind of problem in the sidebar.")
else:
    labels = []
    for d in dims:
        dc = [c for c in checks if c.dimension == d]
        bad = sum(1 for c in dc if sums[c.id].status(c) in ("fail", "warn", "error"))
        labels.append(f"{d}  ·  {bad} of {len(dc)} need a look" if dc else f"{d}  ·  none")
    for tab, d in zip(st.tabs(labels), dims):
        with tab:
            md(f'<div class="dq-small" style="margin:4px 0 10px"><b>{esc(DIMENSIONS[d]["question"])}</b> {esc(DIMENSIONS[d]["explain"])}</div>')
            checks_view.check_list([c for c in checks if c.dimension == d], sums, filters, f"sys_{key}_{d}",
                                   empty_text=f"No {d.lower()} checks for {sys.name} match the current filters.")

page.footer(ran_at)
