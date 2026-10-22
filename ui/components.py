"""Small HTML building blocks (chips, tooltips, rings, meters) and glass containers."""

from __future__ import annotations

import html
import itertools

import streamlit as st

from dq.model import DIMENSIONS, SEVERITIES, STATUS_LABEL, SYSTEMS
from ui.theme import STATUS_COLORS

_counter = itertools.count()


def esc(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def md(markup: str) -> None:
    """Render HTML. Lines are stripped so Markdown never mistakes indentation for a code block."""
    st.markdown("".join(line.strip() for line in markup.splitlines()), unsafe_allow_html=True)


def glass(key: str, lift: bool = False):
    """A frosted-glass container that can hold any Streamlit elements."""
    prefix = "glasslift" if lift else "glass"
    return st.container(key=f"{prefix}_{key}")


def glass_row(key: str):
    return st.container(key=f"glassrow_{key}")


def tip(text: str, align: str = "center") -> str:
    """A small (?) that shows `text` on hover or keyboard focus."""
    cls = {"left": " dq-tip-left", "right": " dq-tip-right"}.get(align, "")
    return f'<span class="dq-tip{cls}" tabindex="0" role="note" aria-label="{esc(text)}" data-tip="{esc(text)}">?</span>'


# --------------------------------------------------------------------------- #
# Icons (inline SVG so nothing depends on an icon font loading)
# --------------------------------------------------------------------------- #

_ICON_PATHS = {
    "pass": '<circle cx="8" cy="8" r="7.25" fill="{c}"/><path d="M4.6 8.3l2.2 2.2 4.6-4.8" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "warn": '<path d="M8 1.2l7 12.6H1z" fill="{c}" stroke="{c}" stroke-linejoin="round" stroke-width="1.2"/><path d="M8 5.6v3.8" stroke="#3b2a00" stroke-width="1.7" stroke-linecap="round"/><circle cx="8" cy="11.6" r="1" fill="#3b2a00"/>',
    "fail": '<circle cx="8" cy="8" r="7.25" fill="{c}"/><path d="M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>',
    "none": '<circle cx="8" cy="8" r="6.5" fill="none" stroke="{c}" stroke-width="1.5"/><path d="M5 8h6" stroke="{c}" stroke-width="1.6" stroke-linecap="round"/>',
    "error": '<circle cx="8" cy="8" r="6.5" fill="none" stroke="{c}" stroke-width="1.5"/><path d="M8 4.6v4.2" stroke="{c}" stroke-width="1.7" stroke-linecap="round"/><circle cx="8" cy="11.2" r="1" fill="{c}"/>',
}


def status_icon(status: str, size: int = 16) -> str:
    path = _ICON_PATHS.get(status, _ICON_PATHS["none"]).format(c=STATUS_COLORS.get(status, "#8a8a85"))
    return f'<svg width="{size}" height="{size}" viewBox="0 0 16 16" aria-hidden="true">{path}</svg>'


def status_pill(status: str) -> str:
    return f'<span class="dq-status">{status_icon(status)}{esc(STATUS_LABEL.get(status, status))}</span>'


def system_chip(key: str) -> str:
    s = SYSTEMS[key]
    return f'<span class="dq-chip"><span class="dq-dot" style="background:{s.color}"></span>{esc(s.name)}</span>'


def systems_chips(keys) -> str:
    keys = list(keys)
    if len(keys) == 2:
        a, b = (SYSTEMS[k] for k in keys)
        return (
            f'<span class="dq-chip"><span class="dq-dot" style="background:{a.color}"></span>{esc(a.name)}'
            f'<span style="color:var(--ink-3)">&harr;</span>'
            f'<span class="dq-dot" style="background:{b.color}"></span>{esc(b.name)}</span>'
        )
    return "".join(system_chip(k) for k in keys)


def severity_chip(sev: str) -> str:
    return f'<span class="dq-chip dq-sev-{esc(sev)}" title="{esc(SEVERITIES[sev]["explain"])}">{esc(sev)} impact</span>'


def dimension_chip(dim: str) -> str:
    return f'<span class="dq-chip" title="{esc(DIMENSIONS[dim]["explain"])}">{esc(dim)}</span>'


def plain_chip(text: str) -> str:
    return f'<span class="dq-chip">{esc(text)}</span>'


def system_badge(key: str, size: int = 40) -> str:
    s = SYSTEMS[key]
    letters = {"esub": "eS", "rhapsody": "Rh", "pms": "PM", "edw": "Rp"}[key]
    return (
        f'<span class="dq-sys-badge" style="width:{size}px;height:{size}px;'
        f'background:linear-gradient(145deg,{s.color},{s.color}cc)">{letters}</span>'
    )


def system_header(key: str, extra_tip: str | None = None) -> str:
    s = SYSTEMS[key]
    return (
        f'<div class="dq-sys">{system_badge(key)}<div>'
        f'<div class="dq-sys-name">{esc(s.name)}{tip(extra_tip or s.blurb, "left")}</div>'
        f'<div class="dq-sys-long">{esc(s.long_name)}</div></div></div>'
    )


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #


def fmt_int(n) -> str:
    return "—" if n is None else f"{int(n):,}"


def fmt_pct(p, digits: int = 1) -> str:
    if p is None:
        return "—"
    v = p * 100
    if 99.95 <= v < 100 and digits == 1:
        return ">99.9%"
    if 0 < v < 0.05 and digits == 1:
        return "<0.1%"
    return f"{v:.{digits}f}%"


def ring(pct: float | None, status: str, size: int = 76, stroke: int = 8) -> str:
    """Circular score gauge. The number sits beside it so the value never depends on color."""
    r = (size - stroke) / 2
    circ = 2 * 3.14159265 * r
    frac = 0 if pct is None else max(0.0, min(1.0, pct))
    color = STATUS_COLORS.get(status, "#8a8a85")
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img" aria-label="Score {fmt_pct(pct)}">'
        f'<circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="rgba(21,22,26,.08)" stroke-width="{stroke}"/>'
        + (
            f'<circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="{color}" stroke-width="{stroke}" '
            f'stroke-linecap="round" stroke-dasharray="{circ * frac:.2f} {circ:.2f}" '
            f'transform="rotate(-90 {size/2} {size/2})"/>'
            if frac > 0 else ""
        )
        + "</svg>"
    )


def meter_row(label: str, pct: float | None, status: str, help_text: str) -> str:
    width = 0 if pct is None else max(2, pct * 100)
    return (
        f'<div class="dq-meter-row"><span>{esc(label)}{tip(help_text, "left")}</span>'
        f'<div class="dq-meter" role="img" aria-label="{esc(label)} {fmt_pct(pct)}">'
        f'<span style="width:{width:.1f}%;background:{STATUS_COLORS.get(status, "#8a8a85")}"></span></div>'
        f'<span class="dq-meter-val">{fmt_pct(pct)}</span></div>'
    )


def kpi(label: str, value: str, sub: str, help_text: str, status: str | None = None) -> str:
    icon = f"{status_icon(status, 18)}&nbsp;" if status else ""
    return (
        f'<div class="dq-kpi-label">{esc(label)}{tip(help_text, "left")}</div>'
        f'<div class="dq-kpi-value">{icon}{esc(value)}</div>'
        f'<div class="dq-kpi-sub">{esc(sub)}</div>'
    )


def section(title: str, sub: str = "", help_text: str | None = None) -> None:
    t = tip(help_text, "left") if help_text else ""
    md(f'<div class="dq-section"><h3>{esc(title)}</h3>{t}<span class="dq-section-sub">{esc(sub)}</span></div>')


def hero(eyebrow: str, title: str, sub: str) -> None:
    md(
        f'<div class="dq-eyebrow">{esc(eyebrow)}</div>'
        f'<div class="dq-hero-title">{esc(title)}</div>'
        f'<div class="dq-hero-sub">{esc(sub)}</div>'
    )


def banner(text: str) -> None:
    md(f'<div class="dq-banner">{status_icon("warn", 18)}<div>{text}</div></div>')


def unique_key(prefix: str) -> str:
    return f"{prefix}_{next(_counter)}"
