"""Synthetic results so the app can be previewed without a SQL warehouse.

Nothing here is real CTP data. Numbers are deterministic per check (seeded by
the check id) so the demo looks stable between reruns. Detail columns are
taken from the check's own SQL, so the demo tables have the real layout.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from functools import lru_cache

import pandas as pd

from dq.model import CONTRACT_COLUMNS, Check

_COMPANIES = [
    "Acme Tobacco Co.", "Blue Ridge Vapor LLC", "Northfield Cigar Inc.", "Pinecrest Products",
    "Summit Nicotine Labs", "Harbor Leaf Ltd.", "Redwood Smokeless Co.", "Atlas E-Liquids",
]
_TYPES = ["PMTA", "SE", "EX REQ", "MRTPA", "TPMF", "Meeting Request", "General Correspondence"]
# Rhapsody "Organization" (the applicant company); the contract column is named `office`.
_OFFICES = ["Acme Tobacco Co.", "Atlas E-Liquids", "Blue Ridge Vapor LLC", "Harbor Leaf Ltd.", "Northfield Cigar Inc."]
_CATEGORIES = ["ENDS (VAPES)", "Cigars", "Smokeless", "Cigarettes", "Pipe Tobacco", "Waterpipe"]
_PREFIX = {"PMTA": "PM", "SE": "SE", "EX REQ": "EX", "MRTPA": "MR", "TPMF": "MF"}


def _rng(*parts) -> random.Random:
    seed = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]
    return random.Random(int(seed, 16))


def _range(f) -> tuple[date, date]:
    end = f.end or date.today()
    start = f.start or end - timedelta(days=365 * 3)
    return start, max(start, end)


def _scale(check: Check, f) -> float:
    start, end = _range(f)
    frac = min(1.0, max(0.02, (end - start).days / (365 * 5)))
    if f.submission_types and "submission_type" in check.filters:
        frac *= max(0.1, len(f.submission_types) / len(_TYPES))
    if f.offices and "office" in check.filters:
        frac *= max(0.1, len(f.offices) / len(_OFFICES))
    return frac if "date" in check.filters else 1.0


def summary(check: Check, f) -> tuple[int, int]:
    r = _rng(check.id)
    base = {"Data feeds": r.randint(12, 40)}.get(check.entity, r.choice([2_400, 8_000, 15_000, 42_000, 120_000]))
    checked = max(1, int(base * _scale(check, f)))
    profile = r.random()
    if profile < 0.45:
        rate = r.uniform(0.0, 0.015)
    elif profile < 0.8:
        rate = r.uniform(0.02, 0.09)
    else:
        rate = r.uniform(0.1, 0.3)
    return checked, int(round(checked * rate))


@lru_cache(maxsize=512)
def _output_columns(check: Check) -> list[str]:
    import sqlglot
    from dq.settings import SETTINGS

    try:
        tree = sqlglot.parse_one(check.render_sql(SETTINGS.placeholders()), read="databricks")
        cols = list(tree.named_selects)
    except Exception:
        cols = list(CONTRACT_COLUMNS)
    return cols


def _fake_value(col: str, r: random.Random, d: date, stn: str, company: str):
    c = col.lower()
    if "stn" in c or "tracking" in c:
        return stn if r.random() > 0.15 else None
    if "date" in c or c.endswith(" on") or "received" in c or "submitted" in c or "refreshed" in c:
        return (d + timedelta(days=r.randint(-20, 20))).isoformat() if r.random() > 0.1 else None
    if "days" in c or "hours" in c or "count" in c or c.startswith("#") or "number of" in c:
        return r.randint(0, 120)
    if "company" in c or "applicant" in c or "firm" in c or "organization name" in c:
        return company if r.random() > 0.2 else company.upper().replace(".", "")
    if "category" in c:
        return r.choice(_CATEGORIES)
    if "type" in c:
        return r.choice(_TYPES)
    if "status" in c:
        return r.choice(["Received", "In Review", "Closed", "Accepted", "Refuse to Accept", None])
    if "product" in c and ("id" in c or "key" in c):
        return f"PRD-{r.randint(100000, 999999)}"
    if "bundle" in c:
        return f"BUNDLE-{r.randint(1000, 9999)}"
    if "name" in c:
        return r.choice(["Original Menthol 20ct", "Classic Tobacco Pod 5%", "Wintergreen Pouch 6mg", None])
    if "office" in c:
        return r.choice(_OFFICES)
    return r.choice(["Y", "N", None, "—"])


def detail(check: Check, f) -> pd.DataFrame:
    checked, failed = summary(check, f)
    n = min(failed, 400)
    r = _rng(check.id, "detail", f)
    start, end = _range(f)
    span = max(1, (end - start).days)
    cols = [c for c in _output_columns(check) if c != "is_issue"]
    rows = []
    for i in range(n):
        st = r.choice(_TYPES)
        d = start + timedelta(days=r.randint(0, span))
        stn = f"{_PREFIX.get(st, 'GC')}{r.randint(1, 9_999_999):07d}"
        company = r.choice(_COMPANIES)
        row = {}
        for col in cols:
            if col == "record_key":
                row[col] = stn if check.entity not in ("Data feeds",) else f"Feed {i + 1}"
            elif col == "record_date":
                row[col] = d
            elif col == "submission_type":
                row[col] = st if "submission_type" in check.filters else None
            elif col == "office":
                row[col] = r.choice(_OFFICES) if "office" in check.filters else None
            elif col == "issue_category":
                row[col] = r.choice(_demo_categories(check))
            elif col == "issue_reason":
                row[col] = f"[Demo] Example of: {check.title.lower()}."
            else:
                row[col] = _fake_value(col, r, d, stn, company)
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


@lru_cache(maxsize=512)
def _sql_categories(check: Check) -> tuple[str, ...]:
    """The literal labels the check's own SQL can put in issue_category."""
    import sqlglot
    from sqlglot import exp

    from dq.settings import SETTINGS

    try:
        tree = sqlglot.parse_one(check.render_sql(SETTINGS.placeholders()), read="databricks")
        for e in tree.expressions:
            if e.alias_or_name.lower() == "issue_category":
                vals = [lit.this for lit in e.find_all(exp.Literal) if lit.is_string and lit.this.strip()]
                return tuple(dict.fromkeys(v for v in vals if len(v) <= 60))[:6]
    except Exception:
        pass
    return ()


def _demo_categories(check: Check) -> list[str]:
    from dq.model import SYSTEMS

    found = _sql_categories(check)
    if found:
        return list(found)
    if check.is_cross_system and check.dimension == "Completeness":
        a, b = (SYSTEMS[s].name for s in check.systems)
        return [f"Only in {a}", f"Only in {b}"]
    return {
        "Completeness": ["Blank value", "Missing link"],
        "Consistency": ["Values differ", "Duplicate", "Unexpected format"],
        "Timeliness": ["Out of order", "Overdue", "Stale"],
    }[check.dimension]


def trend(check: Check, f, grain: str = "MONTH") -> pd.DataFrame:
    start, end = _range(f)
    periods = pd.period_range(start, end, freq={"WEEK": "W", "MONTH": "M", "QUARTER": "Q", "YEAR": "Y"}[grain])
    checked, failed = summary(check, f)
    r = _rng(check.id, "trend")
    base_rate = failed / max(1, checked)
    drift = r.uniform(-0.6, 0.9)
    rows = []
    for i, p in enumerate(periods):
        n = max(1, int(checked / max(1, len(periods)) * r.uniform(0.6, 1.4)))
        rate = max(0.0, base_rate * (1 + drift * (i / max(1, len(periods) - 1) - 0.5)) * r.uniform(0.6, 1.4))
        rows.append({"period": p.start_time.date(), "checked": n, "failed": int(n * min(rate, 1))})
    return pd.DataFrame(rows)


def breakdown(check: Check, f, by: str) -> pd.DataFrame:
    checked, failed = summary(check, f)
    r = _rng(check.id, by)
    buckets = {"issue_category": _demo_categories(check), "submission_type": _TYPES, "office": _OFFICES}[by]
    weights = [r.random() + 0.1 for _ in buckets]
    total = sum(weights)
    rows = []
    for b, w in zip(buckets, weights):
        fail = int(failed * w / total)
        chk = fail if by == "issue_category" else max(fail, int(checked * w / total))
        rows.append({"bucket": b, "checked": chk, "failed": fail})
    return pd.DataFrame(rows).sort_values("failed", ascending=False)
