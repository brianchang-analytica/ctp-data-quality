"""Overview: one glance at the health of every system and where to start."""

import streamlit as st

from dq.checks import checks_for_pair, checks_for_system
from dq.model import DIMENSION_ORDER, DIMENSIONS, SYSTEM_ORDER, SYSTEMS
from ui import checks_view, data, page
from ui.components import (
    esc,
    fmt_int,
    fmt_pct,
    glass,
    hero,
    kpi,
    md,
    meter_row,
    ring,
    section,
    status_pill,
    system_header,
    systems_chips,
    tip,
)

filters = page.filters()
sums, ran_at = page.load()
in_scope = page.scoped()

hero(
    "CTP Data Quality Center",
    "Is our submission and product data complete, consistent and on time?",
    "Every check below compares what eSUB, Rhapsody, PMS and the reporting warehouse say about the same "
    "submissions and products, and flags records that are missing, don't match, or are late. "
    "Hover any (?) for an explanation, and open a check to see exactly which records to fix.",
)

# ---------------------------------------------------------------- KPIs
score = data.weighted_score(in_scope, sums)
statuses = [sums[c.id].status(c) for c in in_scope if c.id in sums]
n_fail, n_warn, n_err = statuses.count("fail"), statuses.count("warn"), statuses.count("error")
flagged = sum(sums[c.id].failed for c in in_scope if c.id in sums and not sums[c.id].error)

st.write("")
k = st.columns(4)
tiles = [
    ("Overall health", fmt_pct(score), f"across {len(in_scope)} checks",
     "The average pass rate of all checks, where high-impact checks count three times as much as low-impact ones. "
     "98% or better is healthy; below 90% needs action.", data.score_status(score)),
    ("Action needed", fmt_int(n_fail), "checks below 90% pass",
     "Checks where more than 1 in 10 records has a problem (or a tighter limit for critical checks). Start here.",
     "fail" if n_fail else "pass"),
    ("Needs attention", fmt_int(n_warn), "checks between 90% and 98%",
     "Checks with a noticeable number of problems that should be worked down.", "warn" if n_warn else "pass"),
    ("Records to review", fmt_int(flagged), "flagged across all checks" + (f" · {n_err} could not run" if n_err else ""),
     "Total records flagged by all checks. One record can be flagged by more than one check, "
     "so this is a workload indicator, not a count of unique records.", None),
]
for col, (label, value, sub, help_text, status) in zip(k, tiles):
    with col:
        with glass(f"kpi_{label}"):
            md(kpi(label, value, sub, help_text, status))

# ---------------------------------------------------------------- Systems
section("Health by system", "How each system is doing on its own data and when compared with the others",
        "Each card scores every check that involves the system — its own internal checks and the ones that compare it "
        "with another system.")
cols = st.columns(4)
for col, key in zip(cols, SYSTEM_ORDER):
    sys = SYSTEMS[key]
    checks = page.scoped(checks_for_system(key))
    s_score = data.weighted_score(checks, sums)
    s_status = data.score_status(s_score)
    s_flagged = sum(sums[c.id].failed for c in checks if c.id in sums and not sums[c.id].error)
    worst = checks_view.sort_by_priority([c for c in checks if sums[c.id].failed or sums[c.id].error], sums)
    with col:
        with glass(f"sys_{key}", lift=True):
            md(system_header(key))
            md(
                f'<div class="dq-ring-wrap" style="margin:14px 0 6px">{ring(s_score, s_status)}'
                f'<div><div class="dq-ring-num">{fmt_pct(s_score)}</div>{status_pill(s_status)}</div></div>'
            )
            meters = ""
            for dim in DIMENSION_ORDER:
                dchecks = [c for c in checks if c.dimension == dim]
                d_score = data.weighted_score(dchecks, sums)
                meters += meter_row(dim, d_score, data.score_status(d_score),
                                    f"{DIMENSIONS[dim]['question']} {len(dchecks)} checks. {DIMENSIONS[dim]['explain']}")
            md(meters)
            top = (f'<div class="dq-muted" style="margin-top:8px">Biggest issue</div>'
                   f'<div class="dq-small" style="min-height:40px">{esc(worst[0].title)}</div>') if worst else (
                   '<div class="dq-muted" style="margin-top:8px">Biggest issue</div>'
                   '<div class="dq-small" style="min-height:40px">None — all checks pass</div>')
            md(top + f'<div class="dq-muted" style="margin:6px 0 10px">{len(checks)} checks · {fmt_int(s_flagged)} records flagged</div>')
            if st.button(f"Open {sys.name}", key=f"open_{key}", icon=":material/arrow_forward:", width="stretch"):
                st.session_state["sys_pick"] = key
                st.switch_page("v2/views/systems.py")

# ---------------------------------------------------------------- Pairs
pairs = []
for i, a in enumerate(SYSTEM_ORDER):
    for b in SYSTEM_ORDER[i + 1:]:
        pc = page.scoped(checks_for_pair(a, b))
        if pc:
            pairs.append((a, b, pc))

if pairs:
    section("Do the systems agree?", "Records that should be identical 1-to-1 across systems",
            "These checks line up the same submission or product in two systems and flag ones that are missing from "
            "one side or have different values.")
    per_row = 3
    for idx, (a, b, pc) in enumerate(pairs):
        if idx % per_row == 0:
            pcols = st.columns(per_row)
        p_score = data.weighted_score(pc, sums)
        p_status = data.score_status(p_score)
        mism = checks_view.sort_by_priority([c for c in pc if sums[c.id].failed], sums)[:3]
        with pcols[idx % per_row]:
            with glass(f"pair_{a}_{b}", lift=True):
                md(
                    f'<div style="display:flex;justify-content:space-between;align-items:center">{systems_chips((a, b))}'
                    f"{status_pill(p_status)}</div>"
                    f'<div style="display:flex;align-items:baseline;gap:8px;margin:10px 0 2px">'
                    f'<span class="dq-ring-num">{fmt_pct(p_score)}</span><span class="dq-muted">agreement'
                    f'{tip("Severity-weighted share of records that match across the two systems, over " + str(len(pc)) + " comparison checks.")}'
                    f"</span></div>"
                )
                rows = "".join(
                    f'<div class="dq-small" style="display:flex;justify-content:space-between;gap:10px;padding:5px 0;'
                    f'border-top:1px solid var(--hairline)"><span>{esc(c.title)}</span>'
                    f'<span style="font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:600">{fmt_int(sums[c.id].failed)}</span></div>'
                    for c in mism
                ) or '<div class="dq-small" style="padding:5px 0">No mismatches found.</div>'
                md(f'<div style="margin:8px 0 12px;min-height:138px">{rows}</div>')
                if st.button("Compare these systems", key=f"cmp_{a}_{b}", icon=":material/compare_arrows:", width="stretch"):
                    st.session_state["pair_pick"] = f"{a}|{b}"
                    st.switch_page("v2/views/compare.py")

# ---------------------------------------------------------------- Fix first
section("Fix these first", "The checks with the most serious problems right now",
        "Ranked by status (action needed first), then impact, then how many records are affected.")
problems = [c for c in in_scope if c.id in sums and (sums[c.id].failed or sums[c.id].error)]
top = checks_view.sort_by_priority(problems, sums)[:8]
if not top:
    with glass("allclear"):
        md(f'<div class="dq-title">{status_pill("pass")} Every check in scope passed.</div>')
for c in top:
    checks_view.check_row(c, sums[c.id], filters, "home")

page.footer(ran_at)
