"""Liquid-glass styling layered on top of Streamlit.

Glass panels are Streamlit containers whose key starts with "glass" (Streamlit
adds a `st-key-<key>` class), so every panel is a real container that can hold
widgets, not just decorative HTML. No external fonts or CDNs are loaded, so the
app works inside a locked-down workspace network.
"""

from __future__ import annotations

import streamlit as st

STATUS_COLORS = {"pass": "#0ca30c", "warn": "#fab219", "fail": "#d03b3b", "none": "#8a8a85", "error": "#8a8a85"}

_CSS = """
<style>
:root {
  --ink: #15161a;
  --ink-2: #4a4d57;
  --ink-3: #767a86;
  --hairline: rgba(21, 22, 26, 0.08);
  --glass: rgba(255, 255, 255, 0.56);
  --glass-strong: rgba(255, 255, 255, 0.78);
  --glass-edge: rgba(255, 255, 255, 0.85);
  --shadow: 0 12px 32px -14px rgba(28, 40, 92, 0.28), 0 2px 6px -2px rgba(28, 40, 92, 0.08);
  --shadow-lift: 0 22px 44px -18px rgba(28, 40, 92, 0.36), 0 4px 10px -4px rgba(28, 40, 92, 0.10);
  --radius: 22px;
  --good: #0ca30c; --warn: #fab219; --crit: #d03b3b; --muted: #8a8a85;
  --esub: #2a78d6; --rhapsody: #eb6834; --pms: #1baf7a; --edw: #e87ba4;
  --accent: #2a78d6;
}

/* ---------- canvas ---------- */
.stApp {
  background:
    radial-gradient(1100px 620px at 8% -8%, rgba(42, 120, 214, 0.20), transparent 62%),
    radial-gradient(900px 560px at 98% 4%, rgba(232, 123, 164, 0.16), transparent 60%),
    radial-gradient(900px 700px at 60% 108%, rgba(27, 175, 122, 0.16), transparent 62%),
    radial-gradient(700px 500px at 0% 100%, rgba(235, 104, 52, 0.10), transparent 60%),
    linear-gradient(180deg, #f5f7fc 0%, #eef1f8 100%);
  background-attachment: fixed;
  color: var(--ink);
}
html, body, .stApp, button, input, textarea, select {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;
}
[data-testid="stHeader"] { background: rgba(245, 247, 252, 0.55); backdrop-filter: blur(18px) saturate(160%); -webkit-backdrop-filter: blur(18px) saturate(160%); }
.block-container { padding-top: 4.2rem; padding-bottom: 4rem; max-width: 1380px; }
h1, h2, h3 { letter-spacing: -0.02em; color: var(--ink); }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] {
  background: rgba(255, 255, 255, 0.42);
  backdrop-filter: blur(28px) saturate(170%);
  -webkit-backdrop-filter: blur(28px) saturate(170%);
  border-right: 1px solid var(--glass-edge);
}
[data-testid="stSidebar"] [data-testid="stSidebarContent"] { background: transparent; }

/* ---------- glass panels ---------- */
div[class*="st-key-glass"] {
  position: relative;
  background: var(--glass);
  backdrop-filter: blur(22px) saturate(180%);
  -webkit-backdrop-filter: blur(22px) saturate(180%);
  border: 1px solid var(--glass-edge);
  border-radius: var(--radius);
  box-shadow: var(--shadow), inset 0 1px 0 rgba(255, 255, 255, 0.9);
  padding: 18px 20px 16px 20px;
  transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
div[class*="st-key-glass"]::before {
  content: "";
  position: absolute; inset: 0;
  border-radius: inherit;
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.55) 0%, rgba(255, 255, 255, 0.0) 42%);
  pointer-events: none;
}
/* Streamlit pulls text blocks up by 1rem at their bottom; inside a glass panel that eats the
   panel's bottom padding. Neutralize it so every panel has even padding on all sides. */
div[class*="st-key-glass"] [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
div[class*="st-key-glass"] [data-testid="stMarkdownContainer"] > :last-child,
div[class*="st-key-glass"] [data-testid="stMarkdownContainer"] p:last-child { margin-bottom: 0 !important; }
div[class*="st-key-glass"]:not([class*="st-key-glassrow"]) { padding-bottom: 20px; }
div[class*="st-key-glasslift"]:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lift), inset 0 1px 0 rgba(255, 255, 255, 0.95);
  background: var(--glass-strong);
}
div[class*="st-key-glass"]:hover, div[class*="st-key-glass"]:focus-within { z-index: 20; }
div[class*="st-key-glassrow"] { padding: 12px 16px 10px 18px; border-radius: 18px; }

/* ---------- typography helpers ---------- */
.dq-eyebrow { font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3); }
.dq-hero-title { font-size: 34px; font-weight: 700; letter-spacing: -0.03em; line-height: 1.1; margin: 2px 0 6px; }
.dq-hero-sub { font-size: 15.5px; color: var(--ink-2); max-width: 760px; line-height: 1.5; }
.dq-section { display: flex; align-items: baseline; gap: 10px; margin: 26px 0 10px; }
.dq-section h3 { margin: 0; font-size: 20px; font-weight: 650; }
.dq-section .dq-section-sub { color: var(--ink-3); font-size: 13.5px; }
.dq-muted { color: var(--ink-3); font-size: 13px; }
.dq-small { font-size: 13px; color: var(--ink-2); line-height: 1.45; }
.dq-title { font-size: 15.5px; font-weight: 600; color: var(--ink); line-height: 1.35; }

/* ---------- KPI tiles ---------- */
.dq-kpi-label { font-size: 13px; font-weight: 550; color: var(--ink-2); display: flex; align-items: center; gap: 4px; }
.dq-kpi-value { font-size: 34px; font-weight: 700; letter-spacing: -0.03em; line-height: 1.15; margin-top: 4px; font-variant-numeric: tabular-nums; }
.dq-kpi-sub { font-size: 12.5px; color: var(--ink-3); margin-top: 2px; }

/* ---------- chips & status ---------- */
.dq-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 550; color: var(--ink-2);
  background: rgba(255, 255, 255, 0.7); border: 1px solid var(--hairline);
  white-space: nowrap; margin: 0 4px 4px 0;
}
.dq-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.dq-status { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; color: var(--ink); white-space: nowrap; }
.dq-status svg { flex: none; }
.dq-sev-High { border-color: rgba(208, 59, 59, 0.28); }
.dq-sev-Medium { border-color: rgba(250, 178, 25, 0.38); }

/* ---------- meters & rings ---------- */
.dq-meter-row { display: grid; grid-template-columns: 112px 1fr 52px; align-items: center; gap: 10px; margin: 7px 0; font-size: 13px; color: var(--ink-2); }
.dq-meter { height: 8px; border-radius: 999px; background: rgba(21, 22, 26, 0.07); overflow: hidden; }
.dq-meter > span { display: block; height: 100%; border-radius: 999px; }
.dq-meter-val { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; color: var(--ink); }
.dq-ring-wrap { display: flex; align-items: center; gap: 16px; }
.dq-ring-num { font-size: 26px; font-weight: 700; letter-spacing: -0.03em; font-variant-numeric: tabular-nums; }

/* ---------- system header ---------- */
.dq-sys { display: flex; align-items: center; gap: 12px; }
.dq-sys-badge { width: 40px; height: 40px; border-radius: 13px; display: grid; place-items: center; color: #fff; font-weight: 700; font-size: 15px; box-shadow: inset 0 1px 0 rgba(255,255,255,.45), 0 6px 14px -6px rgba(0,0,0,.35); }
.dq-sys-name { font-size: 18px; font-weight: 650; letter-spacing: -0.01em; }
.dq-sys-long { font-size: 12.5px; color: var(--ink-3); }

/* ---------- tooltips ---------- */
.dq-tip {
  position: relative; display: inline-grid; place-items: center;
  width: 17px; height: 17px; border-radius: 50%;
  font-size: 11px; font-weight: 700; color: var(--ink-2);
  background: rgba(255, 255, 255, 0.85); border: 1px solid rgba(21, 22, 26, 0.14);
  cursor: help; margin-left: 4px; vertical-align: middle; outline: none; flex: none;
}
.dq-tip:hover, .dq-tip:focus-visible { color: #fff; background: var(--accent); border-color: var(--accent); }
.dq-tip::after {
  content: attr(data-tip);
  position: absolute; left: 50%; bottom: calc(100% + 9px);
  transform: translateX(-50%) translateY(4px);
  width: max-content; max-width: 320px; white-space: pre-line; text-align: left;
  padding: 10px 12px; border-radius: 12px;
  background: rgba(22, 24, 30, 0.92); color: #f4f5f7;
  font-size: 12.5px; line-height: 1.5; font-weight: 400; letter-spacing: 0;
  box-shadow: 0 14px 30px -12px rgba(0, 0, 0, 0.45);
  opacity: 0; visibility: hidden; transition: opacity 0.14s ease, transform 0.14s ease;
  z-index: 1000; pointer-events: none;
}
.dq-tip.dq-tip-left::after { left: 0; transform: translateY(4px); }
.dq-tip.dq-tip-right::after { left: auto; right: 0; transform: translateY(4px); }
.dq-tip:hover::after, .dq-tip:focus-visible::after { opacity: 1; visibility: visible; transform: translateX(-50%) translateY(0); }
.dq-tip.dq-tip-left:hover::after, .dq-tip.dq-tip-left:focus-visible::after,
.dq-tip.dq-tip-right:hover::after, .dq-tip.dq-tip-right:focus-visible::after { transform: translateY(0); }

/* ---------- banners ---------- */
.dq-banner {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 10px 14px; border-radius: 14px; font-size: 13.5px; line-height: 1.45;
  background: rgba(250, 178, 25, 0.14); border: 1px solid rgba(250, 178, 25, 0.45); color: #5a4300;
  margin-bottom: 12px;
}

/* ---------- Streamlit widgets ---------- */
.stButton > button, .stDownloadButton > button, [data-testid="stPopover"] button, .stPageLink a {
  border-radius: 999px !important;
  border: 1px solid rgba(255, 255, 255, 0.9) !important;
  background: rgba(255, 255, 255, 0.66) !important;
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  box-shadow: 0 4px 12px -6px rgba(28, 40, 92, 0.25), inset 0 1px 0 rgba(255,255,255,.9);
  color: var(--ink) !important; font-weight: 550;
  transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover, [data-testid="stPopover"] button:hover {
  background: rgba(255, 255, 255, 0.92) !important; transform: translateY(-1px);
  box-shadow: 0 8px 18px -8px rgba(28, 40, 92, 0.35), inset 0 1px 0 rgba(255,255,255,.95);
}
.stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
  background: linear-gradient(180deg, #3a88e6 0%, #2a78d6 100%) !important; color: #fff !important;
  border: 1px solid rgba(255,255,255,.35) !important;
}
.stButton > button[kind="tertiary"] { background: transparent !important; box-shadow: none; border-color: transparent !important; }

[data-testid="stButtonGroup"] button { border-radius: 999px !important; }
[data-testid="stButtonGroup"] button[kind$="Active"] {
  background: rgba(42,120,214,.12) !important; border-color: rgba(42,120,214,.55) !important; color: #1c5cab !important;
  box-shadow: 0 4px 12px -6px rgba(42,120,214,.45); }
[data-testid="stButtonGroup"] button[kind]:not([kind$="Active"]) { background: rgba(255,255,255,.62) !important; border-color: rgba(21,22,26,.10) !important; }
div[class*="st-key-sys_choice"] button, div[class*="st-key-pair_choice"] button, div[class*="st-key-area_choice"] button { padding: 8px 20px !important; min-height: 42px; }
div[class*="st-key-sys_choice"] button p, div[class*="st-key-pair_choice"] button p, div[class*="st-key-area_choice"] button p { font-size: 15px !important; font-weight: 600; }
[data-baseweb="tab-list"] { gap: 6px; background: rgba(255,255,255,.45); padding: 4px; border-radius: 999px; border: 1px solid var(--glass-edge); width: fit-content; }
[data-baseweb="tab"] { border-radius: 999px !important; padding: 6px 14px !important; height: auto !important; }
[data-baseweb="tab"][aria-selected="true"] { background: rgba(255,255,255,.95); box-shadow: 0 2px 8px -3px rgba(28,40,92,.3); }
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display: none; }

[data-testid="stDataFrame"], [data-testid="stTable"] { border-radius: 16px; overflow: hidden; border: 1px solid var(--hairline); background: rgba(255,255,255,.75); }
[data-testid="stExpander"] details { border-radius: 16px; background: rgba(255,255,255,.5); border: 1px solid var(--glass-edge); }
[data-testid="stMetric"] { background: transparent; }
div[data-baseweb="select"] > div, .stDateInput input, .stTextInput input { border-radius: 12px !important; background: rgba(255,255,255,.75) !important; }
div[role="dialog"] { border-radius: 24px !important; background: rgba(250, 251, 254, 0.92) !important; backdrop-filter: blur(30px) saturate(170%); -webkit-backdrop-filter: blur(30px) saturate(170%); }

/* top navigation */
[data-testid="stTopNavLinkContainer"] a, header nav a { border-radius: 999px !important; }

/* ---------- responsive card grids: wrap instead of squeezing text ---------- */
/* Overview area cards keep a readable width; extra cards move to the next line. */
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-glasslift_area"]) {
  display: grid !important;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
}
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] div[class*="st-key-glasslift_area"]) > [data-testid="stColumn"] {
  width: auto !important; min-width: 0; flex: none !important;
}
[data-testid="stColumn"] > div > div[class*="st-key-glasslift_area"],
[data-testid="stColumn"] div[class*="st-key-glasslift_area"] { height: 100%; }
/* Columns inside any glass panel (e.g. ring + meters) get a sensible minimum too. */
div[class*="st-key-glass"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width: 220px; }
/* Check rows: the title keeps most of the width; count and button wrap below on narrow screens. */
div[class*="st-key-glassrow"] [data-testid="stHorizontalBlock"] { row-gap: 10px; }
div[class*="st-key-glassrow"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(3) { margin-left: auto; }
div[class*="st-key-glassrow"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(1) { min-width: 280px; }
div[class*="st-key-glassrow"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) { min-width: 150px; }
div[class*="st-key-glassrow"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(3) { min-width: 120px; flex-grow: 0; }

/* ---------- check list: one panel, a labeled header, aligned columns ---------- */
div[class*="st-key-glass_list"] { gap: 0 !important; padding: 6px 12px 10px 12px !important; }
div[class*="st-key-dqhead"], div[class*="st-key-dqrow"] { padding: 0 8px; }
div[class*="st-key-dqrow"] { position: relative; padding: 14px 8px; border-radius: 12px; transition: background 0.12s ease; }
div[class*="st-key-dqrow"]::before { content: ""; position: absolute; left: 8px; right: 8px; top: 0; height: 1px; background: var(--hairline); }
div[class*="st-key-dqrow"]:hover { background: rgba(255, 255, 255, 0.72); }
div[class*="st-key-dqhead"] [data-testid="stHorizontalBlock"],
div[class*="st-key-dqrow"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 14px !important; }
div[class*="st-key-dqhead"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child,
div[class*="st-key-dqrow"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child { flex: 1 1 0 !important; width: auto !important; min-width: 0 !important; }
div[class*="st-key-dqhead"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child,
div[class*="st-key-dqrow"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child { flex: 0 0 84px !important; width: 84px !important; min-width: 84px !important; }
div[class*="st-key-dqrow"] .stButton > button { min-height: 34px; padding: 4px 14px; }

.dq-cq { container-type: inline-size; }
.dq-grid {
  display: grid; align-items: center; column-gap: 24px;
  grid-template-columns: minmax(220px, 1fr) 116px 84px minmax(168px, 0.3fr);
  font-size: 14px; color: var(--ink-2); line-height: 1.4;
}
.dq-head { padding: 10px 0 6px; font-size: 11.5px; font-weight: 650; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); }
.dq-head > span { display: inline-flex; align-items: center; white-space: nowrap; }
.dq-head > span:first-child { padding-left: 28px; }
.dq-c-check { display: flex; gap: 10px; align-items: flex-start; min-width: 0; }
.dq-c-icon { flex: none; display: inline-flex; margin-top: 1px; }
.dq-c-title { font-size: 15px; font-weight: 600; color: var(--ink); line-height: 1.35; overflow-wrap: break-word; }
.dq-note { font-size: 12.5px; color: var(--ink-3); margin-top: 2px; }
.dq-c-imp { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.dq-narrow { display: none; }
.dq-num { color: var(--ink); font-variant-numeric: tabular-nums; white-space: nowrap; }
.dq-num b { font-weight: 650; }
.dq-quiet { color: var(--ink-3); }
.dq-pct { margin-left: 8px; font-size: 12.5px; color: var(--ink-3); }
.dq-group { display: flex; align-items: center; gap: 8px; padding: 20px 8px 8px; font-size: 13.5px; font-weight: 650; color: var(--ink); }
.dq-group span { font-size: 12px; font-weight: 600; color: var(--ink-2); background: rgba(21, 22, 26, 0.06); border-radius: 999px; padding: 1px 8px; }
/* ---------- rule list ---------- */
.dq-group-page { padding: 22px 2px 8px; }
.dq-def { display: grid; grid-template-columns: 140px minmax(0, 1fr); gap: 10px 20px; font-size: 14px; line-height: 1.55; color: var(--ink); padding: 4px 2px 6px; }
.dq-def-k { color: var(--ink-3); font-size: 13px; font-weight: 600; padding-top: 1px; }
.dq-def ul { margin: 0 0 0 18px; padding: 0; }
.dq-def li { margin: 0 0 2px; }
.dq-def-sys { display: inline-flex; align-items: center; gap: 6px; margin-right: 14px; white-space: nowrap; }
.dq-def-id { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12.5px; color: var(--ink-3); }
@media (max-width: 640px) {
  .dq-def { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .dq-def-k { padding-top: 10px; }
}
/* Narrow: the title takes the full width and the details wrap beneath it, with no header. */
@container (max-width: 720px) {
  .dq-grid { display: flex; flex-wrap: wrap; gap: 4px 16px; padding-left: 28px; }
  .dq-grid > .dq-c-check { flex: 1 1 100%; margin: 0 0 2px -28px; }
  .dq-grid > .dq-c-rec { flex: 1 1 100%; }
  .dq-head { display: none; }
  .dq-narrow { display: inline; }
}
/* Text never breaks mid-word inside cards; long words wrap at word boundaries only. */
div[class*="st-key-glass"] .dq-title, div[class*="st-key-glass"] .dq-small, div[class*="st-key-glass"] .dq-muted,
div[class*="st-key-glass"] .dq-sys-name { overflow-wrap: break-word; word-break: normal; hyphens: manual; }

@media (max-width: 1280px) {
  .block-container { padding-left: 2rem; padding-right: 2rem; }
}
@media (max-width: 900px) {
  div[class*="st-key-glass"] { padding: 14px 16px 12px 16px; }
  div[class*="st-key-glassrow"] { padding: 10px 12px 8px 14px; }
  .dq-hero-title { font-size: 28px; }
}

@media (prefers-reduced-motion: reduce) {
  div[class*="st-key-glass"], .stButton > button, .dq-tip::after { transition: none !important; }
  div[class*="st-key-glasslift"]:hover { transform: none; }
}
@media (max-width: 640px) {
  .dq-hero-title { font-size: 26px; }
  .block-container { padding-left: 16px; padding-right: 16px; }
}
</style>
"""


def inject_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
