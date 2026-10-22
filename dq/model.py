"""Business vocabulary and the Check contract.

Stewards think in *systems* (eSUB, Rhapsody, PMS, Reporting) and *business
things* (submissions, products, bundles), not in schemas and tables. Every
user-facing label in the app comes from this module, and every data-quality
rule is a `Check` whose SQL follows the output contract in
`dq/checks/AUTHORING.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Systems
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class System:
    key: str
    name: str  # short label stewards use
    long_name: str
    icon: str  # Material Symbols name, rendered with Streamlit's :material/...: syntax
    color: str
    blurb: str  # one plain-English sentence


SYSTEMS: dict[str, System] = {
    "esub": System(
        key="esub",
        name="eSUB",
        long_name="eSUB / iTRAC submission intake",
        icon="inbox",
        color="#2a78d6",
        blurb="Receives and logs industry submissions before review.",
    ),
    "rhapsody": System(
        key="rhapsody",
        name="Rhapsody",
        long_name="Rhapsody review workflow",
        icon="science",
        color="#eb6834",
        blurb="Tracks CTP review work on each submission.",
    ),
    "pms": System(
        key="pms",
        name="PMS",
        long_name="Product Management System",
        icon="inventory_2",
        color="#1baf7a",
        blurb="Master list of tobacco products and their attributes.",
    ),
    "edw": System(
        key="edw",
        name="Reporting",
        long_name="CTP reporting warehouse (EDW)",
        icon="monitoring",
        color="#e87ba4",
        blurb="Consolidated data behind dashboards and official metrics.",
    ),
}

SYSTEM_ORDER = ["esub", "rhapsody", "pms", "edw"]

# --------------------------------------------------------------------------- #
# Dimensions, severities, entities
# --------------------------------------------------------------------------- #

DIMENSIONS: dict[str, dict[str, str]] = {
    "Completeness": {
        "icon": "rule",
        "question": "Is anything missing?",
        "explain": "A required field is blank, or a record is missing from a system.",
    },
    "Consistency": {
        "icon": "compare_arrows",
        "question": "Does it match?",
        "explain": "A record differs between systems or appears more than once.",
    },
    "Timeliness": {
        "icon": "schedule",
        "question": "Is it on time?",
        "explain": "Dates are out of order, or data has not refreshed on schedule.",
    },
}
DIMENSION_ORDER = list(DIMENSIONS)

SEVERITIES: dict[str, dict] = {
    "High": {"weight": 3, "explain": "Affects official counts or regulatory deadlines."},
    "Medium": {"weight": 2, "explain": "Makes reports or searches less reliable."},
    "Low": {"weight": 1, "explain": "Minor. Fix when convenient."},
}

ENTITIES: dict[str, str] = {
    "Submissions": "A filing from industry, identified by its STN (submission tracking number).",
    "Products": "A tobacco product listed in PMS.",
    "Bundles": "A group of submissions/products that Rhapsody reviews together.",
    "Companies & contacts": "The applicant firm and contact person attached to a submission.",
    "Documents": "Files received with a submission.",
    "Letters & communications": "Letters and other messages exchanged with industry.",
    "Review tasks": "Work items assigned to CTP staff in Rhapsody.",
    "Meetings": "Meeting requests from industry and their scheduled dates.",
    "Submission links": "Amendments and other links between related submissions.",
    "Data feeds": "The automated loads that copy each system's data into the lakehouse.",
}

# Filters the engine knows how to apply to a check's output.
FILTER_DATE = "date"
FILTER_SUBMISSION_TYPE = "submission_type"
# The `office` column and filter hold Rhapsody's Organization field, which is the applicant company
# (confirmed on real data: 3,679 values such as "Philip Morris USA Inc."). Stewards see it as "Organization".
FILTER_OFFICE = "office"
ALL_FILTERS = frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE})

# Columns every check's SQL must output, in this order, before any detail columns.
CONTRACT_COLUMNS = (
    "record_key",
    "record_date",
    "submission_type",
    "office",
    "is_issue",
    "issue_category",
    "issue_reason",
)

# --------------------------------------------------------------------------- #
# Check
# --------------------------------------------------------------------------- #

_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")


@dataclass(frozen=True)
class Check:
    id: str
    title: str  # plain-English statement of the problem, e.g. "Submissions logged in eSUB but missing from Rhapsody"
    systems: tuple[str, ...]  # one system for an internal check, two for a cross-system match
    entity: str
    dimension: str
    severity: str
    question: str  # the yes/no question the check answers, from the steward's point of view
    why: str  # why it matters, in business terms
    how_to_fix: str  # what a steward should do with a flagged record
    sql: str  # base query following the contract in AUTHORING.md
    record_label: str = "records"  # plural noun for what is counted: "submissions", "products"
    filters: frozenset[str] = frozenset({FILTER_DATE})
    date_label: str = "Received date"  # what `record_date` means, shown next to the date filter
    warn_below: float = 0.98  # pass rate at or above this is green
    fail_below: float = 0.90  # pass rate below this is red; in between is amber
    technical_note: str = ""  # tables, keys and logic, shown only with "technical details" on
    assumptions: tuple[str, ...] = ()  # anything not yet confirmed with the data owners
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        problems = []
        if not _ID.match(self.id):
            problems.append("id must be UPPER_SNAKE_CASE")
        unknown = [s for s in self.systems if s not in SYSTEMS]
        if not self.systems or unknown or len(self.systems) > 2:
            problems.append(f"systems must be 1-2 of {list(SYSTEMS)}, got {self.systems}")
        if self.dimension not in DIMENSIONS:
            problems.append(f"dimension must be one of {list(DIMENSIONS)}")
        if self.severity not in SEVERITIES:
            problems.append(f"severity must be one of {list(SEVERITIES)}")
        if self.entity not in ENTITIES:
            problems.append(f"entity must be one of {list(ENTITIES)}")
        if not self.filters <= ALL_FILTERS:
            problems.append(f"filters must be a subset of {sorted(ALL_FILTERS)}")
        if not (0 < self.fail_below <= self.warn_below <= 1):
            problems.append("thresholds must satisfy 0 < fail_below <= warn_below <= 1")
        if problems:
            raise ValueError(f"Check {self.id}: " + "; ".join(problems))

    @property
    def is_cross_system(self) -> bool:
        return len(self.systems) == 2

    @property
    def home_system(self) -> str:
        return self.systems[0]

    def placeholders_used(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.sql))

    def render_sql(self, placeholders: dict[str, str]) -> str:
        missing = self.placeholders_used() - placeholders.keys()
        if missing:
            raise KeyError(f"Check {self.id} uses unknown placeholders: {sorted(missing)}")
        from dq.sqlfrag import expand

        return _PLACEHOLDER.sub(lambda m: placeholders[m.group(1)], expand(self.sql)).strip().rstrip(";")


def status_for(pass_rate: float | None, check: Check) -> str:
    """'pass' | 'warn' | 'fail' | 'none' (nothing to evaluate)."""
    if pass_rate is None:
        return "none"
    if pass_rate >= check.warn_below:
        return "pass"
    if pass_rate >= check.fail_below:
        return "warn"
    return "fail"


STATUS_LABEL = {"pass": "Healthy", "warn": "Needs attention", "fail": "Action needed", "none": "No data", "error": "Could not run"}
