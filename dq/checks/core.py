"""The MVP's curated core set of checks, and the five areas the MVP groups them into.

The full catalog stays in the group modules; the MVP shows only CORE_CHECK_IDS
so stewards can review and test a small, high-value set first. Set
DQ_CHECK_SET=all to show every check in the MVP layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dq.checks import CHECKS_BY_ID, load_checks
from dq.model import Check

CORE_CHECK_IDS: tuple[str, ...] = (
    # Across systems — the stewards' #1 pain point: records that should match 1-to-1
    "ESUB_RHAP_MISSING_IN_RHAPSODY",
    "ESUB_RHAP_MISSING_IN_ESUB",
    "ESUB_RHAP_RECEIVED_DATE_DIFFERS",
    "ESUB_RHAP_COMPANY_DIFFERS",
    "ESUB_RHAP_PRODUCT_COUNT_DIFFERS",
    "RHAP_PMS_LINKED_PRODUCT_NOT_IN_PMS",
    "RHAP_PMS_PRODUCT_NAME_DIFFERS",
    "RPT_RHAP_SUBMISSIONS_NOT_IN_BOTH",
    "RPT_RHAP_SUBMISSION_DETAILS_DIFFER",
    "RPT_PMS_PRODUCTS_NOT_IN_BOTH",
    # eSUB
    "ESUB_LOGIN_REQUIRED_FIELDS_BLANK",
    "ESUB_LOGIN_DUPLICATE_STN",
    "ESUB_LOGIN_DATES_IMPOSSIBLE",
    # Rhapsody
    "RHAP_SUB_MISSING_COUNT_FIELDS",
    "RHAP_SUB_DUPLICATE_STN",
    "RHAP_SUB_NO_PRODUCTS",
    "RHAP_SUB_DATES_OUT_OF_ORDER",
    # PMS
    "PMS_PRODUCT_NO_STN_OR_STATIC_ID",
    "PMS_PRODUCT_POSSIBLE_DUPLICATE",
    "PMS_PRODUCT_CATEGORY_NOT_RECOGNIZED",
    # Data feeds
    "FEED_ESUB_FRESHNESS",
    "FEED_RHAP_FRESHNESS",
    "FEED_PMS_FRESHNESS",
    "FEED_EDW_FRESHNESS",
)


@dataclass(frozen=True)
class Area:
    key: str
    name: str
    blurb: str
    color: str


AREAS: dict[str, Area] = {
    "across": Area("across", "Across systems", "Records that should match 1-to-1 between systems.", "#6250d6"),
    "esub": Area("esub", "eSUB", "Incoming submissions and their log-in records.", "#2a78d6"),
    "rhapsody": Area("rhapsody", "Rhapsody", "Submissions under CTP review.", "#eb6834"),
    "pms": Area("pms", "PMS", "The master list of tobacco products.", "#1baf7a"),
    "feeds": Area("feeds", "Data feeds", "Whether each system's data has refreshed on time.", "#8a8a85"),
}
AREA_ORDER = list(AREAS)

# Plain words for the three kinds of problems.
KIND_WORDS = {"Completeness": "Missing", "Consistency": "Doesn't match", "Timeliness": "Timing"}


def area_of(check: Check) -> str:
    if check.entity == "Data feeds":
        return "feeds"
    if check.is_cross_system:
        return "across"
    return check.home_system if check.home_system in AREAS else "feeds"


def active_checks() -> list[Check]:
    """Checks the MVP shows: the core set, or every check when DQ_CHECK_SET=all."""
    if os.getenv("DQ_CHECK_SET", "core").strip().lower() == "all":
        return list(load_checks())
    by_id = CHECKS_BY_ID
    return [by_id[i] for i in CORE_CHECK_IDS if i in by_id]


def missing_core_ids() -> list[str]:
    by_id = CHECKS_BY_ID
    return [i for i in CORE_CHECK_IDS if i not in by_id]
