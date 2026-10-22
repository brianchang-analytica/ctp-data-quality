"""Checks between the source systems and the Reporting warehouse, plus data-feed health.

Two groups:

(a) Rhapsody / PMS / eSUB  <->  Reporting (gold EDW): is every submission, bundle,
    product, letter and submission link that exists in the source also in Reporting,
    and do the key details agree?
(b) Data feeds: has each system's copy refreshed recently, did every record that
    arrived make it through processing, does eSUB intake volume look normal, and did
    the standardization ("clean-up") job succeed?

Keys, names and blank checks use the shared macros in dq/sqlfrag.py (STN_KEY, NUM_KEY,
ANY_KEY, TXT, CO_NAME, BLANK, FLAG_ON). They are expanded by Check.render_sql, so the
SQL that runs on the warehouse (and that "technical details" shows) is plain Databricks
SQL, and every check in the app matches a record the same way.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check

try:  # the placeholder rule is defined once, in the Rhapsody <-> PMS checks (dq/lookup.py imports it too)
    from dq.checks.rhapsody_pms import PLACEHOLDER_ID_ROWS
except ImportError:  # pragma: no cover - keep this module loading if the name moves
    PLACEHOLDER_ID_ROWS = 100

# --------------------------------------------------------------------------- #
# Module helpers (only what the shared macros do not cover)
# --------------------------------------------------------------------------- #

_EARLIEST = "DATE'2009-06-22'"  # Tobacco Control Act: no CTP business date is earlier

# eSUB submission link types (esub_itrac_sub_links.DESCRIPTOR_CODE, compared with ANY_KEY) that Reporting
# does not load by design, so RPT_ESUB_LINKS_MISSING_FROM_REPORTING skips them. Confirmed on real data
# (Oct 2026): type 12 = 1 of 1,363 links found in Reporting, type 15 = 0 of 2, type 8 = 9 of 36; every other
# link type is 96-100% found (P2.10).
_ESUB_LINK_TYPES_NOT_IN_REPORTING = ("8", "12", "15")

# HOW PRODUCTS LINK. Confirmed on real data (Oct 2026):
#   * Reporting product IDs are PMS record numbers: dim_product.id = pms_product.id for 100% (K34), bulk
#     products included (K63), and fact_bundle_submission_product.product_id uses the same id (K35, K36).
#   * Rhapsody product IDs (regular and bulk lists) are PMS UUIDs (K24, K50). Rhapsody bulk IDs match
#     dim_product.id 0% (K64). So a Rhapsody product reaches Reporting ONLY through dim_product.uuid; no check
#     here ever compares a Rhapsody product ID with dim_product.id.
#   * One placeholder value sits on 14,359 Rhapsody product rows and one PMS uuid on 14,368 products (P4 U07,
#     U10). Any value on more than PLACEHOLDER_ID_ROWS product rows is found from the data in a CTE on every run
#     and left out, so those products are never collapsed into one.


def _status_label(expr: str) -> str:
    """Rhapsody / Reporting submission_status for display: '1' -> 'Open (1)', '0' -> 'Closed (0)'.

    Confirmed on real data (Oct 2026, P2.14): Rhapsody holds '1' (16%), '0' (8.5%) or blank (75%). Blank
    stays blank (NULL); any other code is shown as stored.
    """
    return (f"CASE ANY_KEY({expr}) WHEN '1' THEN 'Open (1)' WHEN '0' THEN 'Closed (0)' "
            f"ELSE BLANK({expr}) END")


def _first_plausible(*candidates: str) -> str:
    """SQL for the first candidate date between 2009-06-22 and today.

    The last candidate is the load-date fallback and is used as-is. This keeps a
    mistyped date (e.g. 2099) from pushing a record outside the steward's date filter.
    """
    *business, fallback = candidates
    parts = [
        f"CASE WHEN CAST({c} AS DATE) BETWEEN {_EARLIEST} AND current_date() THEN CAST({c} AS DATE) END"
        for c in business
    ]
    return "coalesce(" + ", ".join(parts + [f"CAST({fallback} AS DATE)"]) + ")"


def _recent(expr: str, days: int = 2) -> str:
    """TRUE when the date falls in the last `days` days up to today (a future date is not 'recent'); never NULL."""
    return f"coalesce(CAST({expr} AS DATE) BETWEEN date_sub(current_date(), {days}) AND current_date(), false)"


_TOKEN = re.compile(r"@[A-Z_]+@")


def _sql(*parts: str, **tokens: str) -> str:
    """Join SQL fragments and fill @TOKEN@ placeholders (macros are expanded later by Check.render_sql)."""
    sql = "".join(parts)
    for name, value in tokens.items():
        sql = sql.replace(f"@{name}@", value)
    left = sorted(set(_TOKEN.findall(sql)))
    if left:
        raise ValueError(f"unreplaced SQL tokens: {left}")
    return sql


# --------------------------------------------------------------------------- #
# Shared building blocks
# --------------------------------------------------------------------------- #

# One row per STN from Rhapsody: an active row when one exists, otherwise the latest
# soft-deleted row (so "removed in Rhapsody" can be told apart from "never in Rhapsody").
_RH_SUBMISSIONS = r"""
rh_rows AS (
  SELECT
    STN_KEY(stn)                                    AS stn_key,
    BLANK(stn)                                      AS stn,
    date_received,
    date_submitted,
    date_created,
    BLANK(stdz_submission_type)                     AS stdz_submission_type,
    BLANK(organization)                             AS organization,
    BLANK(submission_status)                        AS submission_status,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company)) AS company,
    coalesce(halo_is_deleted, false)                AS is_deleted,
    halo_deleted_ts                                 AS removed_ts,
    CAST(halo_insert_ts AS DATE)                    AS load_date,
    coalesce(halo_update_ts, halo_insert_ts)        AS loaded_ts
  FROM ${core}.rhapsody_submissions
),
rh AS (
  SELECT stn_key, stn, date_received, date_submitted, date_created, stdz_submission_type,
         organization, submission_status, company, is_deleted, removed_ts, load_date
  FROM rh_rows
  WHERE stn_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY stn_key
                             ORDER BY CASE WHEN is_deleted THEN 1 ELSE 0 END, loaded_ts DESC NULLS LAST) = 1
)"""

# One row per STN from the current version of Reporting's submission dimension.
_RP_SUBMISSIONS = r"""
rp_rows AS (
  SELECT
    STN_KEY(stn)                                    AS stn_key,
    BLANK(stn)                                      AS stn,
    date_received,
    date_submitted,
    BLANK(stdz_submission_type)                     AS stdz_submission_type,
    BLANK(organization)                             AS organization,
    BLANK(submission_status)                        AS submission_status,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company)) AS company,
    coalesce(halo_is_deleted, false)                AS is_deleted,
    CAST(halo_created_ts AS DATE)                   AS load_date,
    coalesce(halo_modified_ts, halo_created_ts)     AS loaded_ts
  FROM ${edw}.dim_submission
  WHERE halo_is_current = true
),
rp AS (
  SELECT stn_key, stn, date_received, date_submitted, stdz_submission_type, organization,
         submission_status, company, is_deleted, load_date
  FROM rp_rows
  WHERE stn_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY stn_key
                             ORDER BY CASE WHEN is_deleted THEN 1 ELSE 0 END, loaded_ts DESC NULLS LAST) = 1
)"""

# Bundles: Rhapsody (deleted flag or soft delete = removed; temporary working copies set aside)
# and the current version of Reporting's bundle dimension, one row per bundle ID on each side.
_RH_BUNDLES = r"""
rhb_rows AS (
  SELECT
    NUM_KEY(id)                                     AS bundle_key,
    CAST(id AS STRING)                              AS bundle_id,
    BLANK(bundlename)                               AS bundle_name,
    BLANK(bundlestatus)                             AS bundle_status,
    goaldate                                        AS goal_date,
    datereceivedstart                               AS received_start,
    s_createddate                                   AS created_ts,
    BLANK(organization)                             AS organization,
    BLANK(stdz_submission_type)                     AS stdz_submission_type,
    coalesce(halo_is_deleted, false) OR coalesce(deleted, false) AS is_deleted,
    coalesce(s_is_temporary_copy, false)            AS is_temp,
    halo_deleted_ts                                 AS removed_ts,
    CAST(halo_insert_ts AS DATE)                    AS load_date,
    coalesce(halo_update_ts, halo_insert_ts)        AS loaded_ts
  FROM ${core}.rhapsody_bundle
),
rhb AS (
  SELECT bundle_key, bundle_id, bundle_name, bundle_status, goal_date, received_start, created_ts,
         organization, stdz_submission_type, is_deleted, is_temp, removed_ts, load_date
  FROM rhb_rows
  WHERE bundle_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY bundle_key
                             ORDER BY CASE WHEN is_temp THEN 1 ELSE 0 END,
                                      CASE WHEN is_deleted THEN 1 ELSE 0 END,
                                      loaded_ts DESC NULLS LAST) = 1
),
rpb_rows AS (
  SELECT
    NUM_KEY(bundle_id)                              AS bundle_key,
    CAST(bundle_id AS STRING)                       AS bundle_id,
    BLANK(bundle_name)                              AS bundle_name,
    BLANK(bundle_status)                            AS bundle_status,
    goal_date,
    date_received_start                             AS received_start,
    bundle_created_date                             AS created_ts,
    BLANK(organization)                             AS organization,
    BLANK(stdz_submission_type)                     AS stdz_submission_type,
    coalesce(halo_is_deleted, false) OR coalesce(deleted, false) AS is_deleted,
    CAST(halo_created_ts AS DATE)                   AS load_date,
    coalesce(halo_modified_ts, halo_created_ts)     AS loaded_ts
  FROM ${edw}.dim_bundle
  WHERE halo_is_current = true
),
rpb AS (
  SELECT bundle_key, bundle_id, bundle_name, bundle_status, goal_date, received_start, created_ts,
         organization, stdz_submission_type, is_deleted, load_date
  FROM rpb_rows
  WHERE bundle_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY bundle_key
                             ORDER BY CASE WHEN is_deleted THEN 1 ELSE 0 END, loaded_ts DESC NULLS LAST) = 1
)"""

# PMS products (with their unique-product link) and Reporting's product dimension, one row per product ID.
_PMS_PRODUCTS = r"""
pm_rows AS (
  SELECT
    NUM_KEY(id)                                     AS product_key,
    CAST(id AS STRING)                              AS product_id,
    NUM_KEY(unique_product_id)                      AS unique_key,
    BLANK(product_name)                             AS product_name,
    BLANK(tp_number)                                AS tp_number,
    BLANK(stdz_stn)                                 AS stdz_stn,
    create_date,
    coalesce(halo_is_deleted, false)                AS is_deleted,
    halo_deleted_ts                                 AS removed_ts,
    CAST(coalesce(halo_insert_ts, halo_update_ts) AS DATE) AS load_date,
    coalesce(halo_update_ts, halo_insert_ts)        AS loaded_ts
  FROM ${core}.pms_product
),
pm AS (
  SELECT product_key, product_id, unique_key, product_name, tp_number, stdz_stn, create_date,
         is_deleted, removed_ts, load_date
  FROM pm_rows
  WHERE product_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY product_key
                             ORDER BY CASE WHEN is_deleted THEN 1 ELSE 0 END, loaded_ts DESC NULLS LAST) = 1
),
rpp_rows AS (
  SELECT
    NUM_KEY(id)                                     AS product_key,
    CAST(id AS STRING)                              AS product_id,
    BLANK(product_name)                             AS product_name,
    BLANK(tp_number)                                AS tp_number,
    BLANK(stdz_stn)                                 AS stdz_stn,
    BLANK(ctgry_code)                               AS ctgry_code,
    BLANK(subctgry_code)                            AS subctgry_code,
    create_date,
    coalesce(halo_is_deleted, false)                AS is_deleted,
    CAST(halo_created_ts AS DATE)                   AS load_date,
    coalesce(halo_modified_ts, halo_created_ts)     AS loaded_ts
  FROM ${edw}.dim_product
  WHERE halo_is_current = true
),
rpp AS (
  SELECT product_key, product_id, product_name, tp_number, stdz_stn, ctgry_code, subctgry_code,
         create_date, is_deleted, load_date
  FROM rpp_rows
  WHERE product_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY product_key
                             ORDER BY CASE WHEN is_deleted THEN 1 ELSE 0 END, loaded_ts DESC NULLS LAST) = 1
)"""


# --------------------------------------------------------------------------- #
# (a) Source systems vs Reporting
# --------------------------------------------------------------------------- #

RHAP_SUBMISSIONS_MISMATCH = Check(
    id="RPT_RHAP_SUBMISSIONS_NOT_IN_BOTH",
    title="Submissions missing from Rhapsody or Reporting",
    systems=("rhapsody", "edw"),
    entity="Submissions",
    dimension="Completeness",
    severity="High",
    question="Does every active Rhapsody submission appear in Reporting, and does Reporting show only "
             "submissions still active in Rhapsody?",
    why="Official submission counts and dashboards come from Reporting. A submission missing there drops out "
        "of the numbers. A submission removed from Rhapsody but still in Reporting gets counted by mistake.",
    how_to_fix="Find the STN in Rhapsody. If it is active there but missing from Reporting, send the STN to the "
               "Reporting (EDW) team so the next refresh adds it. If someone removed it in Rhapsody on purpose, "
               "ask the Reporting team to remove it too. An STN found only in Reporting may have been mistyped "
               "or merged in Rhapsody, because Reporting takes every submission from Rhapsody. Submissions that "
               "exist only in eSUB are not expected in Reporting and are not checked here.",
    record_label="submissions",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Received date",
    warn_below=0.999,
    fail_below=0.99,
    technical_note=(
        "slvr_ctp_core.rhapsody_submissions FULL OUTER JOIN gold_ctp_edw.dim_submission on STN_KEY(stn) "
        "(upper case, all whitespace removed). Each side is first reduced to one row per STN with "
        "ROW_NUMBER (active row preferred, then latest halo_update_ts / halo_modified_ts). Active = "
        "halo_is_deleted false (Rhapsody; the table has no source deleted flag) and halo_is_current true AND "
        "halo_is_deleted false (Reporting). Population: STNs active in Rhapsody, plus STNs active only in "
        "Reporting. Confirmed on real data (Oct 2026): Reporting's submission list matches Rhapsody STN for "
        "STN (K32: 100% both ways) and every Reporting submission comes from Rhapsody (P2.29: "
        "halo_source_system = Rhapsody on all 46,821), so a Reporting STN with no Rhapsody record is always "
        "flagged, whatever its source label (the former eSUB/iTRAC source exemption was removed). Confirmed on "
        "real data (Oct 2026): eSUB-only STNs are not expected in Reporting (K33: 39% of active eSUB log-in "
        "STNs are in Reporting), so eSUB is never read and no eSUB-only STN is expected. Rhapsody "
        "submissions created (else received) in the last 2 days and not yet in Reporting are not evaluated "
        "(waiting for the nightly refresh). record_date = first plausible (2009-06-22..today) of Rhapsody "
        "received, Reporting received, Rhapsody created, else the first load date. Status = Rhapsody "
        "submission_status, else Reporting's, shown as 'Open (1)' / 'Closed (0)'. Confirmed on real data "
        "(Oct 2026): the field holds '1', '0' or blank (P2.14). Categories: Missing from Reporting / Still in "
        "Reporting (removed in Rhapsody) / Only in Reporting."
    ),
    assumptions=(
        "Reporting refreshes at least daily, so a 2-day grace period for brand-new submissions is enough.",
    ),
    sql=_sql(
        "WITH", _RH_SUBMISSIONS, ",", _RP_SUBMISSIONS, r""",
j AS (
  SELECT
    coalesce(rh.stn, rp.stn)                                        AS stn,
    coalesce(rh.stn_key IS NOT NULL AND NOT rh.is_deleted, false)   AS in_rhap,
    coalesce(rh.is_deleted, false)                                  AS removed_in_rhap,
    coalesce(rp.stn_key IS NOT NULL AND NOT rp.is_deleted, false)   AS in_rpt,
    coalesce(rp.is_deleted, false)                                  AS removed_in_rpt,
    rh.date_received                                                AS rh_received,
    rp.date_received                                                AS rp_received,
    rh.date_created                                                 AS rh_created,
    coalesce(rh.load_date, rp.load_date)                            AS load_date,
    rh.removed_ts                                                   AS rh_removed_ts,
    rh.submission_status                                            AS rh_status,
    rp.submission_status                                            AS rp_status,
    coalesce(rh.stdz_submission_type, rp.stdz_submission_type)      AS sub_type,
    coalesce(rh.organization, rp.organization)                      AS office,
    coalesce(rh.company, rp.company)                                AS company
  FROM rh
  FULL OUTER JOIN rp ON rh.stn_key = rp.stn_key
),
e AS (
  SELECT
    stn, in_rhap, removed_in_rhap, in_rpt, removed_in_rpt, rh_received, rp_received,
    rh_created, load_date, rh_removed_ts, rh_status, rp_status, sub_type, office, company,
    CASE
      WHEN in_rhap AND NOT in_rpt THEN 'Missing from Reporting'
      WHEN in_rpt AND NOT in_rhap AND removed_in_rhap THEN 'Still in Reporting'
      WHEN in_rpt AND NOT in_rhap THEN 'Only in Reporting'
    END AS category
  FROM j
  WHERE (in_rhap AND (in_rpt OR NOT @NEW_IN_RHAP@))
     OR (in_rpt AND NOT in_rhap)
)
SELECT
  coalesce(stn, '(no STN)')                                         AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(category IS NOT NULL, false)                             AS is_issue,
  category                                                          AS issue_category,
  CASE category
    WHEN 'Missing from Reporting' THEN
      concat('Active in Rhapsody (received ', coalesce(CAST(rh_received AS STRING), 'date blank'), ') but ',
             CASE WHEN removed_in_rpt THEN 'marked removed' ELSE 'not found' END, ' in Reporting.')
    WHEN 'Still in Reporting' THEN
      concat('Removed from Rhapsody',
             coalesce(concat(' on ', CAST(CAST(rh_removed_ts AS DATE) AS STRING)), ''),
             ' but still active in Reporting (received ', coalesce(CAST(rp_received AS STRING), 'date blank'), ').')
    WHEN 'Only in Reporting' THEN
      concat('Active in Reporting (received ', coalesce(CAST(rp_received AS STRING), 'date blank'),
             ') but this STN is not in Rhapsody.')
  END                                                               AS issue_reason,
  stn                                                               AS `STN`,
  company                                                           AS `Company`,
  CASE WHEN in_rhap THEN 'Yes' WHEN removed_in_rhap THEN 'Removed' ELSE 'No' END AS `In Rhapsody`,
  CASE WHEN in_rpt THEN 'Yes' WHEN removed_in_rpt THEN 'Removed' ELSE 'No' END   AS `In Reporting`,
  rh_received                                                       AS `Received (Rhapsody)`,
  rp_received                                                       AS `Received (Reporting)`,
  @STATUS@                                                          AS `Status`,
  sub_type                                                          AS `Submission type`
FROM e
""",
        NEW_IN_RHAP=_recent("coalesce(rh_created, rh_received)"),
        REC_DATE=_first_plausible("rh_received", "rp_received", "rh_created", "load_date"),
        STATUS=_status_label("coalesce(rh_status, rp_status)"),
    ),
)


RHAP_SUBMISSION_DETAILS = Check(
    id="RPT_RHAP_SUBMISSION_DETAILS_DIFFER",
    title="Submission details differ: Rhapsody vs Reporting",
    systems=("rhapsody", "edw"),
    entity="Submissions",
    dimension="Consistency",
    severity="High",
    question="For submissions in both systems, do the dates, type, status, organization and company in "
             "Reporting match Rhapsody?",
    why="Reporting should be an exact copy of Rhapsody. A wrong date in Reporting skews review-time metrics. "
        "A wrong type or status skews the counts that dashboards show, even when Rhapsody is right.",
    how_to_fix="Compare the values side by side. Rhapsody is the system of record. If Rhapsody is right, send "
               "the STN to the Reporting (EDW) team with the fields that differ. If Rhapsody changed today, "
               "re-check after the overnight refresh. If Rhapsody is wrong, correct it there.",
    record_label="submissions",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Received date",
    warn_below=0.99,
    fail_below=0.95,
    technical_note=(
        "slvr_ctp_core.rhapsody_submissions INNER JOIN gold_ctp_edw.dim_submission on STN_KEY(stn); "
        "both sides de-duplicated to one row per STN and limited to active rows (halo_is_deleted false; "
        "Reporting also halo_is_current true). Compared null-safely with <=>: date_received and "
        "date_submitted exactly (both DATE, copied from the same source, so no tolerance); "
        "stdz_submission_type / organization with TXT() (upper/trim/whitespace collapse); submission_status "
        "with ANY_KEY() (so '1' = '1.0') and shown as 'Open (1)' / 'Closed (0)'; "
        "company = coalesce(abpc_company_name, itrac_company, abc_company) with CO_NAME() (punctuation and "
        "INC/LLC/LTD/CO/CORP/... removed). Blank and missing count as the same. record_date = first plausible "
        "of Rhapsody received, Reporting received, Rhapsody submitted, else the first load date. Confirmed on "
        "real data (Oct 2026): every Rhapsody STN is in Reporting and the reverse (K32: 100%), so the inner "
        "match loses no submission. Confirmed on real data (Oct 2026): submission_status is '1' (open, 16%), "
        "'0' (closed, 8.5%) or blank (75%) (P2.14). Confirmed on real data (Oct 2026): organization is the "
        "applicant company stewards see as Organization in Rhapsody (P2.17: 3,679 distinct values such as "
        "'Philip Morris USA Inc.'), not a CTP office."
    ),
    assumptions=(
        "Reporting copies these fields from Rhapsody unchanged. Any difference is a refresh or load problem, "
        "not an intended change.",
        "In both systems the company shown is the company-record name. When that is blank, it falls back to "
        "the iTRAC company, then the ABC company.",
        "A blank status means Rhapsody has not marked the submission open or closed yet.",
    ),
    sql=_sql(
        "WITH", _RH_SUBMISSIONS, ",", _RP_SUBMISSIONS, r""",
m AS (
  SELECT
    rh.stn,
    rh.load_date,
    rh.date_received        AS rh_recv,     rp.date_received        AS rp_recv,
    rh.date_submitted       AS rh_subm,     rp.date_submitted       AS rp_subm,
    rh.stdz_submission_type AS rh_type,     rp.stdz_submission_type AS rp_type,
    rh.submission_status    AS rh_status,   rp.submission_status    AS rp_status,
    rh.organization         AS rh_org,      rp.organization         AS rp_org,
    rh.company              AS rh_company,  rp.company              AS rp_company
  FROM rh
  JOIN rp ON rh.stn_key = rp.stn_key
  WHERE NOT rh.is_deleted AND NOT rp.is_deleted
),
d AS (
  SELECT
    stn, load_date, rh_recv, rp_recv, rh_subm, rp_subm, rh_type, rp_type, rh_status, rp_status,
    rh_org, rp_org, rh_company, rp_company,
    @RH_STATUS@                                        AS rh_status_label,
    @RP_STATUS@                                        AS rp_status_label,
    NOT (rh_recv <=> rp_recv)                          AS d_recv,
    NOT (rh_subm <=> rp_subm)                          AS d_subm,
    NOT (TXT(rh_type) <=> TXT(rp_type))                AS d_type,
    NOT (ANY_KEY(rh_status) <=> ANY_KEY(rp_status))    AS d_status,
    NOT (TXT(rh_org) <=> TXT(rp_org))                  AS d_org,
    NOT (CO_NAME(rh_company) <=> CO_NAME(rp_company))  AS d_company
  FROM m
),
n AS (
  SELECT
    d.*,
    CAST(d_recv AS INT) + CAST(d_subm AS INT) + CAST(d_type AS INT)
      + CAST(d_status AS INT) + CAST(d_org AS INT) + CAST(d_company AS INT) AS n_diff
  FROM d
)
SELECT
  coalesce(stn, '(no STN)')                                         AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(rh_type AS STRING)                                           AS submission_type,
  CAST(rh_org AS STRING)                                            AS office,
  coalesce(n_diff > 0, false)                                       AS is_issue,
  CASE
    WHEN n_diff = 0 THEN NULL
    WHEN n_diff > 1 THEN 'Several details differ'
    WHEN d_recv     THEN 'Received date differs'
    WHEN d_subm     THEN 'Submitted date differs'
    WHEN d_type     THEN 'Submission type differs'
    WHEN d_status   THEN 'Status differs'
    WHEN d_org      THEN 'Organization differs'
    ELSE 'Company differs'
  END                                                               AS issue_category,
  CASE WHEN n_diff > 0 THEN concat_ws(' ',
    CASE WHEN d_recv THEN concat('Received date is ', coalesce(CAST(rh_recv AS STRING), 'blank'),
                                 ' in Rhapsody but ', coalesce(CAST(rp_recv AS STRING), 'blank'), ' in Reporting.') END,
    CASE WHEN d_subm THEN concat('Submitted date is ', coalesce(CAST(rh_subm AS STRING), 'blank'),
                                 ' in Rhapsody but ', coalesce(CAST(rp_subm AS STRING), 'blank'), ' in Reporting.') END,
    CASE WHEN d_type THEN concat('Submission type is ', coalesce(concat('"', rh_type, '"'), 'blank'),
                                 ' in Rhapsody but ', coalesce(concat('"', rp_type, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_status THEN concat('Status is ', coalesce(concat('"', rh_status_label, '"'), 'blank'),
                                   ' in Rhapsody but ', coalesce(concat('"', rp_status_label, '"'), 'blank'),
                                   ' in Reporting.') END,
    CASE WHEN d_org THEN concat('Organization is ', coalesce(concat('"', rh_org, '"'), 'blank'),
                                   ' in Rhapsody but ', coalesce(concat('"', rp_org, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_company THEN concat('Company is ', coalesce(concat('"', rh_company, '"'), 'blank'),
                                    ' in Rhapsody but ', coalesce(concat('"', rp_company, '"'), 'blank'), ' in Reporting.') END
  ) END                                                             AS issue_reason,
  stn                                                               AS `STN`,
  concat_ws(', ',
    CASE WHEN d_recv THEN 'Received date' END, CASE WHEN d_subm THEN 'Submitted date' END,
    CASE WHEN d_type THEN 'Submission type' END, CASE WHEN d_status THEN 'Status' END,
    CASE WHEN d_org THEN 'Organization' END, CASE WHEN d_company THEN 'Company' END)     AS `Fields that differ`,
  rh_recv                                                           AS `Received (Rhapsody)`,
  rp_recv                                                           AS `Received (Reporting)`,
  rh_type                                                           AS `Type (Rhapsody)`,
  rp_type                                                           AS `Type (Reporting)`,
  rh_status_label                                                   AS `Status (Rhapsody)`,
  rp_status_label                                                   AS `Status (Reporting)`,
  rh_org                                                            AS `Organization (Rhapsody)`,
  rp_org                                                            AS `Organization (Reporting)`,
  rh_company                                                        AS `Company (Rhapsody)`,
  rp_company                                                        AS `Company (Reporting)`
FROM n
""",
        REC_DATE=_first_plausible("rh_recv", "rp_recv", "rh_subm", "load_date"),
        RH_STATUS=_status_label("rh_status"),
        RP_STATUS=_status_label("rp_status"),
    ),
)


RHAP_BUNDLES_MISMATCH = Check(
    id="RPT_RHAP_BUNDLES_NOT_IN_BOTH",
    title="Bundles missing from Rhapsody or Reporting",
    systems=("rhapsody", "edw"),
    entity="Bundles",
    dimension="Completeness",
    severity="High",
    question="Does every active Rhapsody bundle appear in Reporting, and does Reporting show only bundles "
             "still active in Rhapsody?",
    why="Dashboards that track workload and goal dates count bundles from Reporting. A missing bundle hides "
        "work and deadlines. A bundle deleted in Rhapsody but still active in Reporting inflates the counts.",
    how_to_fix="Find the bundle in Rhapsody by its ID or name. If it is active there but missing from Reporting, "
               "send the bundle ID to the Reporting (EDW) team. Ask that team to remove or flag any bundle "
               "deleted in Rhapsody on purpose. A bundle found only in Reporting may have been split or "
               "replaced in Rhapsody.",
    record_label="bundles",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Bundle received date",
    warn_below=0.999,
    fail_below=0.99,
    technical_note=(
        "slvr_ctp_core.rhapsody_bundle (id BIGINT) FULL OUTER JOIN gold_ctp_edw.dim_bundle (bundle_id INT), "
        "matched with NUM_KEY() (BIGINT text). Each side de-duplicated to one row per bundle "
        "(non-temporary, then active, then latest load). Removed = halo_is_deleted OR the source 'deleted' "
        "flag (both systems carry it). Reporting limited to halo_is_current = true. Bundles whose only "
        "Rhapsody record is a temporary working copy (s_is_temporary_copy) are excluded. Bundles created "
        "(else received) in Rhapsody in the last 2 days and not yet in Reporting are not evaluated. "
        "record_date = first plausible of Rhapsody datereceivedstart, s_createddate, Reporting "
        "date_received_start, bundle_created_date, else the first load date. Submission type / organization = "
        "the bundle's stdz_submission_type / organization. Confirmed on real data (Oct 2026): Reporting's "
        "bundle ID is the Rhapsody bundle ID (K37: 100% of live Rhapsody bundles found, and the reverse). "
        "Confirmed on real data (Oct 2026): organization is the applicant company stewards see as "
        "Organization in Rhapsody (P2.17), not a CTP office."
    ),
    assumptions=(
        "A bundle marked deleted in Rhapsody should either be absent from Reporting or also marked deleted there.",
        "Rhapsody 'temporary copy' records are editing copies, not real bundles.",
    ),
    sql=_sql("WITH", _RH_BUNDLES, r""",
j AS (
  SELECT
    coalesce(rhb.bundle_id, rpb.bundle_id)                                AS bundle_id,
    coalesce(rhb.bundle_name, rpb.bundle_name)                            AS bundle_name,
    coalesce(rhb.is_temp, false)                                          AS rh_is_temp,
    coalesce(rhb.bundle_key IS NOT NULL AND NOT rhb.is_deleted, false)    AS in_rhap,
    coalesce(rhb.is_deleted, false)                                       AS removed_in_rhap,
    coalesce(rpb.bundle_key IS NOT NULL AND NOT rpb.is_deleted, false)    AS in_rpt,
    coalesce(rpb.is_deleted, false)                                       AS removed_in_rpt,
    rhb.removed_ts                                                        AS rh_removed_ts,
    rhb.bundle_status                                                     AS rh_status,
    rpb.bundle_status                                                     AS rp_status,
    rhb.created_ts                                                        AS rh_created,
    rhb.received_start                                                    AS rh_received,
    rpb.received_start                                                    AS rp_received,
    rpb.created_ts                                                        AS rp_created,
    coalesce(rhb.load_date, rpb.load_date)                                AS load_date,
    coalesce(rhb.stdz_submission_type, rpb.stdz_submission_type)          AS sub_type,
    coalesce(rhb.organization, rpb.organization)                          AS office
  FROM rhb
  FULL OUTER JOIN rpb ON rhb.bundle_key = rpb.bundle_key
),
e AS (
  SELECT
    bundle_id, bundle_name, in_rhap, removed_in_rhap, in_rpt, removed_in_rpt, rh_removed_ts,
    rh_status, rp_status, rh_created, rh_received, rp_received, rp_created, load_date, sub_type, office,
    CASE
      WHEN in_rhap AND NOT in_rpt THEN 'Missing from Reporting'
      WHEN in_rpt AND NOT in_rhap AND removed_in_rhap THEN 'Still in Reporting'
      WHEN in_rpt AND NOT in_rhap THEN 'Only in Reporting'
    END AS category
  FROM j
  WHERE NOT rh_is_temp
    AND ((in_rhap AND (in_rpt OR NOT @NEW_IN_RHAP@))
         OR (in_rpt AND NOT in_rhap))
)
SELECT
  coalesce(bundle_id, '(no bundle ID)')                             AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(category IS NOT NULL, false)                             AS is_issue,
  category                                                          AS issue_category,
  CASE category
    WHEN 'Missing from Reporting' THEN
      concat('Active in Rhapsody (status ', coalesce(concat('"', rh_status, '"'), 'blank'), ') but ',
             CASE WHEN removed_in_rpt THEN 'marked deleted' ELSE 'not found' END, ' in Reporting.')
    WHEN 'Still in Reporting' THEN
      concat('Deleted in Rhapsody',
             coalesce(concat(' on ', CAST(CAST(rh_removed_ts AS DATE) AS STRING)), ''),
             ' but still active in Reporting (status ', coalesce(concat('"', rp_status, '"'), 'blank'), ').')
    WHEN 'Only in Reporting' THEN
      concat('Active in Reporting (status ', coalesce(concat('"', rp_status, '"'), 'blank'),
             ') but bundle ', bundle_id, ' is not in Rhapsody.')
  END                                                               AS issue_reason,
  bundle_id                                                         AS `Bundle ID`,
  bundle_name                                                       AS `Bundle name`,
  CASE WHEN in_rhap THEN 'Yes' WHEN removed_in_rhap THEN 'Deleted' ELSE 'No' END AS `In Rhapsody`,
  CASE WHEN in_rpt THEN 'Yes' WHEN removed_in_rpt THEN 'Deleted' ELSE 'No' END   AS `In Reporting`,
  rh_status                                                         AS `Status (Rhapsody)`,
  rp_status                                                         AS `Status (Reporting)`,
  sub_type                                                          AS `Submission type`,
  office                                                            AS `Organization (bundle)`
FROM e
""",
        NEW_IN_RHAP=_recent("coalesce(rh_created, rh_received)"),
        REC_DATE=_first_plausible("rh_received", "rh_created", "rp_received", "rp_created", "load_date"),
    ),
)


RHAP_BUNDLE_DETAILS = Check(
    id="RPT_RHAP_BUNDLE_DETAILS_DIFFER",
    title="Bundle details differ: Rhapsody vs Reporting",
    systems=("rhapsody", "edw"),
    entity="Bundles",
    dimension="Consistency",
    severity="High",
    question="For bundles in both systems, do the status, dates, name, organization and submission type in "
             "Reporting match Rhapsody?",
    why="Deadline tracking and review reports rely on bundle status and goal dates. If Reporting shows an old "
        "status or goal date, managers see the wrong deadlines.",
    how_to_fix="Open the bundle in Rhapsody and compare it with the values shown. Rhapsody is the system of "
               "record. If Rhapsody is right, send the bundle ID to the Reporting (EDW) team with the fields "
               "that differ. If Rhapsody changed today, re-check after the overnight refresh.",
    record_label="bundles",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Bundle received date",
    warn_below=0.99,
    fail_below=0.95,
    technical_note=(
        "slvr_ctp_core.rhapsody_bundle INNER JOIN gold_ctp_edw.dim_bundle on NUM_KEY(bundle ID), "
        "both de-duplicated, active (not deleted by either flag), not temporary copies; Reporting "
        "halo_is_current = true. Compared null-safely: bundlestatus/bundle_status, bundlename/bundle_name, "
        "organization and stdz_submission_type with TXT(); datereceivedstart/date_received_start as dates, "
        "exactly (<=>, no tolerance); goaldate/goal_date as dates with a +/-1 day tolerance for a possible "
        "time-zone conversion. Confirmed on real data (Oct 2026): received dates carry no time-zone shift "
        "(eSUB stores DATE_RECEIVED at midnight, and eSUB and Rhapsody received dates are the same calendar "
        "date for 100% of the 43,906 STNs in both, where a UTC shift would put 86% one day apart; P3.1, P3.2), "
        "so the received-date tolerance was removed. Confirmed on real data (Oct 2026): the bundle ID matches "
        "100% between the two systems (K37), and organization is the applicant company stewards see as "
        "Organization in Rhapsody (P2.17), not a CTP office. A value present on one side and blank on the "
        "other counts as a difference. record_date = first plausible of Rhapsody received, Rhapsody created, "
        "Reporting received, else the first load date."
    ),
    assumptions=(
        "Reporting copies these bundle fields from Rhapsody unchanged.",
        "A time-zone conversion in Reporting may still shift goal dates by up to one day. Larger gaps are real "
        "differences.",
    ),
    sql=_sql("WITH", _RH_BUNDLES, r""",
m AS (
  SELECT
    rhb.bundle_id,
    rhb.load_date,
    rhb.bundle_name                    AS rh_name,    rpb.bundle_name                    AS rp_name,
    rhb.bundle_status                  AS rh_status,  rpb.bundle_status                  AS rp_status,
    CAST(rhb.goal_date AS DATE)        AS rh_goal,    CAST(rpb.goal_date AS DATE)        AS rp_goal,
    CAST(rhb.received_start AS DATE)   AS rh_recv,    CAST(rpb.received_start AS DATE)   AS rp_recv,
    CAST(rhb.created_ts AS DATE)       AS rh_created,
    rhb.organization                   AS rh_org,     rpb.organization                   AS rp_org,
    rhb.stdz_submission_type           AS rh_type,    rpb.stdz_submission_type           AS rp_type
  FROM rhb
  JOIN rpb ON rhb.bundle_key = rpb.bundle_key
  WHERE NOT rhb.is_deleted AND NOT rhb.is_temp AND NOT rpb.is_deleted
),
d AS (
  SELECT
    bundle_id, load_date, rh_name, rp_name, rh_status, rp_status, rh_goal, rp_goal, rh_recv, rp_recv,
    rh_created, rh_org, rp_org, rh_type, rp_type,
    NOT (TXT(rh_status) <=> TXT(rp_status))      AS d_status,
    CASE WHEN rh_goal IS NULL AND rp_goal IS NULL THEN false
         WHEN rh_goal IS NULL OR rp_goal IS NULL THEN true
         ELSE abs(datediff(rp_goal, rh_goal)) > 1 END AS d_goal,
    NOT (rh_recv <=> rp_recv)                    AS d_recv,
    NOT (TXT(rh_name) <=> TXT(rp_name))          AS d_name,
    NOT (TXT(rh_org) <=> TXT(rp_org))            AS d_org,
    NOT (TXT(rh_type) <=> TXT(rp_type))          AS d_type
  FROM m
),
n AS (
  SELECT
    d.*,
    CAST(d_status AS INT) + CAST(d_goal AS INT) + CAST(d_recv AS INT)
      + CAST(d_name AS INT) + CAST(d_org AS INT) + CAST(d_type AS INT) AS n_diff
  FROM d
)
SELECT
  coalesce(bundle_id, '(no bundle ID)')                             AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(rh_type AS STRING)                                           AS submission_type,
  CAST(rh_org AS STRING)                                            AS office,
  coalesce(n_diff > 0, false)                                       AS is_issue,
  CASE
    WHEN n_diff = 0 THEN NULL
    WHEN n_diff > 1 THEN 'Several details differ'
    WHEN d_status   THEN 'Status differs'
    WHEN d_goal     THEN 'Goal date differs'
    WHEN d_recv     THEN 'Received date differs'
    WHEN d_name     THEN 'Bundle name differs'
    WHEN d_org      THEN 'Organization differs'
    ELSE 'Submission type differs'
  END                                                               AS issue_category,
  CASE WHEN n_diff > 0 THEN concat_ws(' ',
    CASE WHEN d_status THEN concat('Status is ', coalesce(concat('"', rh_status, '"'), 'blank'),
                                   ' in Rhapsody but ', coalesce(concat('"', rp_status, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_goal THEN concat('Goal date is ', coalesce(CAST(rh_goal AS STRING), 'blank'), ' in Rhapsody but ',
                                 coalesce(CAST(rp_goal AS STRING), 'blank'), ' in Reporting',
                                 coalesce(concat(' (', CAST(abs(datediff(rp_goal, rh_goal)) AS STRING), ' days apart)'), ''),
                                 '.') END,
    CASE WHEN d_recv THEN concat('Received date is ', coalesce(CAST(rh_recv AS STRING), 'blank'), ' in Rhapsody but ',
                                 coalesce(CAST(rp_recv AS STRING), 'blank'), ' in Reporting.') END,
    CASE WHEN d_name THEN concat('Name is ', coalesce(concat('"', rh_name, '"'), 'blank'),
                                 ' in Rhapsody but ', coalesce(concat('"', rp_name, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_org THEN concat('Organization is ', coalesce(concat('"', rh_org, '"'), 'blank'),
                                   ' in Rhapsody but ', coalesce(concat('"', rp_org, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_type THEN concat('Submission type is ', coalesce(concat('"', rh_type, '"'), 'blank'),
                                 ' in Rhapsody but ', coalesce(concat('"', rp_type, '"'), 'blank'), ' in Reporting.') END
  ) END                                                             AS issue_reason,
  bundle_id                                                         AS `Bundle ID`,
  rh_name                                                           AS `Bundle name`,
  concat_ws(', ',
    CASE WHEN d_status THEN 'Status' END, CASE WHEN d_goal THEN 'Goal date' END,
    CASE WHEN d_recv THEN 'Received date' END, CASE WHEN d_name THEN 'Bundle name' END,
    CASE WHEN d_org THEN 'Organization' END, CASE WHEN d_type THEN 'Submission type' END) AS `Fields that differ`,
  rh_status                                                         AS `Status (Rhapsody)`,
  rp_status                                                         AS `Status (Reporting)`,
  rh_goal                                                           AS `Goal date (Rhapsody)`,
  rp_goal                                                           AS `Goal date (Reporting)`,
  rh_recv                                                           AS `Received (Rhapsody)`,
  rp_recv                                                           AS `Received (Reporting)`,
  rh_org                                                            AS `Organization (Rhapsody)`,
  rp_org                                                            AS `Organization (Reporting)`
FROM n
""",
        REC_DATE=_first_plausible("rh_recv", "rh_created", "rp_recv", "load_date"),
    ),
)


RHAP_PRODUCT_COUNTS = Check(
    id="RPT_RHAP_SUBMISSION_PRODUCT_COUNTS_DIFFER",
    title="Product counts differ: Rhapsody vs Reporting",
    systems=("rhapsody", "edw"),
    entity="Products",
    dimension="Completeness",
    severity="High",
    question="Does each submission have the same number of products in Reporting as in Rhapsody?",
    why="Product counts per submission are an official CTP metric and drive workload reports. When Reporting "
        "has missing or extra products, every count that uses them is wrong.",
    how_to_fix="Open the submission in Rhapsody and count its products, including any bulk product list. If "
               "Rhapsody has products attached by mistake, correct them in Rhapsody. If the submission itself "
               "is missing from Reporting, fix that first using the submissions check. If Rhapsody is right, "
               "send the STN with both counts to the Reporting (EDW) team. If someone added products in "
               "Rhapsody today, re-check after the overnight refresh. Products that share a placeholder ID "
               "are left out of both counts, and the number left out is shown.",
    record_label="submissions",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Received date",
    warn_below=0.99,
    fail_below=0.95,
    technical_note=(
        "Rhapsody: distinct ANY_KEY(product_id) per STN_KEY(stn) from slvr_ctp_core.rhapsody_submission_products "
        "('listed') plus slvr_ctp_core.rhapsody_bulk_products ('bulk'), both halo_is_deleted = false. "
        "Reporting: each gold_ctp_edw.fact_bundle_submission_product row (halo_is_current AND NOT "
        "halo_is_deleted) is translated from its product_id to the product's dim_product.uuid (one current "
        "dim_product row per NUM_KEY(id), non-deleted preferred, so the lookup cannot multiply rows), and "
        "distinct ANY_KEY(uuid) are counted per STN; a fact whose product has no uuid counts under its own "
        "product_id (fallback product_sk). The STN is looked up from dim_submission by submission_sk (any "
        "version, one row per submission_sk, because facts may point at an older version's key; K61: 100% "
        "found). Confirmed on real data (Oct 2026): Rhapsody product IDs are PMS uuids (K24, K50: 100%), "
        "Reporting product IDs are PMS record numbers (K34-K36: 100%), and Rhapsody bulk IDs match "
        "dim_product.id 0% (K64), so both sides are counted in the uuid space and dim_product.id is never "
        "compared with a Rhapsody product ID. Confirmed on real data (Oct 2026): one placeholder value sits on "
        "14,359 Rhapsody product rows and one PMS uuid on 14,368 products (P4 U07, U10). CTE placeholder_ids "
        "finds, on every run, any ANY_KEY(product_id) on more than " + str(PLACEHOLDER_ID_ROWS) + " live rows "
        "of rhapsody_products + rhapsody_bulk_products, and any ANY_KEY(uuid) on more than "
        + str(PLACEHOLDER_ID_ROWS) + " current, non-deleted dim_product rows (one row per value, so the "
        "lookup cannot multiply rows). Those products are left out of both counts, so ~14k products never "
        "collapse into one; the number left out on each side is shown. FULL OUTER JOIN of the two per-STN "
        "counts. Passes when the Reporting count equals either the listed count or listed + bulk, because it "
        "is not confirmed whether Reporting includes bulk-list products. STNs whose Rhapsody submission is "
        "soft-deleted are left out (covered by the submissions check), as are submissions created (else "
        "received) in Rhapsody in the last 2 days. 'Submission missing' = Rhapsody has products but the STN "
        "has no current, non-deleted dim_submission row. Fact rows whose submission_sk has no dim_submission "
        "row cannot be tied to an STN and are not counted. The STN shown falls back to the product list's own "
        "STN when the submission record is missing."
    ),
    assumptions=(
        "Reporting takes the products on each submission from the Rhapsody product list and bulk product lists.",
        "It is not confirmed whether Reporting counts bulk-list products, so either count passes.",
        "Inactive entries in bulk product lists still count. The meaning of the bulk list's active field is "
        "not yet confirmed.",
        f"A product ID shared by more than {PLACEHOLDER_ID_ROWS} product records is a placeholder, not one "
        "product. Products that carry it are left out of both counts.",
    ),
    sql=_sql("WITH", _RH_SUBMISSIONS, r""",
placeholder_ids AS (
  -- A product ID or uuid on more than @PH_ROWS@ product records is a shared placeholder (P4 U07, U10).
  SELECT product_key
  FROM (
    SELECT ANY_KEY(product_id) AS product_key
    FROM ${core}.rhapsody_products
    WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT ANY_KEY(product_id) AS product_key
    FROM ${core}.rhapsody_bulk_products
    WHERE coalesce(halo_is_deleted, false) = false
  ) rhap_ids
  WHERE product_key IS NOT NULL
  GROUP BY product_key
  HAVING count(*) > @PH_ROWS@
  UNION
  SELECT ANY_KEY(uuid) AS product_key
  FROM ${edw}.dim_product
  WHERE halo_is_current = true
    AND coalesce(halo_is_deleted, false) = false
    AND ANY_KEY(uuid) IS NOT NULL
  GROUP BY ANY_KEY(uuid)
  HAVING count(*) > @PH_ROWS@
),
rhl_rows AS (
  SELECT STN_KEY(stn) AS stn_key, BLANK(stn) AS stn, ANY_KEY(product_id) AS product_key,
         CAST(halo_insert_ts AS DATE) AS load_date, false AS is_bulk
  FROM ${core}.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
  UNION ALL
  SELECT STN_KEY(stn) AS stn_key, BLANK(stn) AS stn, ANY_KEY(product_id) AS product_key,
         CAST(halo_insert_ts AS DATE) AS load_date, true AS is_bulk
  FROM ${core}.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhl AS (
  SELECT r.stn_key, r.stn, r.product_key, r.load_date, r.is_bulk,
         ph.product_key IS NOT NULL                            AS is_placeholder
  FROM rhl_rows r
  LEFT JOIN placeholder_ids ph ON ph.product_key = r.product_key
  WHERE r.stn_key IS NOT NULL AND r.product_key IS NOT NULL
),
rhc AS (
  SELECT
    stn_key,
    max(stn)                                                                        AS stn,
    min(load_date)                                                                  AS load_date,
    count(DISTINCT CASE WHEN NOT is_bulk AND NOT is_placeholder THEN product_key END) AS n_listed,
    count(DISTINCT CASE WHEN is_bulk AND NOT is_placeholder THEN product_key END)     AS n_bulk,
    count(DISTINCT CASE WHEN NOT is_placeholder THEN product_key END)                AS n_all,
    count_if(is_placeholder)                                                         AS n_ph
  FROM rhl
  GROUP BY stn_key
),
sk AS (
  SELECT submission_sk, max(STN_KEY(stn)) AS stn_key, max(BLANK(stn)) AS stn, max(date_received) AS date_received
  FROM ${edw}.dim_submission
  WHERE submission_sk IS NOT NULL
  GROUP BY submission_sk
),
rps AS (
  SELECT DISTINCT STN_KEY(stn) AS stn_key
  FROM ${edw}.dim_submission
  WHERE halo_is_current = true
    AND coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) IS NOT NULL
),
rp_prod AS (
  -- Reporting product ID (= PMS record number) -> PMS uuid, the ID Rhapsody uses. One row per ID.
  SELECT NUM_KEY(id) AS id_key, ANY_KEY(uuid) AS uuid_key
  FROM ${edw}.dim_product
  WHERE halo_is_current = true
    AND NUM_KEY(id) IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(id)
                             ORDER BY CASE WHEN coalesce(halo_is_deleted, false) THEN 1 ELSE 0 END,
                                      coalesce(halo_modified_ts, halo_created_ts) DESC NULLS LAST) = 1
),
rpl AS (
  SELECT
    sk.stn_key,
    sk.stn,
    sk.date_received,
    CAST(f.halo_created_ts AS DATE)                                                 AS load_date,
    coalesce(NUM_KEY(f.product_id), concat('#', CAST(f.product_sk AS STRING)))      AS rp_product_id,
    coalesce(p.uuid_key, concat('#', coalesce(NUM_KEY(f.product_id),
                                              concat('sk', CAST(f.product_sk AS STRING))))) AS product_key,
    ph.product_key IS NOT NULL                                                      AS is_placeholder
  FROM ${edw}.fact_bundle_submission_product f
  JOIN sk ON f.submission_sk = sk.submission_sk
  LEFT JOIN rp_prod p          ON p.id_key = NUM_KEY(f.product_id)
  LEFT JOIN placeholder_ids ph ON ph.product_key = p.uuid_key
  WHERE f.halo_is_current = true
    AND coalesce(f.halo_is_deleted, false) = false
    AND sk.stn_key IS NOT NULL
),
rpc AS (
  SELECT
    stn_key,
    max(stn)                                                                AS stn,
    max(date_received)                                                      AS date_received,
    min(load_date)                                                          AS load_date,
    count(DISTINCT CASE WHEN NOT is_placeholder THEN product_key END)       AS n_rpt,
    count(DISTINCT CASE WHEN is_placeholder THEN rp_product_id END)         AS n_ph
  FROM rpl
  GROUP BY stn_key
),
j AS (
  SELECT
    coalesce(rhc.stn_key, rpc.stn_key)  AS stn_key,
    rhc.stn                             AS list_stn,
    coalesce(rhc.load_date, rpc.load_date) AS list_load_date,
    rpc.stn                             AS rp_stn,
    rpc.date_received                   AS rp_received,
    coalesce(rhc.n_listed, 0)           AS n_listed,
    coalesce(rhc.n_bulk, 0)             AS n_bulk,
    coalesce(rhc.n_all, 0)              AS n_all,
    coalesce(rpc.n_rpt, 0)              AS n_rpt,
    coalesce(rhc.n_ph, 0)               AS n_ph_rh,
    coalesce(rpc.n_ph, 0)               AS n_ph_rp
  FROM rhc
  FULL OUTER JOIN rpc ON rhc.stn_key = rpc.stn_key
),
e AS (
  SELECT
    coalesce(rh.stn, j.list_stn, j.rp_stn)             AS stn,
    rh.company,
    rh.stdz_submission_type                            AS sub_type,
    rh.organization                                    AS office,
    rh.date_received                                   AS rh_received,
    rh.date_created                                    AS rh_created,
    j.rp_received,
    coalesce(rh.load_date, j.list_load_date)           AS load_date,
    rps.stn_key IS NOT NULL                            AS sub_in_rpt,
    j.n_listed, j.n_bulk, j.n_all, j.n_rpt, j.n_ph_rh, j.n_ph_rp,
    NOT (j.n_rpt = j.n_all OR j.n_rpt = j.n_listed)    AS differs
  FROM j
  LEFT JOIN rh  ON j.stn_key = rh.stn_key
  LEFT JOIN rps ON j.stn_key = rps.stn_key
  WHERE NOT coalesce(rh.is_deleted, false)
    AND NOT @NEW_IN_RHAP@
)
SELECT
  coalesce(stn, '(no STN)')                                         AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(differs, false)                                          AS is_issue,
  CASE
    WHEN NOT differs THEN NULL
    WHEN n_all = 0   THEN 'Only in Reporting'
    WHEN n_rpt = 0 AND NOT sub_in_rpt THEN 'Submission missing'
    WHEN n_rpt = 0   THEN 'None in Reporting'
    WHEN n_rpt < n_all THEN 'Fewer in Reporting'
    ELSE 'More in Reporting'
  END                                                               AS issue_category,
  CASE WHEN differs THEN
    concat('Rhapsody has ', CAST(n_all AS STRING), ' product(s)',
           CASE WHEN n_bulk > 0 THEN concat(' (', CAST(n_listed AS STRING), ' listed, ',
                                            CAST(n_bulk AS STRING), ' in bulk lists)') ELSE '' END,
           CASE WHEN n_rpt = 0 AND NOT sub_in_rpt
                THEN ' but the submission is not in Reporting.'
                ELSE concat(' but Reporting has ', CAST(n_rpt AS STRING), '.') END,
           CASE WHEN n_ph_rh + n_ph_rp > 0
                THEN concat(' Left out of both counts because they share a placeholder ID: ',
                            CAST(n_ph_rh AS STRING), ' Rhapsody product row(s) and ',
                            CAST(n_ph_rp AS STRING), ' Reporting product(s).')
                ELSE '' END)
  END                                                               AS issue_reason,
  stn                                                               AS `STN`,
  company                                                           AS `Company`,
  n_listed                                                          AS `Listed (Rhapsody)`,
  n_bulk                                                            AS `Bulk lists (Rhapsody)`,
  n_rpt                                                             AS `Products (Reporting)`,
  n_rpt - n_all                                                     AS `Reporting minus Rhapsody`,
  CASE WHEN n_ph_rh + n_ph_rp > 0
       THEN concat(CAST(n_ph_rh AS STRING), ' in Rhapsody, ', CAST(n_ph_rp AS STRING), ' in Reporting')
  END                                                               AS `Left out (placeholder ID)`,
  CASE WHEN sub_in_rpt THEN 'Yes' ELSE 'No' END                     AS `STN in Reporting`,
  coalesce(rh_received, rp_received)                                AS `Received date`,
  sub_type                                                          AS `Submission type`
FROM e
""",
        NEW_IN_RHAP=_recent("coalesce(rh.date_created, rh.date_received)"),
        REC_DATE=_first_plausible("rh_received", "rp_received", "rh_created", "load_date"),
        PH_ROWS=str(PLACEHOLDER_ID_ROWS),
    ),
)


PMS_PRODUCTS_MISMATCH = Check(
    id="RPT_PMS_PRODUCTS_NOT_IN_BOTH",
    title="Products missing from PMS or Reporting",
    systems=("pms", "edw"),
    entity="Products",
    dimension="Completeness",
    severity="High",
    question="Does every active PMS product appear in Reporting, and does Reporting show only products still "
             "active in PMS?",
    why="Official product counts by category and company come from Reporting. A product missing there drops "
        "out of the counts. A product removed from PMS but still in Reporting gets counted by mistake.",
    how_to_fix="Search PMS for the product ID or TP number. If it is active in PMS but missing from Reporting, "
               "send the product ID to the Reporting (EDW) team. If someone removed it from PMS on purpose, ask "
               "that team to remove it from Reporting too.",
    record_label="products",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Product created date",
    warn_below=0.999,
    fail_below=0.99,
    technical_note=(
        "slvr_ctp_core.pms_product (id) FULL OUTER JOIN gold_ctp_edw.dim_product (id), matched with NUM_KEY(); "
        "both de-duplicated to one row per id (active row preferred, then latest load). Active = "
        "halo_is_deleted false (pms_product has no source deleted flag); Reporting also halo_is_current true. "
        "Confirmed on real data (Oct 2026): Reporting's product ID is the PMS record number (K34: 100% of the "
        "26,687,185 current Reporting products are PMS products), bulk products included (K63: 100% of the "
        "24,412,579 marked is_bulk_product; P2.24), so every Reporting product is expected in PMS and the "
        "former exemption for Reporting-only bulk products was removed. The match is on id, which is unique, "
        "so the shared placeholder uuid (P4 U10) cannot collapse products here. Products created in PMS in "
        "the last 2 days and not yet in Reporting are not evaluated. record_date = first plausible of PMS "
        "create_date, Reporting create_date, else the first load date. Submission type / organization = the "
        "Rhapsody record (any state) of the product's STN (STN_KEY(stdz_stn)), one row per STN, so no "
        "fan-out; products whose STN is not in Rhapsody show as '(not recorded)' (K30: 59.8% of PMS STNs are "
        "in Rhapsody)."
    ),
    assumptions=(),
    sql=_sql("WITH", _RH_SUBMISSIONS, ",", _PMS_PRODUCTS, r""",
j AS (
  SELECT
    coalesce(pm.product_id, rpp.product_id)                              AS product_id,
    coalesce(pm.product_name, rpp.product_name)                          AS product_name,
    coalesce(pm.tp_number, rpp.tp_number)                                AS tp_number,
    coalesce(pm.stdz_stn, rpp.stdz_stn)                                  AS stn,
    coalesce(pm.product_key IS NOT NULL AND NOT pm.is_deleted, false)    AS in_pms,
    coalesce(pm.is_deleted, false)                                       AS removed_in_pms,
    coalesce(rpp.product_key IS NOT NULL AND NOT rpp.is_deleted, false)  AS in_rpt,
    coalesce(rpp.is_deleted, false)                                      AS removed_in_rpt,
    pm.removed_ts                                                        AS pm_removed_ts,
    pm.create_date                                                       AS pm_created,
    rpp.create_date                                                      AS rp_created,
    coalesce(pm.load_date, rpp.load_date)                                AS load_date
  FROM pm
  FULL OUTER JOIN rpp ON pm.product_key = rpp.product_key
),
e AS (
  SELECT
    j.product_id, j.product_name, j.tp_number, j.stn, j.in_pms, j.removed_in_pms, j.in_rpt, j.removed_in_rpt,
    j.pm_removed_ts, j.pm_created, j.rp_created, j.load_date,
    rh.stdz_submission_type                                              AS sub_type,
    rh.organization                                                      AS office,
    CASE
      WHEN j.in_pms AND NOT j.in_rpt THEN 'Missing from Reporting'
      WHEN j.in_rpt AND NOT j.in_pms AND j.removed_in_pms THEN 'Still in Reporting'
      WHEN j.in_rpt AND NOT j.in_pms THEN 'Only in Reporting'
    END AS category
  FROM j
  LEFT JOIN rh ON STN_KEY(j.stn) = rh.stn_key
  WHERE (j.in_pms AND (j.in_rpt OR NOT @NEW_IN_PMS@))
     OR (j.in_rpt AND NOT j.in_pms)
)
SELECT
  coalesce(product_id, '(no product ID)')                           AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(category IS NOT NULL, false)                             AS is_issue,
  category                                                          AS issue_category,
  CASE category
    WHEN 'Missing from Reporting' THEN
      concat('Active in PMS (created ', coalesce(CAST(CAST(pm_created AS DATE) AS STRING), 'date blank'), ') but ',
             CASE WHEN removed_in_rpt THEN 'marked removed' ELSE 'not found' END, ' in Reporting.')
    WHEN 'Still in Reporting' THEN
      concat('Removed from PMS',
             coalesce(concat(' on ', CAST(CAST(pm_removed_ts AS DATE) AS STRING)), ''),
             ' but still active in Reporting.')
    WHEN 'Only in Reporting' THEN
      concat('Active in Reporting but product ID ', product_id, ' is not in PMS.')
  END                                                               AS issue_reason,
  product_id                                                        AS `Product ID`,
  tp_number                                                         AS `TP number`,
  product_name                                                      AS `Product name`,
  stn                                                               AS `STN`,
  CASE WHEN in_pms THEN 'Yes' WHEN removed_in_pms THEN 'Removed' ELSE 'No' END AS `In PMS`,
  CASE WHEN in_rpt THEN 'Yes' WHEN removed_in_rpt THEN 'Removed' ELSE 'No' END AS `In Reporting`,
  CAST(coalesce(pm_created, rp_created) AS DATE)                    AS `Created date`,
  sub_type                                                          AS `Submission type (Rhapsody)`,
  office                                                            AS `Organization (Rhapsody)`
FROM e
""",
        NEW_IN_PMS=_recent("j.pm_created"),
        REC_DATE=_first_plausible("pm_created", "rp_created", "load_date"),
    ),
)


PMS_PRODUCT_DETAILS = Check(
    id="RPT_PMS_PRODUCT_DETAILS_DIFFER",
    title="Product details differ: PMS vs Reporting",
    systems=("pms", "edw"),
    entity="Products",
    dimension="Consistency",
    severity="Medium",
    question="For products in both systems, do the name, TP number, STN, category and subcategory in "
             "Reporting match PMS?",
    why="Reports group products by category and find them by STN or TP number. When Reporting holds an old or "
        "different value, counts by category go wrong. Searches miss the product too.",
    how_to_fix="Open the product in PMS and compare it with the values shown. PMS is the system of record. If "
               "PMS is right, send the product ID to the Reporting (EDW) team with the fields that differ. If "
               "someone corrected PMS today, re-check after the overnight refresh.",
    record_label="products",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Product created date",
    warn_below=0.99,
    fail_below=0.95,
    technical_note=(
        "slvr_ctp_core.pms_product (+ pms_unique_product via unique_product_id for ctgry_code / subctgry_code) "
        "INNER JOIN gold_ctp_edw.dim_product on NUM_KEY(id). Confirmed on real data (Oct 2026): Reporting's "
        "product ID is the PMS record number (K34: 100%) and every product has its unique-product record "
        "(K31: 100%). All three de-duplicated to one row per ID "
        "and limited to active rows (halo_is_deleted false; Reporting also halo_is_current true). Compared "
        "null-safely with TXT(): product_name, tp_number, ctgry_code, subctgry_code; stdz_stn with STN_KEY(). "
        "Category and subcategory are compared only when the product's unique-product record was found, or "
        "the product has no unique-product link at all (then PMS's category is blank); a link to a "
        "unique-product record missing from the copy is not judged here. Blank and missing count as the same. "
        "record_date = first plausible PMS create_date, else the first load date. Submission type / "
        "organization = the Rhapsody record of the product's PMS STN (one row per STN, so rows do not multiply)."
    ),
    assumptions=(
        "Reporting's category and subcategory codes come from the PMS unique-product record linked to the product.",
        "Reporting copies these fields from PMS unchanged.",
    ),
    sql=_sql("WITH", _RH_SUBMISSIONS, ",", _PMS_PRODUCTS, r""",
up AS (
  SELECT NUM_KEY(id) AS unique_key, BLANK(ctgry_code) AS ctgry_code, BLANK(subctgry_code) AS subctgry_code
  FROM ${core}.pms_unique_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(id) IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(id)
                             ORDER BY coalesce(halo_update_ts, halo_insert_ts) DESC NULLS LAST) = 1
),
m AS (
  SELECT
    pm.product_id,
    pm.create_date,
    pm.load_date,
    pm.unique_key IS NULL OR up.unique_key IS NOT NULL AS cat_known,
    rh.stdz_submission_type AS sub_type,
    rh.organization    AS office,
    pm.product_name    AS pm_name,    rpp.product_name   AS rp_name,
    pm.tp_number       AS pm_tp,      rpp.tp_number      AS rp_tp,
    pm.stdz_stn        AS pm_stn,     rpp.stdz_stn       AS rp_stn,
    up.ctgry_code      AS pm_cat,     rpp.ctgry_code     AS rp_cat,
    up.subctgry_code   AS pm_subcat,  rpp.subctgry_code  AS rp_subcat
  FROM pm
  JOIN rpp ON pm.product_key = rpp.product_key
  LEFT JOIN up ON pm.unique_key = up.unique_key
  LEFT JOIN rh ON STN_KEY(pm.stdz_stn) = rh.stn_key
  WHERE NOT pm.is_deleted AND NOT rpp.is_deleted
),
d AS (
  SELECT
    product_id, create_date, load_date, sub_type, office, pm_name, rp_name, pm_tp, rp_tp, pm_stn, rp_stn,
    pm_cat, rp_cat, pm_subcat, rp_subcat,
    NOT (TXT(pm_name) <=> TXT(rp_name))                      AS d_name,
    NOT (TXT(pm_tp) <=> TXT(rp_tp))                          AS d_tp,
    NOT (STN_KEY(pm_stn) <=> STN_KEY(rp_stn))                AS d_stn,
    cat_known AND NOT (TXT(pm_cat) <=> TXT(rp_cat))          AS d_cat,
    cat_known AND NOT (TXT(pm_subcat) <=> TXT(rp_subcat))    AS d_subcat
  FROM m
),
n AS (
  SELECT
    d.*,
    CAST(d_name AS INT) + CAST(d_tp AS INT) + CAST(d_stn AS INT) + CAST(d_cat AS INT) + CAST(d_subcat AS INT) AS n_diff
  FROM d
)
SELECT
  coalesce(product_id, '(no product ID)')                           AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(n_diff > 0, false)                                       AS is_issue,
  CASE
    WHEN n_diff = 0 THEN NULL
    WHEN n_diff > 1 THEN 'Several details differ'
    WHEN d_name     THEN 'Product name differs'
    WHEN d_tp       THEN 'TP number differs'
    WHEN d_stn      THEN 'STN differs'
    WHEN d_cat      THEN 'Category differs'
    ELSE 'Subcategory differs'
  END                                                               AS issue_category,
  CASE WHEN n_diff > 0 THEN concat_ws(' ',
    CASE WHEN d_name THEN concat('Product name is ', coalesce(concat('"', pm_name, '"'), 'blank'),
                                 ' in PMS but ', coalesce(concat('"', rp_name, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_tp THEN concat('TP number is ', coalesce(concat('"', pm_tp, '"'), 'blank'),
                               ' in PMS but ', coalesce(concat('"', rp_tp, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_stn THEN concat('STN is ', coalesce(concat('"', pm_stn, '"'), 'blank'),
                                ' in PMS but ', coalesce(concat('"', rp_stn, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_cat THEN concat('Category is ', coalesce(concat('"', pm_cat, '"'), 'blank'),
                                ' in PMS but ', coalesce(concat('"', rp_cat, '"'), 'blank'), ' in Reporting.') END,
    CASE WHEN d_subcat THEN concat('Subcategory is ', coalesce(concat('"', pm_subcat, '"'), 'blank'),
                                   ' in PMS but ', coalesce(concat('"', rp_subcat, '"'), 'blank'), ' in Reporting.') END
  ) END                                                             AS issue_reason,
  product_id                                                        AS `Product ID`,
  concat_ws(', ',
    CASE WHEN d_name THEN 'Product name' END, CASE WHEN d_tp THEN 'TP number' END,
    CASE WHEN d_stn THEN 'STN' END, CASE WHEN d_cat THEN 'Category' END,
    CASE WHEN d_subcat THEN 'Subcategory' END)                      AS `Fields that differ`,
  pm_name                                                           AS `Name (PMS)`,
  rp_name                                                           AS `Name (Reporting)`,
  pm_tp                                                             AS `TP number (PMS)`,
  rp_tp                                                             AS `TP number (Reporting)`,
  pm_stn                                                            AS `STN (PMS)`,
  rp_stn                                                            AS `STN (Reporting)`,
  pm_cat                                                            AS `Category (PMS)`,
  rp_cat                                                            AS `Category (Reporting)`,
  pm_subcat                                                         AS `Subcategory (PMS)`,
  rp_subcat                                                         AS `Subcategory (Reporting)`
FROM n
""",
        REC_DATE=_first_plausible("create_date", "load_date"),
    ),
)


ESUB_LINKS_MISSING = Check(
    id="RPT_ESUB_LINKS_MISSING_FROM_REPORTING",
    title="eSUB submission links missing from Reporting",
    systems=("esub", "edw"),
    entity="Submission links",
    dimension="Completeness",
    severity="Medium",
    question="Does every eSUB link between two submissions (such as an amendment and its original) also "
             "appear in Reporting?",
    why="Reports use these links to tie amendments to the original submission and to count amendments per "
        "application. A missing link makes an amendment look unrelated. It also under-counts the work on the "
        "original.",
    how_to_fix="Find both STNs in eSUB and confirm the link is correct. If the reason says a submission that "
               "is active in Rhapsody is missing from Reporting, fix that first using the submissions checks. "
               "Otherwise, send both STNs to the Reporting (EDW) team. A submission that is not active in "
               "Rhapsody is not expected in Reporting's submission list, but its links still are. Link types "
               "that Reporting leaves out by design are not checked.",
    record_label="links",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Link created date",
    technical_note=(
        "slvr_ctp_core.esub_itrac_sub_links (halo_is_deleted false), one row per (STN_KEY(STN), "
        "STN_KEY(CHILD_STN)) pair, self-links dropped. Link rows whose ANY_KEY(DESCRIPTOR_CODE) is one of "
        + "/".join(_ESUB_LINK_TYPES_NOT_IN_REPORTING) + " (_ESUB_LINK_TYPES_NOT_IN_REPORTING) are dropped "
        "before pairing, because Reporting does not load those types by design. Confirmed on real data "
        "(Oct 2026): type 12 = 1 of 1,363 links found in Reporting, 15 = 0 of 2, 8 = 9 of 36; every other "
        "type is 96-100% found (P2.10), and 98.4% of eSUB link pairs are found overall (K62). A pair that also "
        "carries another link type is still evaluated; rows with a blank link type are kept. Matched to "
        "gold_ctp_edw.dim_submission_relationships "
        "(halo_is_current AND NOT halo_is_deleted) as an unordered pair against both (original_stn, related_stn) "
        "and (through_stn, related_stn). A link therefore matches in either direction and at any level. "
        "Presence of each STN in Reporting is from dim_submission (current, not deleted) and only sets the "
        "category. Confirmed on real data (Oct 2026): Reporting's submission list is exactly Rhapsody's (K32: "
        "100%; P2.29) and eSUB-only STNs are not expected there (K33: 39% of active eSUB log-in STNs are in "
        "Reporting). So an STN missing from dim_submission sets the 'STN missing' categories only when it is "
        "active in Rhapsody (rh, not soft-deleted); any other STN is shown as 'Not expected (not active in "
        "Rhapsody)' and its link is judged on the relationship list alone ('Link missing'). "
        "Links created in the last 2 days and not yet in Reporting are not evaluated. Submission "
        "type / organization come from the Rhapsody record of the child STN (fallback parent); links whose STNs are "
        "not in Rhapsody show as '(not recorded)'. esub_itrac_sub_links has no deleted/inactive indicator of "
        "its own, and links to inactive submissions are kept because Reporting's relationship list carries "
        "inactive indicators too. record_date = first plausible CREATE_DATE, else the first load date. Covers "
        "eSUB to Reporting only; Reporting links not in eSUB are not evaluated because Reporting also derives "
        "multi-level links."
    ),
    assumptions=(
        "In eSUB the STN is the parent submission and the child STN is the amendment or other linked "
        "submission.",
        "Reporting's link list also holds links to submissions that never reached Rhapsody, so those links "
        "are still checked.",
    ),
    sql=_sql("WITH", _RH_SUBMISSIONS, r""",
link_rows AS (
  SELECT
    STN_KEY(STN)             AS parent_key,
    STN_KEY(CHILD_STN)       AS child_key,
    BLANK(STN)               AS parent_stn,
    BLANK(CHILD_STN)         AS child_stn,
    CREATE_DATE              AS created_ts,
    halo_insert_ts           AS first_loaded_ts,
    BLANK(DESCRIPTOR_CODE)   AS link_type
  FROM ${core}.esub_itrac_sub_links
  WHERE coalesce(halo_is_deleted, false) = false
    -- link types Reporting does not load by design (_ESUB_LINK_TYPES_NOT_IN_REPORTING); blank types are kept
    AND coalesce(ANY_KEY(DESCRIPTOR_CODE) NOT IN (@SKIP_LINK_TYPES@), true)
),
links AS (
  SELECT
    parent_key,
    child_key,
    min(parent_stn)                                              AS parent_stn,
    min(child_stn)                                               AS child_stn,
    min(created_ts)                                              AS created_ts,
    min(first_loaded_ts)                                         AS first_loaded_ts,
    nullif(array_join(sort_array(collect_set(link_type)), ', '), '') AS link_types,
    concat(least(parent_key, child_key), '|', greatest(parent_key, child_key)) AS pair_key
  FROM link_rows
  WHERE parent_key IS NOT NULL AND child_key IS NOT NULL AND parent_key <> child_key
  GROUP BY parent_key, child_key
),
rel AS (
  SELECT DISTINCT concat(least(a, b), '|', greatest(a, b)) AS pair_key
  FROM (
    SELECT STN_KEY(original_stn) AS a, STN_KEY(related_stn) AS b
    FROM ${edw}.dim_submission_relationships
    WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT STN_KEY(through_stn) AS a, STN_KEY(related_stn) AS b
    FROM ${edw}.dim_submission_relationships
    WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
  ) p
  WHERE a IS NOT NULL AND b IS NOT NULL AND a <> b
),
rp_subs AS (
  SELECT DISTINCT STN_KEY(stn) AS stn_key
  FROM ${edw}.dim_submission
  WHERE halo_is_current = true
    AND coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) IS NOT NULL
),
e AS (
  SELECT
    l.parent_stn,
    l.child_stn,
    l.created_ts,
    l.first_loaded_ts,
    l.link_types,
    rel.pair_key IS NOT NULL                                     AS in_rpt,
    rps.stn_key IS NOT NULL                                      AS parent_in_rpt,
    rpc.stn_key IS NOT NULL                                      AS child_in_rpt,
    -- Only STNs active in Rhapsody are expected in Reporting's submission list (K32, K33).
    coalesce(p.stn_key IS NOT NULL AND NOT p.is_deleted, false)  AS parent_expected,
    coalesce(c.stn_key IS NOT NULL AND NOT c.is_deleted, false)  AS child_expected,
    coalesce(c.stdz_submission_type, p.stdz_submission_type)     AS sub_type,
    coalesce(c.organization, p.organization)                     AS office,
    coalesce(p.company, c.company)                               AS company
  FROM links l
  LEFT JOIN rel             ON l.pair_key = rel.pair_key
  LEFT JOIN rp_subs rps     ON l.parent_key = rps.stn_key
  LEFT JOIN rp_subs rpc     ON l.child_key = rpc.stn_key
  LEFT JOIN rh c            ON l.child_key = c.stn_key
  LEFT JOIN rh p            ON l.parent_key = p.stn_key
),
f AS (
  SELECT
    parent_stn, child_stn, created_ts, first_loaded_ts, link_types, parent_in_rpt, child_in_rpt,
    parent_expected, child_expected, sub_type, office, company,
    CASE
      WHEN in_rpt THEN NULL
      WHEN parent_expected AND NOT parent_in_rpt AND child_expected AND NOT child_in_rpt THEN 'Both STNs missing'
      WHEN parent_expected AND NOT parent_in_rpt THEN 'Parent STN missing'
      WHEN child_expected AND NOT child_in_rpt  THEN 'Child STN missing'
      ELSE 'Link missing'
    END AS category
  FROM e
  WHERE in_rpt
     OR NOT coalesce(created_ts BETWEEN current_timestamp() - INTERVAL 2 DAYS AND current_timestamp(), false)
)
SELECT
  concat(coalesce(parent_stn, '(no STN)'), ' > ', coalesce(child_stn, '(no STN)')) AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(category IS NOT NULL, false)                             AS is_issue,
  category                                                          AS issue_category,
  CASE WHEN category IS NOT NULL THEN
    concat('eSUB links ', parent_stn, ' to ', child_stn,
           coalesce(concat(' (link type ', link_types, ')'), ''),
           coalesce(concat(' since ', CAST(CAST(created_ts AS DATE) AS STRING)), ''),
           CASE category
             WHEN 'Link missing' THEN
               CASE WHEN parent_in_rpt AND child_in_rpt
                    THEN '. Both STNs are in Reporting, but the link is not.'
                    WHEN NOT parent_in_rpt AND NOT child_in_rpt
                    THEN concat('. Reporting does not have the link. ', parent_stn, ' and ', child_stn,
                                ' are not active in Rhapsody, so Reporting is not expected to list those submissions.')
                    ELSE concat('. Reporting does not have the link. ',
                                CASE WHEN parent_in_rpt THEN child_stn ELSE parent_stn END,
                                ' is not active in Rhapsody, so Reporting is not expected to list that submission.')
               END
             WHEN 'Child STN missing'
               THEN concat('. Reporting does not have ', child_stn, ', which is active in Rhapsody.')
             WHEN 'Parent STN missing'
               THEN concat('. Reporting does not have ', parent_stn, ', which is active in Rhapsody.')
             ELSE '. Reporting has neither STN, though both are active in Rhapsody.'
           END)
  END                                                               AS issue_reason,
  parent_stn                                                        AS `Parent STN`,
  child_stn                                                         AS `Child STN`,
  link_types                                                        AS `Link type`,
  CAST(created_ts AS DATE)                                          AS `Link created (eSUB)`,
  CASE WHEN parent_in_rpt THEN 'Yes' WHEN parent_expected THEN 'No'
       ELSE 'Not expected (not active in Rhapsody)' END             AS `Parent in Reporting`,
  CASE WHEN child_in_rpt THEN 'Yes' WHEN child_expected THEN 'No'
       ELSE 'Not expected (not active in Rhapsody)' END             AS `Child in Reporting`,
  company                                                           AS `Company`
FROM f
""",
        REC_DATE=_first_plausible("created_ts", "first_loaded_ts"),
        SKIP_LINK_TYPES=", ".join(f"'{t}'" for t in _ESUB_LINK_TYPES_NOT_IN_REPORTING),
    ),
)


RHAP_LETTERS_MISMATCH = Check(
    id="RPT_RHAP_LETTERS_NOT_IN_REPORTING",
    title="Letters missing from Reporting or kept after deletion",
    systems=("rhapsody", "edw"),
    entity="Letters & communications",
    dimension="Completeness",
    severity="High",
    question="Does every active Rhapsody letter or communication appear in Reporting, and are letters deleted "
             "in Rhapsody gone from Reporting too?",
    why="Letter counts and review-decision metrics come from Reporting. A missing letter drops out of those "
        "numbers and searches. A deleted letter left in Reporting gets counted by mistake.",
    how_to_fix="Open the bundle in Rhapsody and find the letter by its file name or issue date. If it is active "
               "in Rhapsody but missing from Reporting, tell the Reporting (EDW) team. Include the letter ID "
               "with its bundle ID. If someone deleted it in Rhapsody on purpose, ask that team to remove it "
               "from Reporting too. If almost every flagged letter has no issue date, Reporting may take only "
               "issued letters by design. Confirm this with the Reporting team.",
    record_label="letters",
    filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
    date_label="Letter date",
    warn_below=0.999,
    fail_below=0.99,
    technical_note=(
        "slvr_ctp_core.rhapsody_communications (id BIGINT) LEFT JOIN gold_ctp_edw.dim_communication "
        "(communication_id BIGINT), matched with NUM_KEY(). Each side de-duplicated to one row per ID "
        "(Rhapsody: non-temporary, then active, then latest load; Reporting: halo_is_current = true, active "
        "row preferred). Removed = halo_is_deleted OR the source 'deleted' flag on both sides. Temporary "
        "working copies (s_is_temporary_copy) are excluded. Population: letters active in Rhapsody, plus "
        "letters deleted in Rhapsody that are still active in Reporting. Letters uploaded (else issued, else "
        "last modified) in the last 2 days and not yet in Reporting are not evaluated. Submission type / "
        "organization come from the letter's bundle (uploadcommunicationstobundle_id, else bundleid, matched to "
        "rhapsody_bundle). Confirmed on real data (Oct 2026): uploadcommunicationstobundle_id reaches 97.4% of "
        "bundles (K21) and bundleid 75% (K19, K20). Confirmed on real data (Oct 2026): Reporting's "
        "communication ID is the Rhapsody letter ID (K60: 100% of live Rhapsody letters found, and the "
        "reverse). record_date = first plausible of issuedate, uploadeddate, s_lastmodified, else the "
        "first load date. Reporting letters with no Rhapsody record are not evaluated (see assumptions)."
    ),
    assumptions=(
        "Reporting should take every Rhapsody letter, even unissued ones. Letters with no issue date get their "
        "own category, in case Reporting skips them by design.",
        "Reporting may also load Rhapsody's separate meeting records, so letters found only in Reporting are "
        "not flagged.",
    ),
    sql=_sql(r"""
WITH bnd AS (
  SELECT
    NUM_KEY(id)                                   AS bundle_key,
    BLANK(bundlename)                             AS bundle_name,
    BLANK(stdz_submission_type)                   AS sub_type,
    BLANK(organization)                           AS office
  FROM ${core}.rhapsody_bundle
  WHERE NUM_KEY(id) IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY NUM_KEY(id)
    ORDER BY CASE WHEN coalesce(s_is_temporary_copy, false) THEN 1 ELSE 0 END,
             CASE WHEN coalesce(halo_is_deleted, false) OR coalesce(deleted, false) THEN 1 ELSE 0 END,
             coalesce(halo_update_ts, halo_insert_ts) DESC NULLS LAST) = 1
),
rhl_rows AS (
  SELECT
    NUM_KEY(id)                                                       AS letter_key,
    CAST(id AS STRING)                                                AS letter_id,
    coalesce(BLANK(stdz_communicationstypelocal), BLANK(communicationstypelocal),
             BLANK(communicationtype))                                AS letter_type,
    issuedate                                                         AS issue_ts,
    uploadeddate                                                      AS uploaded_ts,
    s_lastmodified                                                    AS modified_ts,
    coalesce(BLANK(finalfilename), BLANK(doc_filename), BLANK(s_filename)) AS file_name,
    coalesce(NUM_KEY(uploadcommunicationstobundle_id), ANY_KEY(bundleid)) AS bundle_key,
    coalesce(halo_is_deleted, false) OR coalesce(deleted, false)      AS is_deleted,
    coalesce(s_is_temporary_copy, false)                              AS is_temp,
    halo_deleted_ts                                                   AS removed_ts,
    halo_insert_ts                                                    AS first_loaded_ts,
    coalesce(halo_update_ts, halo_insert_ts)                          AS loaded_ts
  FROM ${core}.rhapsody_communications
),
rhl AS (
  SELECT letter_key, letter_id, letter_type, issue_ts, uploaded_ts, modified_ts, file_name, bundle_key,
         is_deleted, is_temp, removed_ts, first_loaded_ts
  FROM rhl_rows
  WHERE letter_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY letter_key
                             ORDER BY CASE WHEN is_temp THEN 1 ELSE 0 END,
                                      CASE WHEN is_deleted THEN 1 ELSE 0 END,
                                      loaded_ts DESC NULLS LAST) = 1
),
rpl_rows AS (
  SELECT
    NUM_KEY(communication_id)                                         AS letter_key,
    coalesce(halo_is_deleted, false) OR coalesce(deleted, false)      AS is_deleted,
    coalesce(halo_modified_ts, halo_created_ts)                       AS loaded_ts
  FROM ${edw}.dim_communication
  WHERE halo_is_current = true
),
rpl AS (
  SELECT letter_key, is_deleted
  FROM rpl_rows
  WHERE letter_key IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY letter_key
                             ORDER BY CASE WHEN is_deleted THEN 1 ELSE 0 END, loaded_ts DESC NULLS LAST) = 1
),
j AS (
  SELECT
    rhl.letter_id, rhl.letter_type, rhl.issue_ts, rhl.uploaded_ts, rhl.modified_ts, rhl.file_name,
    rhl.bundle_key, rhl.removed_ts, rhl.first_loaded_ts,
    NOT rhl.is_deleted                                                AS in_rhap,
    coalesce(rpl.letter_key IS NOT NULL AND NOT rpl.is_deleted, false) AS in_rpt,
    coalesce(rpl.is_deleted, false)                                   AS removed_in_rpt
  FROM rhl
  LEFT JOIN rpl ON rhl.letter_key = rpl.letter_key
  WHERE NOT rhl.is_temp
),
e AS (
  SELECT
    j.letter_id, j.letter_type, j.issue_ts, j.uploaded_ts, j.modified_ts, j.file_name, j.bundle_key,
    j.removed_ts, j.first_loaded_ts, j.in_rhap, j.in_rpt, j.removed_in_rpt,
    bnd.bundle_name, bnd.sub_type, bnd.office,
    CASE
      WHEN j.in_rhap AND NOT j.in_rpt AND j.issue_ts IS NULL THEN 'Missing, not issued'
      WHEN j.in_rhap AND NOT j.in_rpt THEN 'Missing from Reporting'
      WHEN j.in_rpt AND NOT j.in_rhap THEN 'Still in Reporting'
    END AS category
  FROM j
  LEFT JOIN bnd ON j.bundle_key = bnd.bundle_key
  WHERE (j.in_rhap AND (j.in_rpt OR NOT @NEW_LETTER@))
     OR (j.in_rpt AND NOT j.in_rhap)
)
SELECT
  coalesce(letter_id, '(no letter ID)')                             AS record_key,
  @REC_DATE@                                                        AS record_date,
  CAST(sub_type AS STRING)                                          AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(category IS NOT NULL, false)                             AS is_issue,
  category                                                          AS issue_category,
  CASE
    WHEN category IN ('Missing from Reporting', 'Missing, not issued') THEN
      concat('Letter ', letter_id, coalesce(concat(' (', letter_type, ')'), ''),
             coalesce(concat(' on bundle ', bundle_key), ''),
             ' is active in Rhapsody but ',
             CASE WHEN removed_in_rpt THEN 'marked deleted' ELSE 'not found' END, ' in Reporting.',
             coalesce(concat(' Issued ', CAST(CAST(issue_ts AS DATE) AS STRING), '.'), ' It has no issue date.'))
    WHEN category = 'Still in Reporting' THEN
      concat('Letter ', letter_id, coalesce(concat(' (', letter_type, ')'), ''), ' was deleted in Rhapsody',
             coalesce(concat(' on ', CAST(CAST(removed_ts AS DATE) AS STRING)), ''),
             ' but is still active in Reporting.')
  END                                                               AS issue_reason,
  letter_id                                                         AS `Letter ID`,
  letter_type                                                       AS `Letter type`,
  CAST(issue_ts AS DATE)                                            AS `Issue date`,
  file_name                                                         AS `File name`,
  bundle_key                                                        AS `Bundle ID`,
  bundle_name                                                       AS `Bundle name`,
  CASE WHEN in_rhap THEN 'Yes' ELSE 'Deleted' END                   AS `In Rhapsody`,
  CASE WHEN in_rpt THEN 'Yes' WHEN removed_in_rpt THEN 'Deleted' ELSE 'No' END AS `In Reporting`,
  sub_type                                                          AS `Submission type (bundle)`,
  office                                                            AS `Organization (bundle)`
FROM e
""",
        NEW_LETTER=_recent("coalesce(j.uploaded_ts, j.issue_ts, j.modified_ts)"),
        REC_DATE=_first_plausible("issue_ts", "uploaded_ts", "modified_ts", "first_loaded_ts"),
    ),
)


# --------------------------------------------------------------------------- #
# (b) Data feeds
# --------------------------------------------------------------------------- #

# --- Freshness: one row per feed (a key copy of the system), latest load time ---

_FRESHNESS_TAIL = r"""
f AS (
  SELECT
    feed_name,
    what,
    last_loaded_ts,
    total_records,
    active_records,
    CAST(last_loaded_ts AS DATE)                                  AS last_loaded_date,
    datediff(current_date(), CAST(last_loaded_ts AS DATE)) - 1    AS days_between
  FROM feeds
),
s AS (
  SELECT
    feed_name, what, last_loaded_ts, total_records, active_records, last_loaded_date,
    CASE
      WHEN last_loaded_ts IS NULL THEN 'No refresh recorded'
      WHEN active_records = 0 THEN 'No active records'
      WHEN days_between <= 0 THEN NULL
      WHEN days_between = 1 AND dayofweek(date_add(last_loaded_date, 1)) IN (1, 7) THEN NULL
      WHEN days_between = 2 AND dayofweek(date_add(last_loaded_date, 1)) = 7 THEN NULL
      ELSE 'Refresh overdue'
    END AS category
  FROM f
)
SELECT
  feed_name                                                     AS record_key,
  last_loaded_date                                              AS record_date,
  CAST(NULL AS STRING)                                          AS submission_type,
  CAST(NULL AS STRING)                                          AS office,
  coalesce(category IS NOT NULL, false)                         AS is_issue,
  category                                                      AS issue_category,
  CASE category
    WHEN 'No refresh recorded' THEN
      CASE WHEN total_records = 0 THEN concat('The copy of ', feed_name, ' is empty.')
           ELSE concat('None of the ', CAST(total_records AS STRING), ' records in ', feed_name,
                       ' has a load time, so its last refresh is unknown.') END
    WHEN 'No active records' THEN
      concat('All ', CAST(total_records AS STRING), ' records in ', feed_name,
             ' are marked removed or replaced (last change ', date_format(last_loaded_ts, 'yyyy-MM-dd HH:mm'),
             '). The copy is effectively empty.')
    WHEN 'Refresh overdue' THEN
      concat('Last refreshed ', date_format(last_loaded_ts, 'yyyy-MM-dd HH:mm'), ' (',
             CAST(timestampdiff(HOUR, last_loaded_ts, current_timestamp()) AS STRING),
             ' hours ago). A full working day has passed with no new data.')
  END                                                           AS issue_reason,
  feed_name                                                     AS `Data feed`,
  what                                                          AS `Contents`,
  last_loaded_ts                                                AS `Last refreshed`,
  timestampdiff(HOUR, last_loaded_ts, current_timestamp())      AS `Hours since refresh`,
  active_records                                                AS `Active records`,
  total_records                                                 AS `Total records`
FROM s
"""


def _freshness_sql(feeds: list[tuple[str, str, str]], gold: bool = False) -> str:
    """feeds: (friendly feed name, what it contains, table reference)."""
    if gold:
        ts = "greatest(halo_modified_ts, halo_created_ts)"
        active = "halo_is_current = true AND coalesce(halo_is_deleted, false) = false"
    else:
        ts = "greatest(halo_update_ts, halo_insert_ts, halo_deleted_ts)"
        active = "coalesce(halo_is_deleted, false) = false"
    union = "\n  UNION ALL\n".join(
        f"  SELECT '{name}' AS feed_name, '{what}' AS what,\n"
        f"         max(CASE WHEN {ts} <= current_timestamp() + INTERVAL 1 DAY THEN {ts} END) AS last_loaded_ts,\n"
        f"         count(*) AS total_records,\n"
        f"         count_if({active}) AS active_records\n"
        f"  FROM {table}"
        for name, what, table in feeds
    )
    return _sql("WITH feeds AS (\n" + union + "\n),", _FRESHNESS_TAIL)


_FRESHNESS_NOTE = (
    "Latest load time per table = max(greatest(halo_update_ts, halo_insert_ts, halo_deleted_ts)) for "
    "slvr_ctp_core copies, max(greatest(halo_modified_ts, halo_created_ts)) for gold_ctp_edw tables; "
    "these are the pipeline's own load stamps (not source business dates) and advance whenever a load "
    "touches a record. Stamps more than 1 day in the future are ignored so one bad "
    "value cannot hide a stopped feed. Flagged when at least one full working day (Mon-Fri) lies strictly "
    "between the last load date and today, so a quiet weekend is not flagged but a missed weekday load is "
    "(on a weekday this means no load for between 24 and 48 hours, depending on the time of day). Also "
    "flagged: no load time at all, or no active records (every row soft-deleted, or for Reporting no "
    "current non-deleted version). Dates use the warehouse session time zone. Tables with naturally little "
    "daily activity (reference lists) are deliberately not included. Active records = not soft-deleted "
    "(Reporting: halo_is_current AND NOT halo_is_deleted)."
)

_FRESHNESS_ASSUMPTIONS = (
    "Each copy gets new or changed records every working day. A working day with no load means the feed did "
    "not run.",
    "A feed may be flagged once on the first working day after a federal holiday, because nothing changed on "
    "the holiday. The flag clears with the next refresh.",
)


def _freshness_check(cid: str, system: str, name: str, feeds, gold: bool = False) -> Check:
    return Check(
        id=cid,
        title=f"{name} feeds overdue for a refresh",
        systems=(system,),
        entity="Data feeds",
        dimension="Timeliness",
        severity="High",
        question=f"Has every {name} data feed refreshed within the last working day?",
        why=f"This app and CTP reports read a copy of {name} that refreshes automatically. When a feed stops, "
            f"every number that uses it goes out of date without warning.",
        how_to_fix=f"Tell the data engineering team which {name} feed is flagged and when it last refreshed. "
                   f"Treat {name} numbers as out of date until the team fixes the feed. "
                   f"A single flag on the first working day after a holiday is normal.",
        record_label="data feeds",
        filters=frozenset(),
        date_label="Last refreshed",
        warn_below=0.999,
        fail_below=0.99,
        technical_note=_FRESHNESS_NOTE + " Tables: " + ", ".join(t.split(".")[-1] for _, _, t in feeds) + ".",
        assumptions=_FRESHNESS_ASSUMPTIONS,
        sql=_freshness_sql(feeds, gold=gold),
    )


ESUB_FRESHNESS = _freshness_check("FEED_ESUB_FRESHNESS", "esub", "eSUB", [
    ("eSUB log-in list", "Submissions logged in through iTRAC", "${core}.esub_itrac_reglist"),
    ("eSUB electronic submissions", "Submissions received through the electronic gateway", "${core}.esub_tob_submission"),
    ("eSUB documents", "Files received with submissions", "${core}.esub_document"),
    ("eSUB submission documents", "Which files belong to which submission", "${core}.esub_submission_document"),
    ("eSUB submission links", "Amendments and other links between submissions", "${core}.esub_itrac_sub_links"),
])

RHAP_FRESHNESS = _freshness_check("FEED_RHAP_FRESHNESS", "rhapsody", "Rhapsody", [
    ("Rhapsody submissions", "Submissions under review", "${core}.rhapsody_submissions"),
    ("Rhapsody bundles", "Review bundles with status and goal dates", "${core}.rhapsody_bundle"),
    ("Rhapsody products under review", "Products under review in each bundle", "${core}.rhapsody_submission_product_wrapper"),
    ("Rhapsody products", "Product details shown in Rhapsody", "${core}.rhapsody_products"),
    ("Rhapsody letters and communications", "Letters and other communications", "${core}.rhapsody_communications"),
    ("Rhapsody review tasks", "Work items assigned to CTP staff", "${core}.rhapsody_notf_task_instance"),
])

PMS_FRESHNESS = _freshness_check("FEED_PMS_FRESHNESS", "pms", "PMS", [
    ("PMS products", "Products and their attributes", "${core}.pms_product"),
    ("PMS unique products", "Distinct product definitions shared by products", "${core}.pms_unique_product"),
])

EDW_FRESHNESS = _freshness_check("FEED_EDW_FRESHNESS", "edw", "Reporting", [
    ("Reporting submissions", "Submissions used by dashboards", "${edw}.dim_submission"),
    ("Reporting bundles", "Bundles used by dashboards", "${edw}.dim_bundle"),
    ("Reporting products", "Products used by dashboards", "${edw}.dim_product"),
    ("Reporting bundle-submission-product links", "Which products are in which submission and bundle",
     "${edw}.fact_bundle_submission_product"),
    ("Reporting letters and communications", "Letters and communications used by dashboards", "${edw}.dim_communication"),
    ("Reporting documents", "Files received with submissions", "${edw}.dim_document"),
    ("Reporting submission links", "Amendments and other links between submissions", "${edw}.dim_submission_relationships"),
], gold=True)


# --- Pipeline completeness: raw copy vs processed copy, one row per table pair ---

class _Pair(NamedTuple):
    name: str        # friendly feed name
    what: str        # what it contains
    raw_table: str   # as-received (bronze) table
    raw_key: str     # record ID expression on the raw table (shared macros)
    harm_table: str  # processed (slvr_ctp_core) table
    harm_key: str    # record ID expression on the processed table
    where: str = "TRUE"  # rows to compare, applied to both copies


_NOT_TEMP = "coalesce(s_is_temporary_copy, false) = false"


def _pipeline_sql(source: str, pairs: list[_Pair]) -> str:
    ctes, selects = [], []
    for i, p in enumerate(pairs, 1):
        ctes.append(f"""raw_{i} AS (
  SELECT k, min(loaded_ts) AS first_received_ts, max(loaded_ts) AS last_received_ts
  FROM (SELECT {p.raw_key} AS k, halo_insert_ts AS loaded_ts FROM {p.raw_table} WHERE {p.where}) x
  WHERE k IS NOT NULL
  GROUP BY k
),
harm_{i} AS (
  SELECT k, max(CASE WHEN is_deleted THEN 0 ELSE 1 END) AS is_active, max(load_ts) AS load_ts
  FROM (SELECT {p.harm_key} AS k, coalesce(halo_is_deleted, false) AS is_deleted,
               greatest(halo_update_ts, halo_insert_ts, halo_deleted_ts) AS load_ts
        FROM {p.harm_table} WHERE {p.where}) x
  WHERE k IS NOT NULL
  GROUP BY k
),
cmp_{i} AS (
  SELECT
    '{p.name}'                                    AS feed_name,
    '{p.what}'                                    AS what,
    count(r.k)                                    AS raw_n,
    count(h.k)                                    AS harm_n,
    count_if(h.is_active = 1)                     AS harm_active_n,
    count_if(r.k IS NOT NULL AND h.k IS NULL)     AS missing_n,
    min(CASE WHEN h.k IS NULL THEN r.first_received_ts END) AS oldest_missing_ts,
    max(r.last_received_ts)                       AS raw_loaded_ts,
    max(h.load_ts)                                AS harm_loaded_ts
  FROM raw_{i} r
  FULL OUTER JOIN harm_{i} h ON r.k = h.k
)""")
        selects.append(
            f"  SELECT feed_name, what, raw_n, harm_n, harm_active_n, missing_n, oldest_missing_ts, "
            f"raw_loaded_ts, harm_loaded_ts FROM cmp_{i}"
        )
    tail = r""",
all_feeds AS (
""" + "\n  UNION ALL\n".join(selects) + r"""
),
s AS (
  SELECT
    feed_name, what, raw_n, harm_n, harm_active_n, missing_n, oldest_missing_ts, raw_loaded_ts, harm_loaded_ts,
    CAST(floor(raw_n * 0.001) AS BIGINT)                          AS allowed_n,
    coalesce(oldest_missing_ts < harm_loaded_ts, false)           AS has_overdue
  FROM all_feeds
),
c AS (
  SELECT
    feed_name, what, raw_n, harm_n, harm_active_n, missing_n, oldest_missing_ts, raw_loaded_ts,
    harm_loaded_ts, allowed_n, has_overdue,
    CASE
      WHEN raw_n = 0 THEN 'Nothing received'
      WHEN missing_n = 0 THEN NULL
      WHEN harm_n = 0 THEN 'Nothing processed'
      WHEN has_overdue OR missing_n > allowed_n THEN 'Records lost'
    END                                                           AS category
  FROM s
)
SELECT
  feed_name                                                     AS record_key,
  CAST(raw_loaded_ts AS DATE)                                   AS record_date,
  CAST(NULL AS STRING)                                          AS submission_type,
  CAST(NULL AS STRING)                                          AS office,
  coalesce(category IS NOT NULL, false)                         AS is_issue,
  category                                                      AS issue_category,
  CASE category
    WHEN 'Nothing received' THEN
      concat('The as-received copy of ', feed_name, ' is empty, so nothing can be checked. ',
             CASE WHEN harm_n > 0 THEN concat('The processed copy has ', CAST(harm_n AS STRING), ' records.')
                  ELSE 'The processed copy is empty too.' END)
    WHEN 'Nothing processed' THEN
      concat('The processed copy is empty, though ', CAST(raw_n AS STRING), ' records arrived from @SOURCE@.')
    WHEN 'Records lost' THEN
      concat(CAST(missing_n AS STRING), ' of ', CAST(raw_n AS STRING), ' @SOURCE@ records (',
             CAST(round(100.0 * missing_n / nullif(raw_n, 0), 2) AS STRING), '%) are missing after processing.',
             CASE WHEN has_overdue
                  THEN concat(' The oldest arrived ', date_format(oldest_missing_ts, 'yyyy-MM-dd HH:mm'),
                              ', before the last run at ', date_format(harm_loaded_ts, 'yyyy-MM-dd HH:mm'), '.')
                  WHEN allowed_n > 0
                  THEN concat(' They arrived after the last refresh, but more than ', CAST(allowed_n AS STRING),
                              ' waiting records is unusual.')
                  ELSE ' They arrived after the last refresh. Re-check after the next one.' END)
  END                                                           AS issue_reason,
  feed_name                                                     AS `Data feed`,
  what                                                          AS `Contents`,
  raw_n                                                         AS `Records received`,
  harm_n                                                        AS `Processed records`,
  harm_active_n                                                 AS `Active processed records`,
  missing_n                                                     AS `Missing records`,
  round(100.0 * missing_n / nullif(raw_n, 0), 2)                AS `Share missing (%)`,
  oldest_missing_ts                                             AS `Oldest missing record`,
  raw_loaded_ts                                                 AS `Last received`,
  harm_loaded_ts                                                AS `Processed copy refreshed`
FROM c
"""
    return _sql("WITH\n" + ",\n".join(ctes) + tail, SOURCE=source)


_PIPELINE_NOTE = (
    "For each raw (bronze) table and its processed (slvr_ctp_core) copy: distinct record IDs on each side, "
    "normalized with the shared macros (NUM_KEY for numeric IDs, STN_KEY for STNs, ANY_KEY for STRING IDs "
    "of unknown format, so no STRING value is ever cast in a way that can fail), composite IDs joined with "
    "'|'; FULL OUTER JOIN on the ID. Missing = IDs in the raw copy that are not in the processed copy at all. "
    "Soft-deleted processed rows still count as present. The result therefore holds for a full-snapshot raw "
    "copy and for an append-only history. An empty raw copy is always flagged. Otherwise flagged when "
    "(a) any missing record first arrived (min raw halo_insert_ts) before the processed copy's latest load "
    "stamp (max greatest(halo_update_ts, halo_insert_ts, halo_deleted_ts)), i.e. processing has run since "
    "and still skipped it; or (b) more than 0.1% of the raw IDs (rounded down; any for tables under 1,000 "
    "records) are missing even though they arrived after the last processing run. Rhapsody temporary working "
    "copies (s_is_temporary_copy) are left out on both sides where the column exists. One row per table "
    "pair; record_date = latest raw halo_insert_ts."
)

_PIPELINE_ASSUMPTIONS = (
    "Every record in the as-received copy should reach the processed copy. Nothing is filtered out on "
    "purpose. Rhapsody temporary working copies are left out, in case processing skips them by design.",
    "Each pair uses a record ID that means the same thing in both copies. Processing builds each processed "
    "copy from the as-received copy in its pair. For Rhapsody, that is the direct Rhapsody copy, not the "
    "older CDR archive copy.",
    "The as-received copy keeps every record it receives and is not emptied after each load. An empty copy "
    "means the feed failed.",
    "Records received after the last processing run are flagged only when there are unusually many. A "
    "record received just before an unfinished run can be flagged once. The flag clears when the run ends.",
)


def _pipeline_check(cid: str, system: str, name: str, pairs: list[_Pair]) -> Check:
    return Check(
        id=cid,
        title=f"{name} records lost in processing",
        systems=(system,),
        entity="Data feeds",
        dimension="Completeness",
        severity="High",
        question=f"Does every record received from {name} reach the processed copy that reports use?",
        why=f"Records from {name} land first in an as-received copy. Processing then builds the version that "
            f"Reporting and this app read. A record lost between the two steps vanishes from every count "
            f"without warning.",
        how_to_fix=f"Tell the data engineering team which {name} feed is flagged and how many records are "
                   "missing. Include the date of the oldest missing record. If the reason says the records "
                   "arrived after the last refresh, re-check after the next refresh before you escalate.",
        record_label="data feeds",
        filters=frozenset(),
        date_label="Last received",
        warn_below=0.999,
        fail_below=0.99,
        technical_note=_PIPELINE_NOTE + " Pairs: " + "; ".join(
            f"{p.raw_table.split('.')[-1]} -> {p.harm_table.split('.')[-1]} on {p.raw_key}" for p in pairs) + ".",
        assumptions=_PIPELINE_ASSUMPTIONS,
        sql=_pipeline_sql(name, pairs),
    )


def _same(raw_table: str, harm_table: str, key: str, name: str, what: str, where: str = "TRUE") -> _Pair:
    return _Pair(name, what, raw_table, key, harm_table, key, where)


# Rhapsody submission-product rows have no single ID: STN + product + bundle (all STRING in the raw copy).
_RHAP_SUBPROD_KEY = ("nullif(concat_ws('|', coalesce(STN_KEY(stn), ''), coalesce(ANY_KEY(product_id), ''), "
                     "coalesce(ANY_KEY(bundle_id), '')), '||')")

ESUB_PIPELINE = _pipeline_check("FEED_ESUB_PROCESSING_LOSS", "esub", "eSUB", [
    _same("${brnz_esub}.itrac_reglist", "${core}.esub_itrac_reglist", "NUM_KEY(REGLIST_KEY)",
          "eSUB log-in list", "Submissions logged in through iTRAC"),
    _same("${brnz_esub}.tob_submission", "${core}.esub_tob_submission", "NUM_KEY(SUBMISSION_KEY)",
          "eSUB electronic submissions", "Submissions received through the electronic gateway"),
    _same("${brnz_esub}.document", "${core}.esub_document", "NUM_KEY(DOCUMENT_KEY)",
          "eSUB documents", "Files received with submissions"),
    _same("${brnz_esub}.submission_document", "${core}.esub_submission_document", "NUM_KEY(SUBMISSION_DOCUMENT_KEY)",
          "eSUB submission documents", "Which files belong to which submission"),
    _same("${brnz_esub}.itrac_sub_links", "${core}.esub_itrac_sub_links", "NUM_KEY(SUB_LINKS_KEY)",
          "eSUB submission links", "Amendments and other links between submissions"),
    _same("${brnz_esub}.application", "${core}.esub_application", "NUM_KEY(APPLICATION_KEY)",
          "eSUB applications", "Applications that group related submissions"),
    _same("${brnz_esub}.application_submission", "${core}.esub_application_submission",
          "NUM_KEY(APPLICATION_SUBMISSION_KEY)",
          "eSUB application submissions", "Which submissions belong to which application"),
    _same("${brnz_esub}.itrac_doc_types", "${core}.esub_itrac_doc_types", "NUM_KEY(DOC_TYPES_KEY)",
          "eSUB submission type list", "Submission types offered in iTRAC"),
])

RHAP_PIPELINE = _pipeline_check("FEED_RHAP_PROCESSING_LOSS", "rhapsody", "Rhapsody", [
    _same("${brnz_rhap}.rhapsody_submissions", "${core}.rhapsody_submissions", "STN_KEY(stn)",
          "Rhapsody submissions", "Submissions under review"),
    _same("${brnz_rhap}.o3fdarhapsodyprojectbundle", "${core}.rhapsody_bundle", "NUM_KEY(id)",
          "Rhapsody bundles", "Review bundles with status and goal dates", _NOT_TEMP),
    _same("${brnz_rhap}.o3fdarhapsodyprojectsubmissionproductwrapper", "${core}.rhapsody_submission_product_wrapper",
          "NUM_KEY(id)", "Rhapsody products under review", "Products under review in each bundle", _NOT_TEMP),
    _same("${brnz_rhap}.rhapsody_submissionproducts", "${core}.rhapsody_submission_products", _RHAP_SUBPROD_KEY,
          "Rhapsody submission product list", "Which products belong to which submission and bundle"),
    _same("${brnz_rhap}.rhapsody_products", "${core}.rhapsody_products", "ANY_KEY(product_id)",
          "Rhapsody products", "Product details shown in Rhapsody"),
    _same("${brnz_rhap}.rhapsody_bulk_products", "${core}.rhapsody_bulk_products", _RHAP_SUBPROD_KEY,
          "Rhapsody bulk product lists", "Products filed in bulk product lists"),
    _same("${brnz_rhap}.o3fdarhapsodyprojectuploadcommunications", "${core}.rhapsody_communications", "NUM_KEY(id)",
          "Rhapsody letters and communications", "Letters and other communications", _NOT_TEMP),
    _same("${brnz_rhap}.o3fdarhapsodyprojecttouploadcommunicationsmeeting", "${core}.rhapsody_communications_meeting",
          "NUM_KEY(id)", "Rhapsody meeting communications", "Meeting-related letters and records", _NOT_TEMP),
    _same("${brnz_rhap}.o3fdarhapsodyprojectmeetingrequest", "${core}.rhapsody_meeting_request", "NUM_KEY(id)",
          "Rhapsody meeting requests", "Meeting requests from industry", _NOT_TEMP),
    _same("${brnz_rhap}.notf_task_instance", "${core}.rhapsody_notf_task_instance", "ANY_KEY(task_instance_id)",
          "Rhapsody review tasks", "Work items assigned to CTP staff"),
    _same("${brnz_rhap}.o3fdarhapsodyprojectgenericrta", "${core}.rhapsody_generic_rta", "NUM_KEY(id)",
          "Rhapsody GenRTA records", "GenRTA review records", _NOT_TEMP),
    _same("${brnz_rhap}.o3fdarhapsodyprojectgfbundle", "${core}.rhapsody_gfbundle", "NUM_KEY(id)",
          "Rhapsody GF bundles", "GF review bundles", _NOT_TEMP),
])

PMS_PIPELINE = _pipeline_check("FEED_PMS_PROCESSING_LOSS", "pms", "PMS", [
    _same("${brnz_pms}.product", "${core}.pms_product", "NUM_KEY(id)",
          "PMS products", "Products and their attributes"),
    _same("${brnz_pms}.unique_product", "${core}.pms_unique_product", "NUM_KEY(id)",
          "PMS unique products", "Distinct product definitions shared by products"),
    _same("${brnz_pms_ref}.category", "${core}.pms_category", "NUM_KEY(id)",
          "PMS product categories", "The list of product categories"),
    _same("${brnz_pms_ref}.subcategory", "${core}.pms_subcategory", "NUM_KEY(id)",
          "PMS product subcategories", "The list of product subcategories"),
    _same("${brnz_pms_ref}.product_attribute", "${core}.pms_product_attribute", "NUM_KEY(id)",
          "PMS product attribute list", "The list of product attributes and their codes"),
])


# --- eSUB weekly intake volume -------------------------------------------------

ESUB_WEEKLY_VOLUME = Check(
    id="FEED_ESUB_WEEKLY_LOGIN_VOLUME",
    title="Weeks with unusually few eSUB log-ins",
    systems=("esub",),
    entity="Data feeds",
    dimension="Completeness",
    severity="Medium",
    question="Does every week in the last two years have a normal number of submissions logged in eSUB?",
    why="A week with few or no log-ins usually means records failed to load, not that industry stopped "
        "submitting. Submission counts and trend charts for that week come out too low.",
    how_to_fix="Compare the week's count with the eSUB log-in list for the same dates. If eSUB shows more, "
               "report the week to the data engineering team so they can reload the missing records. Ignore "
               "holiday weeks and known FDA closures.",
    record_label="weeks",
    filters=frozenset(),
    date_label="Week starting",
    warn_below=0.999,
    fail_below=0.98,
    technical_note=(
        "slvr_ctp_core.esub_itrac_reglist (halo_is_deleted false): distinct NUM_KEY(REGLIST_KEY) per "
        "Monday-starting week of the log-in date coalesce(DATE_ENTERED, CREATE_DATE, DATE_RECEIVED) (all "
        "TIMESTAMP, so no text is cast). Every week is generated as 116 consecutive Mondays (sequence 0..115) "
        "so weeks with zero log-ins appear. Typical = approximate median (percentile_approx) of the previous "
        "12 weeks. Flagged when a week has 0 log-ins, or fewer than one third of typical when typical is at "
        "least 20. One row per completed week for the last 104 weeks (the current, partial week is excluded; "
        "log-in dates in the future fall outside the generated weeks and are ignored). Inactive log-ins are "
        "counted, because they still arrived through the feed."
    ),
    assumptions=(
        "The log-in date is when the record was entered in iTRAC (date entered, else created, else received).",
        "Outside holiday weeks and FDA closures, weekly volume stays above a third of the recent median "
        "unless the feed has a gap.",
    ),
    sql=_sql(r"""
WITH logins AS (
  SELECT CAST(date_trunc('WEEK', login_date) AS DATE) AS week_start,
         count(DISTINCT reglist_key)                  AS logins
  FROM (
    SELECT NUM_KEY(REGLIST_KEY)                                              AS reglist_key,
           CAST(coalesce(DATE_ENTERED, CREATE_DATE, DATE_RECEIVED) AS DATE)  AS login_date
    FROM ${core}.esub_itrac_reglist
    WHERE coalesce(halo_is_deleted, false) = false
  ) r
  WHERE reglist_key IS NOT NULL
    AND login_date >= date_sub(current_date(), 7 * 120)
  GROUP BY CAST(date_trunc('WEEK', login_date) AS DATE)
),
weeks AS (
  SELECT CAST(date_add(CAST(date_trunc('WEEK', date_sub(current_date(), 7 * 116)) AS DATE), 7 * i) AS DATE) AS week_start
  FROM (SELECT explode(sequence(0, 115)) AS i) w
),
series AS (
  SELECT w.week_start, coalesce(l.logins, 0) AS logins
  FROM weeks w
  LEFT JOIN logins l ON w.week_start = l.week_start
),
scored AS (
  SELECT
    week_start,
    logins,
    percentile_approx(logins, 0.5) OVER (ORDER BY week_start ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING) AS typical
  FROM series
),
judged AS (
  SELECT
    week_start, logins, typical,
    CASE
      WHEN logins = 0 THEN 'No log-ins recorded'
      WHEN typical >= 20 AND logins * 3 < typical THEN 'Far below normal'
    END AS category
  FROM scored
  WHERE week_start >= CAST(date_trunc('WEEK', date_sub(current_date(), 7 * 104)) AS DATE)
)
SELECT
  concat('Week of ', CAST(week_start AS STRING))                AS record_key,
  week_start                                                    AS record_date,
  CAST(NULL AS STRING)                                          AS submission_type,
  CAST(NULL AS STRING)                                          AS office,
  coalesce(category IS NOT NULL, false)                         AS is_issue,
  category                                                      AS issue_category,
  CASE WHEN category IS NOT NULL THEN
    concat(CASE WHEN logins = 0 THEN 'No' ELSE concat('Only ', CAST(logins AS STRING)) END,
           ' eSUB log-ins in the week of ', CAST(week_start AS STRING), '.',
           coalesce(concat(' A typical week over the prior 12 weeks has ', CAST(typical AS STRING), '.'), ''))
  END                                                           AS issue_reason,
  week_start                                                    AS `Week starting`,
  logins                                                        AS `Log-ins`,
  typical                                                       AS `Typical week`,
  round(100.0 * logins / nullif(typical, 0), 0)                 AS `Share of typical (%)`
FROM judged
"""),
)


# --- Standardization ("clean-up") job log -------------------------------------

_STDZ_SQL = r"""
WITH rules AS (
  SELECT
    rule_key,
    max(rule_name)                                                       AS rule_name,
    max(rule_source)                                                     AS rule_source
  FROM (
    SELECT ANY_KEY(stdz_rule_id)                                         AS rule_key,
           BLANK(rule_name)                                              AS rule_name,
           lower(concat_ws(' ', brnz_schema_name, brnz_table_name))      AS rule_source
    FROM ${dq}.standardization_rules
  ) r
  WHERE rule_key IS NOT NULL
  GROUP BY rule_key
),
runs AS (
  SELECT
    coalesce(ANY_KEY(rule_id), 'not recorded')                           AS rule_key,
    coalesce(nullif(lower(regexp_extract(BLANK(table_name), '([^.]+)$', 1)), ''), '(not recorded)') AS table_name,
    execution_timestamp,
    BLANK(execution_status)                                              AS status,
    regexp_replace(upper(coalesce(execution_status, '')), '[^A-Z]', '')  AS status_code,
    BLANK(error_message)                                                 AS error_message,
    records_processed,
    BLANK(log_id)                                                        AS log_id
  FROM ${dq}.standardization_log
  WHERE execution_timestamp >= current_timestamp() - INTERVAL 30 DAYS
),
classified AS (
  SELECT
    runs.rule_key,
    runs.table_name,
    runs.execution_timestamp,
    runs.status,
    runs.error_message,
    runs.records_processed,
    runs.log_id,
    rules.rule_name,
    CASE
      WHEN lower(concat_ws(' ', runs.table_name, rules.rule_source)) RLIKE 'rhapsody|o3fda|o3opentext' THEN 'rhapsody'
      WHEN lower(concat_ws(' ', runs.table_name, rules.rule_source)) RLIKE 'pms' THEN 'pms'
      WHEN lower(concat_ws(' ', runs.table_name, rules.rule_source)) RLIKE 'esub|itrac' THEN 'esub'
      ELSE 'other'
    END                                                                  AS source_system,
    runs.status_code IN ('SUCCESS', 'SUCCEEDED', 'SUCCESSFUL', 'COMPLETED', 'COMPLETE',
                         'OK', 'PASS', 'PASSED', 'DONE', 'FINISHED')     AS is_success,
    runs.status_code IN ('RUNNING', 'INPROGRESS', 'STARTED', 'PENDING', 'QUEUED', 'PROCESSING') AS is_running
  FROM runs
  LEFT JOIN rules ON runs.rule_key = rules.rule_key
),
ranked AS (
  SELECT
    rule_key, table_name, execution_timestamp, status, error_message, records_processed, rule_name,
    is_success, is_running,
    row_number() OVER (PARTITION BY rule_key, table_name
                       ORDER BY execution_timestamp DESC, CASE WHEN is_running THEN 1 ELSE 0 END,
                                log_id DESC) AS rn,
    count(*) OVER (PARTITION BY rule_key, table_name)                                       AS runs_30d,
    sum(CASE WHEN is_success OR (is_running AND execution_timestamp >= current_timestamp() - INTERVAL 1 DAY)
             THEN 0 ELSE 1 END) OVER (PARTITION BY rule_key, table_name)                    AS failed_30d
  FROM classified
  WHERE source_system = '@SYSTEM@'
),
latest AS (
  SELECT
    rule_key, table_name, execution_timestamp, status, error_message, records_processed, rule_name,
    runs_30d, failed_30d,
    coalesce(concat(rule_name, ' (rule ', rule_key, ')'), concat('Rule ', rule_key)) AS rule_label,
    CASE WHEN table_name = '(not recorded)' THEN '@SYSNAME@ (area not recorded)'
         ELSE concat('@SYSNAME@ ', trim(regexp_replace(
                regexp_replace(table_name, '^(rhapsody_|pms_|esub_|o3fdarhapsodyproject|o3opentextentity)', ''),
                '_', ' ')))
    END AS data_area,
    CASE
      WHEN is_success THEN NULL
      WHEN is_running AND execution_timestamp >= current_timestamp() - INTERVAL 1 DAY THEN NULL
      WHEN is_running THEN 'Run never finished'
      ELSE 'Last run failed'
    END AS category
  FROM ranked
  WHERE rn = 1
)
SELECT
  concat(rule_label, ' - ', data_area, ' [', table_name, ']')   AS record_key,
  CAST(execution_timestamp AS DATE)                             AS record_date,
  CAST(NULL AS STRING)                                          AS submission_type,
  CAST(NULL AS STRING)                                          AS office,
  coalesce(category IS NOT NULL, false)                         AS is_issue,
  category                                                      AS issue_category,
  CASE WHEN category IS NOT NULL THEN
    concat('Last run on ', date_format(execution_timestamp, 'yyyy-MM-dd HH:mm'),
           CASE WHEN category = 'Run never finished' THEN ' started but never finished'
                ELSE concat(' ended with status ', coalesce(concat('"', status, '"'), 'blank')) END,
           coalesce(concat(': ', substring(error_message, 1, 200)), ''),
           '. ', CAST(failed_30d AS STRING), ' of ', CAST(runs_30d AS STRING),
           ' runs in the last 30 days did not succeed.')
  END                                                           AS issue_reason,
  rule_label                                                    AS `Clean-up rule`,
  data_area                                                     AS `Data area`,
  execution_timestamp                                           AS `Last run`,
  status                                                        AS `Result`,
  failed_30d                                                    AS `Failed runs (30 days)`,
  runs_30d                                                      AS `Runs (30 days)`,
  records_processed                                             AS `Records processed`,
  error_message                                                 AS `Error message`
FROM latest
"""


def _stdz_check(cid: str, system: str, name: str) -> Check:
    return Check(
        id=cid,
        title=f"{name} clean-up rules whose last run failed",
        systems=(system,),
        entity="Data feeds",
        dimension="Completeness",
        severity="Medium",
        question=f"Did every automatic clean-up rule for {name} data succeed the last time it ran?",
        why="Clean-up rules fill in standard values (such as the standard submission type or a corrected STN). "
            f"Reports use these values to group and filter records. A failed rule can leave {name} values "
            f"blank or out of date.",
        how_to_fix="Send the rule name and its error message to the data engineering team. The flag clears once "
                   "the rule runs successfully again.",
        record_label="clean-up rules",
        filters=frozenset(),
        date_label="Last run",
        warn_below=0.999,
        fail_below=0.95,
        technical_note=(
            "slvr_ctp_dq.standardization_log, runs in the last 30 days, one row per (rule_id, table_name) "
            "evaluated on its most recent run (ties broken in favor of a finished run, then the higher log_id). "
            "rule_id (STRING) is matched to standardization_rules.stdz_rule_id (BIGINT) with ANY_KEY() on both "
            "sides to get the rule name and bronze schema/table. Confirmed on real data (Oct 2026): every rule "
            "number in the log is on the rule list (K65: 10 of 10). System is inferred from the log "
            "table_name plus the rule's brnz_schema_name/brnz_table_name ('rhapsody'/'o3fda' -> Rhapsody, "
            "'pms' -> PMS, 'esub'/'itrac' -> eSUB). execution_status is upper-cased with non-letters removed; "
            "SUCCESS/SUCCEEDED/SUCCESSFUL/COMPLETED/COMPLETE/OK/PASS/PASSED/DONE/FINISHED pass; "
            "RUNNING/INPROGRESS/STARTED/PENDING/QUEUED/PROCESSING pass for 1 day, then 'Run never finished'; "
            "anything else (including blank) is a failure. Rules that have not run at all in 30 days are not "
            "listed. record_key includes the rule ID and the log's table name so it is unique per row."
        ),
        assumptions=(
            f"A rule counts as a {name} rule when the name of the data it cleans mentions {name}. Runs whose "
            f"names mention no system are not shown.",
            "Any status outside the listed success and in-progress values counts as a failure.",
        ),
        sql=_sql(_STDZ_SQL, SYSTEM=system, SYSNAME=name),
    )


RHAP_STDZ = _stdz_check("FEED_RHAP_CLEANUP_FAILURES", "rhapsody", "Rhapsody")
PMS_STDZ = _stdz_check("FEED_PMS_CLEANUP_FAILURES", "pms", "PMS")


CHECKS: list[Check] = [
    # (a) source systems vs Reporting
    RHAP_SUBMISSIONS_MISMATCH,
    RHAP_SUBMISSION_DETAILS,
    RHAP_BUNDLES_MISMATCH,
    RHAP_BUNDLE_DETAILS,
    RHAP_PRODUCT_COUNTS,
    PMS_PRODUCTS_MISMATCH,
    PMS_PRODUCT_DETAILS,
    ESUB_LINKS_MISSING,
    RHAP_LETTERS_MISMATCH,
    # (b) data feeds
    ESUB_FRESHNESS,
    RHAP_FRESHNESS,
    PMS_FRESHNESS,
    EDW_FRESHNESS,
    ESUB_PIPELINE,
    RHAP_PIPELINE,
    PMS_PIPELINE,
    ESUB_WEEKLY_VOLUME,
    RHAP_STDZ,
    PMS_STDZ,
]
