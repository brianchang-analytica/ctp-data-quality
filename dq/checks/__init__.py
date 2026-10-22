"""Registry of every data-quality check, grouped by the system(s) it covers.

Each module in GROUP_MODULES exposes `CHECKS: list[Check]`. To add a check,
append it to the right module; it appears in the app automatically.

ALL_CHECKS / CHECKS_BY_ID are built on first access, so importing one module
(e.g. to validate it) does not import the others.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module

from dq.model import SYSTEM_ORDER, Check

GROUP_MODULES = [
    "esub",  # inside eSUB
    "rhapsody",  # inside Rhapsody
    "pms",  # inside PMS
    "esub_rhapsody",  # eSUB  <-> Rhapsody
    "rhapsody_pms",  # Rhapsody <-> PMS
    "reporting",  # Rhapsody / PMS / eSUB <-> Reporting warehouse, and data feed freshness
]


@lru_cache(maxsize=1)
def load_checks() -> tuple[Check, ...]:
    checks: list[Check] = []
    seen: dict[str, str] = {}
    for name in GROUP_MODULES:
        module = import_module(f"dq.checks.{name}")
        for check in module.CHECKS:
            if check.id in seen:
                raise ValueError(f"Duplicate check id {check.id} in {name} and {seen[check.id]}")
            seen[check.id] = name
            checks.append(check)
    order = {s: i for i, s in enumerate(SYSTEM_ORDER)}
    checks.sort(key=lambda c: (order[c.home_system], c.is_cross_system, c.dimension, c.id))
    return tuple(checks)


def __getattr__(name: str):
    if name == "ALL_CHECKS":
        return list(load_checks())
    if name == "CHECKS_BY_ID":
        return {c.id: c for c in load_checks()}
    raise AttributeError(name)


def checks_for_system(system: str) -> list[Check]:
    """Every check that involves this system, internal or cross-system."""
    return [c for c in load_checks() if system in c.systems]


def checks_for_pair(a: str, b: str) -> list[Check]:
    return [c for c in load_checks() if set(c.systems) == {a, b}]
