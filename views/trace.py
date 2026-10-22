"""Trace a submission (MVP): one STN, the key details from every system side by side."""

import re

import pandas as pd
import streamlit as st

from dq import lookup
from dq.db import friendly_error
from dq.model import SYSTEM_ORDER, SYSTEMS
from dq.settings import SETTINGS
from ui import data
from ui.components import banner, esc, glass, md, status_icon

md('<div class="dq-hero-title">Trace a submission</div>'
   '<div class="dq-hero-sub">Enter an STN to compare its details across eSUB, Rhapsody, PMS and Reporting. '
   'Differences are highlighted.</div>')
st.write("")
if SETTINGS.demo_mode:
    banner("<b>Demo data.</b> Any STN returns sample values.")
md('<div class="dq-muted" style="margin-bottom:8px">Filters do not apply here. '
   "A trace shows every record for the STN.</div>")

with glass("trace_input"):
    with st.form("trace_form", border=False):
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        with c1:
            stn_in = st.text_input("STN", key="trace_stn", placeholder="e.g. PM0001234",
                                   help="Spaces and letter case are ignored.")
        with c2:
            go = st.form_submit_button("Trace", icon=":material/travel_explore:", type="primary", width="stretch")

stn = lookup.normalize_stn(stn_in)
if not stn:
    md('<div class="dq-muted" style="margin-top:12px">Tip: copy an STN from any check and paste it here.</div>')
    st.stop()
if not lookup.TRACE_QUERIES:
    st.info("No trace queries are configured yet.")
    st.stop()


@st.cache_data(ttl=SETTINGS.cache_minutes * 60, show_spinner=False)
def _trace(stn: str, ident: str, _token):
    out = {}
    for qid, res in lookup.run_trace(stn, _token).items():
        out[qid] = res if isinstance(res, pd.DataFrame) else friendly_error(res)
    return out


with st.spinner(f"Looking up {stn} in every system…"):
    results = _trace(stn, data.identity(), data.user_token())

queries = {q.id: q for q in lookup.TRACE_QUERIES}


def _norm(concept: str, v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip()):
        return None
    s = str(v).strip()
    if "date" in concept.lower():
        return s[:10]
    if concept.lower() in ("company",):
        # Same rule as the CO_NAME macro the checks use (dq/sqlfrag.py).
        s = s.upper().replace("&", " AND ").replace(".", "")
        s = re.sub(r"[^A-Z0-9 ]", " ", s)
        s = re.sub(r"\b(THE|INCORPORATED|INC|LLC|L L C|LIMITED|LTD|CORPORATION|CORP|COMPANY|CO|LP|LLP|PLC)\b", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    try:
        return str(int(float(s)))
    except ValueError:
        return re.sub(r"\s+", " ", s.upper())


# ---------------------------------------------------------------- comparison grid
concepts: list[str] = []
grid: dict[str, dict[str, object]] = {}
for q in lookup.TRACE_QUERIES:
    res = results.get(q.id)
    if not isinstance(res, pd.DataFrame) or res.empty:
        continue
    for concept, alias in q.compare:
        if alias not in res.columns:
            continue
        if concept not in concepts:
            concepts.append(concept)
        cell = grid.setdefault(concept, {})
        sysname = SYSTEMS[q.system].name
        if sysname not in cell:
            val = res[alias].dropna()
            cell[sysname] = val.iloc[0] if len(val) else None

ORDER = ["Received date", "Submitted date", "Company", "Submission type", "Status", "Product count"]
concepts.sort(key=lambda c: ORDER.index(c) if c in ORDER else len(ORDER))


def _verdict(concept: str, values: list) -> str:
    present = [v for v in values if _norm(concept, v) is not None]
    if len(present) < 2:
        return "Only in one system"
    if "date" in concept.lower():
        days = pd.to_datetime([_norm(concept, v) for v in present], errors="coerce").dropna()
        if len(days) == len(present):
            spread = (days.max() - days.min()).days
            if spread == 0:
                return "✓ Same"
            if spread <= 1:
                return "✓ Same (1 day apart, likely time zone)"
            return f"✗ Differs by {spread} days"
    return "✓ Same" if len({_norm(concept, v) for v in present}) == 1 else "✗ Differs"


if concepts:
    md(f'<div class="dq-section"><h3>{esc(stn)} across systems</h3><span class="dq-section-sub">Rows marked “Differs” need review</span></div>')
    sys_cols = [SYSTEMS[k].name for k in SYSTEM_ORDER if any(SYSTEMS[k].name in grid[c] for c in concepts)]
    rows = []
    for concept in concepts:
        vals = {s: grid[concept].get(s) for s in sys_cols}
        verdict = _verdict(concept, list(vals.values()))
        rows.append({"Detail": concept, **{s: ("" if v is None else str(v)) for s, v in vals.items()}, "Agrees?": verdict})
    df = pd.DataFrame(rows)

    def _hl(row):
        bad = row["Agrees?"].startswith("✗")
        return ["background-color: rgba(208,59,59,0.10); font-weight:600" if bad else "" for _ in row]

    with glass("trace_grid"):
        st.dataframe(df.style.apply(_hl, axis=1), hide_index=True, width="stretch")

# ---------------------------------------------------------------- per system
md('<div class="dq-section"><h3>Details by system</h3></div>')
for key in SYSTEM_ORDER:
    qs = [q for q in lookup.TRACE_QUERIES if q.system == key]
    if not qs:
        continue
    found = any(isinstance(results.get(q.id), pd.DataFrame) and not results[q.id].empty for q in qs)
    label = f"{SYSTEMS[key].name}: {'found' if found else 'not found'}"
    with st.expander(label, icon=":material/check_circle:" if found else ":material/help:"):
        for q in qs:
            res = results.get(q.id)
            md(f'<div class="dq-title" style="margin-top:8px">{esc(q.title)}</div>')
            if isinstance(res, str):
                st.error(res)
            elif res is None or res.empty:
                md('<div class="dq-muted">None found.</div>')
            elif q.layout == "record":
                rec = res.iloc[0]
                kv = pd.DataFrame({"Field": rec.index, "Value": ["" if pd.isna(v) else str(v) for v in rec.values]})
                st.dataframe(kv, hide_index=True, width="stretch")
                if len(res) > 1:
                    md(f'<div class="dq-muted">{len(res)} matching records. Showing the first. More than one record can itself be a problem.</div>')
            else:
                st.dataframe(res, hide_index=True, width="stretch")
