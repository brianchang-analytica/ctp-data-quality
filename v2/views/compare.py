"""Do systems agree? Side-by-side reconciliation of two systems."""

import streamlit as st

from dq.checks import checks_for_pair
from dq.model import SYSTEM_ORDER, SYSTEMS
from ui import checks_view, data, page
from ui.components import esc, fmt_int, fmt_pct, glass, hero, kpi, md, section
from ui.theme import STATUS_COLORS

filters = page.filters()
sums, ran_at = page.load()

hero(
    "Do systems agree?",
    "Records that should be identical 1-to-1 across systems",
    "Pick two systems. We line up the same submissions and products in both, then show what is missing from one "
    "side, which details disagree, and where one system is behind the other.",
)

pairs = []
for i, a in enumerate(SYSTEM_ORDER):
    for b in SYSTEM_ORDER[i + 1:]:
        if checks_for_pair(a, b):
            pairs.append(f"{a}|{b}")

if not pairs:
    st.info("No cross-system checks are defined yet.")
    st.stop()

wanted = st.session_state.pop("pair_pick", None)
if wanted in pairs:
    st.session_state["pair_choice"] = wanted
elif st.session_state.get("pair_choice") not in pairs:
    st.session_state["pair_choice"] = pairs[0]

choice = st.segmented_control(
    "Systems to compare",
    pairs,
    format_func=lambda p: " ↔ ".join(SYSTEMS[k].name for k in p.split("|")),
    key="pair_choice",
    label_visibility="collapsed",
) or pairs[0]
a, b = choice.split("|")
A, B = SYSTEMS[a], SYSTEMS[b]

checks = page.scoped(checks_for_pair(a, b))
presence = [c for c in checks if c.dimension == "Completeness"]
fields = [c for c in checks if c.dimension == "Consistency"]
timing = [c for c in checks if c.dimension == "Timeliness"]

st.write("")
k = st.columns(4)
for col, (label, group, help_text) in zip(k, [
    ("Overall agreement", checks, f"Severity-weighted share of records that line up between {A.name} and {B.name}."),
    ("Present in both", presence, f"Do the same submissions and products exist in both {A.name} and {B.name}?"),
    ("Details match", fields, "When a record exists in both systems, do its key details (dates, company, counts, names) agree?"),
    ("In step", timing, "Is one system lagging behind or out of order compared with the other?"),
]):
    sc = data.weighted_score(group, sums)
    with col:
        with glass(f"cmpkpi_{label}"):
            md(kpi(label, fmt_pct(sc), f"{len(group)} checks" if group else "no checks", help_text, data.score_status(sc) if group else None))

# ---------------------------------------------------------------- Presence
SEG_COLORS = ["#eb6834", "#1baf7a", "#6250d6", "#e87ba4"]  # categorical slots for the problem types
MATCH_COLOR = "#2a78d6"


def presence_bar(c) -> str:
    s = sums[c.id]
    if s.error or not s.checked:
        return ""
    try:
        bd = data.breakdown(c, filters, "issue_category")
    except Exception:
        bd = None
    segs = [("Matched", s.checked - s.failed, MATCH_COLOR)]
    if bd is not None and not bd.empty:
        other = s.failed
        for i, row in enumerate(bd.head(len(SEG_COLORS)).itertuples()):
            segs.append((str(row.bucket), int(row.failed), SEG_COLORS[i]))
            other -= int(row.failed)
        if other > 0:
            segs.append(("Other", other, "#8a8a85"))
    elif s.failed:
        segs.append(("Problem", s.failed, SEG_COLORS[0]))
    total = max(1, sum(n for _, n, _ in segs))
    bar = "".join(
        f'<span title="{esc(lbl)}: {fmt_int(n)} ({n / total:.1%})" style="flex:{max(n / total, 0.004):.4f};background:{color};'
        f'height:14px;border-radius:4px"></span>'
        for lbl, n, color in segs if n > 0
    )
    legend = "".join(
        f'<span class="dq-chip"><span class="dq-dot" style="background:{color}"></span>{esc(lbl)} · '
        f'<b style="font-variant-numeric:tabular-nums">{fmt_int(n)}</b></span>'
        for lbl, n, color in segs
    )
    return (f'<div style="display:flex;gap:2px;margin:6px 0 8px" role="img" aria-label="{esc(c.title)}">{bar}</div>'
            f"<div>{legend}</div>")


if presence:
    section("Present in both?", "Records found in one system but not the other",
            "Blue is records found in both systems. The other colors are records found on one side only.")
    for c in checks_view.sort_by_priority(checks_view.visible(presence, sums), sums):
        checks_view.check_row(c, sums[c.id], filters, f"cmp_p_{choice}")
        bar = presence_bar(c)
        if bar:
            md(f'<div style="margin:-2px 8px 14px 8px">{bar}</div>')

# ---------------------------------------------------------------- Fields
if fields:
    section("Do the details match?", "For records in both systems, the share whose details agree",
            "Each bar is one detail we compare. Shorter bars mean more records disagree.")
    rows = []
    for c in sorted(fields, key=lambda c: (sums[c.id].pass_rate if sums[c.id].pass_rate is not None else 2)):
        s = sums[c.id]
        st_ = s.status(c)
        pct = s.pass_rate
        width = 0 if pct is None else max(1.5, pct * 100)
        rows.append(
            f'<div style="display:grid;grid-template-columns:minmax(220px,2.4fr) 3fr 70px 110px;gap:12px;align-items:center;'
            f'padding:7px 2px;border-top:1px solid var(--hairline)">'
            f'<span class="dq-small" style="color:var(--ink)">{esc(c.title)}</span>'
            f'<div class="dq-meter" style="height:10px"><span style="width:{width:.1f}%;background:{STATUS_COLORS.get(st_, "#8a8a85")}"></span></div>'
            f'<span class="dq-meter-val">{fmt_pct(pct)}</span>'
            f'<span class="dq-muted" style="text-align:right;font-variant-numeric:tabular-nums">{fmt_int(s.failed)} differ</span></div>'
        )
    with glass(f"fields_{choice}"):
        md(f'<div class="dq-muted" style="display:grid;grid-template-columns:minmax(220px,2.4fr) 3fr 70px 110px;gap:12px">'
           f"<span>Detail compared</span><span>Records that agree</span><span style='text-align:right'>Agree</span>"
           f"<span style='text-align:right'>Disagree</span></div>" + "".join(rows))
    st.write("")
    checks_view.check_list(fields, sums, filters, f"cmp_f_{choice}")

# ---------------------------------------------------------------- Timing
if timing:
    section("In step with each other?", "Delays and out-of-order dates between the two systems")
    checks_view.check_list(timing, sums, filters, f"cmp_t_{choice}")

page.footer(ran_at)
