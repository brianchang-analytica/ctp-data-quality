"""Checks inside Rhapsody (the CTP review workflow): submissions, product links,
bundles, letters and review tasks.

Every check here looks at Rhapsody's own data only. Cross-system matches
(eSUB <-> Rhapsody, Rhapsody <-> PMS, Rhapsody <-> Reporting) live in their own
modules. See dq/checks/AUTHORING.md for the output contract.

Keys, company names, blank checks and flags use the shared macros in
dq/sqlfrag.py (STN_KEY, ANY_KEY, NUM_KEY, TXT, CO_NAME, BLANK), so a record is
matched the same way here as in every other module. The shared fragments below
are spliced into each check's query so that every check reads submissions,
bundles and letters the same way (same soft-delete rules, same de-duplication,
same "first plausible date" rule for record_date).
"""

from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check

_ALL_FILTERS = frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE})

# Earliest plausible business date: the Tobacco Control Act, which created CTP, was signed on 2009-06-22.
_MIN_DATE = "DATE '2009-06-22'"


def _plausible(expr: str) -> str:
    """The value as a DATE when it falls between 2009-06-22 and today, else NULL (not covered by the shared macros)."""
    return f"CASE WHEN CAST({expr} AS DATE) BETWEEN {_MIN_DATE} AND current_date() THEN CAST({expr} AS DATE) END"


def _first_plausible(*exprs: str, load: str) -> str:
    """record_date rule from AUTHORING.md: first plausible candidate date, else the date the row was loaded."""
    return "coalesce(" + ", ".join(_plausible(e) for e in exprs) + f", CAST({load} AS DATE))"


def _person(expr: str) -> str:
    """Readable person name from a value that may be a directory name like 'cn=jdoe,cn=users,o=fda'.

    Not covered by the shared macros; `expr` should already be BLANK()-normalized.
    """
    return f"coalesce(nullif(trim(regexp_extract({expr}, '(?i)cn=([^,]+)', 1)), ''), {expr})"


# Status words that mean a bundle's review is finished. Matched as whole words on the upper-cased status.
# Deliberately leaves out words like FINAL, DECISION, ISSUED and COMPLETE that also appear in in-progress steps.
# Confirmed on real data (Oct 2026), P2.11: bundlestatus values treated as finished are REFUSE TO ACCEPT, REFUSE TO
# FILE, WITHDRAWN, ADMINISTRATIVELY WITHDRAWN, ADMINISTRATIVELY CLOSED, CANCELLED, RESCINDED, DENIED, GRANTED and
# REMOVED FROM REVIEW. Still open: ACCEPTED, FILED, OCE REVIEW COMPLETED, CYCLE 1..5, RECEIVED, PAUSED,
# RFI LETTER ISSUED, OCE REVIEW PENDING, ENVIRONMENTAL.
_BUNDLE_CLOSED_RE = (
    r"\\b(CLOS\\w*|WITHDR\\w*|CANCEL\\w*|RESCIND\\w*|REJECT\\w*|REFUS\\w*|RTA|RTF|DENI\\w*|DECLIN\\w*|"
    r"TERMINAT\\w*|ARCHIV\\w*|INACTIVE|ABORT\\w*|VOID\\w*|ORDERS?|GRANTED|EXEMPT|NSE|DONE|"
    r"REMOVED\\s+FROM\\s+REVIEW)\\b"
)
# Lifecycle instance status (the workflow engine's own state for the bundle). Confirmed on real data (Oct 2026), P2.13:
# Complete 78%, In Progress 21%, New 0.8%; Complete means finished.
_INSTANCE_CLOSED_RE = r"\\b(COMPLET\\w*|CLOS\\w*|TERMINAT\\w*|ABORT\\w*|CANCEL\\w*|ARCHIV\\w*|INACTIVE|DONE)\\b"
# Task states that mean the task is no longer waiting on anyone.
_TASK_CLOSED_RE = (
    r"\\b(COMPLET\\w*|CANCEL\\w*|REVOK\\w*|SKIP\\w*|DELET\\w*|EXPIR\\w*|TERMINAT\\w*|ABORT\\w*|CLOS\\w*|"
    r"OBSOLETE|WITHDR\\w*|DONE|FINISH\\w*|ARCHIV\\w*|INACTIVE|RESOLVED)\\b"
)
# Task state codes that mean the task is done. Confirmed on real data (Oct 2026), P2.19 and P2.20: 5 = Completed,
# 8 = Obsolete; 1 = Created and 2 = Assigned are open. Obsolete tasks never get a completion date, so the code must be
# checked on its own.
_TASK_DONE_CODES = "('5', '8')"
_TASK_STATE_NAME = (
    "CASE ANY_KEY({x}) WHEN '1' THEN 'Created' WHEN '2' THEN 'Assigned' WHEN '5' THEN 'Completed' "
    "WHEN '8' THEN 'Obsolete' END"
)
# Submission / bundle types that never list products or carry a review goal date (matched on the upper-cased type).
# MR is deliberately not here: modified-risk applications use it and do have products.
_NON_REVIEW_TYPES_RE = r"\\b(MEETING|CORRESPOND\\w*|MASTER FILES?|TPMF|MF|GC)\\b"
# Standard submission type values that really mean "not recognized" (compared after TXT()).
_TYPE_PLACEHOLDERS = "('UNKNOWN', 'UNMAPPED', 'NOT MAPPED', 'N/A', 'NA', 'NONE', 'NOT PROVIDED', 'INVALID')"
# Values in free-text "error" fields that actually mean "no error" (compared after TXT()).
_NO_ERROR_VALUES = "('N', 'NO', 'FALSE', 'F', '0', 'NONE', 'NULL', 'N/A', 'NA', 'SUCCESS', 'SUCCESSFUL', 'OK', '-')"
# Values in submission_rejected_desc that mean "not rejected" (compared after TXT()).
_NOT_REJECTED_VALUES = "('N/A', 'NA', 'NONE', 'NULL', '-', '0', 'N', 'NO', 'FALSE', 'F')"

# --------------------------------------------------------------------------- #
# Shared fragments
# --------------------------------------------------------------------------- #

# One row per Rhapsody submission (latest copy per STN_KEY; rows with a blank STN are all kept).
# rec_date is the first plausible (2009-06-22..today) of received / submitted / created, else the load date,
# so a typo'd date never pushes a record outside the steward's date filter.
_SUBS = (
    r"""
  SELECT
    BLANK(stn)                                                   AS stn,
    STN_KEY(stn)                                                 AS stn_norm,
    BLANK(submission_type)                                       AS raw_type,
    BLANK(stdz_submission_type)                                  AS stdz_type,
    BLANK(organization)                                          AS office,
    BLANK(rhpm)                                                  AS rhpm,
    BLANK(submission_status)                                     AS sub_status,
    BLANK(version_type)                                          AS version_type,
    date_received,
    date_submitted,
    date_created,
    """ + _plausible("date_received") + r"""                    AS received_ok,
    """ + _first_plausible("date_received", "date_submitted", "date_created", load="halo_insert_ts") + r"""
                                                                 AS rec_date,
    BLANK(abpc_company_name)                                     AS company_contact,
    BLANK(itrac_company)                                         AS company_itrac,
    BLANK(abc_company)                                           AS company_abc,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company),
             BLANK(mvw_submission_org_company))                  AS company,
    BLANK(abpp_first_name)                                       AS contact_first,
    BLANK(abpp_last_name)                                        AS contact_last,
    BLANK(abpp_email)                                            AS contact_email,
    coalesce(no_products, false)                                 AS no_products,
    coalesce(TXT(submission_rejected_desc), 'N/A') NOT IN """ + _NOT_REJECTED_VALUES + r""" AS is_rejected
  FROM ${core}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
  QUALIFY STN_KEY(stn) IS NULL
       OR row_number() OVER (PARTITION BY STN_KEY(stn)
                             ORDER BY halo_update_ts DESC NULLS LAST, halo_insert_ts DESC NULLS LAST) = 1
"""
)

# Live Rhapsody bundles, one row per bundle ID, plus a steward-facing label (name, or name + ID when shared)
# and whether the bundle's review is finished.
_BUNDLES = (
    r"""
b0 AS (
  SELECT
    id,
    NUM_KEY(id)                                           AS bid,
    BLANK(bundlename)                                     AS bundle_name,
    bundlename                                            AS bundle_name_raw,
    coalesce(stdz_bundlename_contains_error, false)       AS name_flagged,
    BLANK(bundlestatus)                                   AS status,
    BLANK(s_lbb_current_state)                            AS lifecycle_state,
    BLANK(s_instance_status)                              AS instance_status,
    BLANK(submissiontype)                                 AS raw_type,
    BLANK(stdz_submission_type)                           AS stdz_type,
    BLANK(organization)                                   AS office,
    BLANK(rhpm)                                           AS rhpm_raw,
    torhpmuser_id                                         AS rhpm_user_id,
    goaldate,
    datereceivedstart,
    s_createddate,
    halo_insert_ts,
    totalproductsinbundle,
    coalesce(isduplicate, false)                          AS is_duplicate
  FROM ${core}.rhapsody_bundle
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
    AND id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY id ORDER BY halo_update_ts DESC NULLS LAST, s_lastmodifieddate DESC NULLS LAST) = 1
),
b AS (
  SELECT
    id, bid, bundle_name, bundle_name_raw, name_flagged, status, lifecycle_state, instance_status,
    raw_type, stdz_type, office, rhpm_user_id, goaldate, datereceivedstart, totalproductsinbundle, is_duplicate,
    """ + _person("rhpm_raw") + r"""                          AS rhpm,
    CAST(goaldate AS DATE)                                    AS goal_dt,
    CAST(datereceivedstart AS DATE)                           AS received_dt,
    """ + _first_plausible("datereceivedstart", "s_createddate", load="halo_insert_ts") + r""" AS bundle_rec_date,
    coalesce(upper(coalesce(status, '')) rlike '__BUNDLE_CLOSED__'
             OR upper(coalesce(lifecycle_state, '')) rlike '__BUNDLE_CLOSED__'
             OR upper(coalesce(instance_status, '')) rlike '__INSTANCE_CLOSED__', false) AS is_closed,
    CASE
      WHEN bundle_name IS NULL THEN concat('Bundle ID ', bid)
      WHEN count(*) OVER (PARTITION BY TXT(bundle_name)) > 1 THEN concat(bundle_name, ' (ID ', bid, ')')
      ELSE bundle_name
    END                                                       AS bundle_label
  FROM b0
)""".replace("__BUNDLE_CLOSED__", _BUNDLE_CLOSED_RE).replace("__INSTANCE_CLOSED__", _INSTANCE_CLOSED_RE)
)

# Every bundle ID Rhapsody knows about, one row per ID, including deleted ones so "deleted" can be told apart
# from "never existed". Temporary (draft) copies are ignored; when an ID has several rows a live one wins.
# `id` is BIGINT, so NUM_KEY(id) is one-to-one with it and `bid` is unique here (bundle IDs are unique: P4 U05).
_BUND_LOOKUP = (
    r"""
bund AS (
  SELECT
    NUM_KEY(id)                                                 AS bid,
    CASE WHEN coalesce(deleted, false) THEN 0 ELSE 1 END        AS is_live,
    BLANK(bundlename)                                           AS bundle_name,
    BLANK(stdz_submission_type)                                 AS stdz_type,
    BLANK(organization)                                         AS office,
    """ + _plausible("datereceivedstart") + r"""                AS received_start
  FROM ${core}.rhapsody_bundle
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
    AND id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY id
                             ORDER BY CASE WHEN coalesce(deleted, false) THEN 1 ELSE 0 END,
                                      halo_update_ts DESC NULLS LAST) = 1
)"""
)

# Live letters/communications, one row per letter, each resolved to ONE bundle.
# A letter names its bundle in two fields. Confirmed on real data (Oct 2026): the upload-to-bundle number
# (`uploadcommunicationstobundle_id`) reaches 97.4% of live bundles (K21); the bundle ID text (`bundleid`) only 75%
# (K19, K20). A letter is linked when EITHER field names a live bundle. Where one bundle is needed (its name, type,
# organization, received date, STNs), the upload-to-bundle number wins and `bundleid` is the fallback; a deleted
# bundle is used only when neither field names a live one. Letters are de-duplicated first (letter IDs are unique:
# P4 U06) and `bund` has one row per bundle ID, so two fields naming different bundles still give one row per letter.
_LETTERS = (
    r"""
comms AS (
  SELECT
    id, stdz_communicationstypelocal, communicationstypelocal, communicationtype, issuedate, industryduedate,
    uploadeddate, s_lastmodified, halo_insert_ts, finalfilename, doc_filename, s_filename, doc_guid, noupload,
    failuremsg, stdz_failuremsg, esuberror, ruleerrorbypass, ruleerrorbypassocebulkupload,
    NUM_KEY(uploadcommunicationstobundle_id)                      AS upload_bid,
    ANY_KEY(bundleid)                                             AS field_bid,
    BLANK(bundleid)                                               AS field_ref
  FROM ${core}.rhapsody_communications
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
    AND id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY id ORDER BY halo_update_ts DESC NULLS LAST, s_lastmodified DESC NULLS LAST) = 1
),
letters0 AS (
  SELECT
    c.id                                                          AS letter_id,
    coalesce(BLANK(c.stdz_communicationstypelocal), BLANK(c.communicationstypelocal),
             BLANK(c.communicationtype))                          AS letter_type,
    c.issuedate,
    c.industryduedate,
    """ + _first_plausible("c.issuedate", "c.uploadeddate", "c.s_lastmodified", load="c.halo_insert_ts") + r"""
                                                                  AS letter_date,
    coalesce(BLANK(c.finalfilename), BLANK(c.doc_filename), BLANK(c.s_filename)) AS file_name,
    BLANK(c.doc_guid)                                             AS doc_guid,
    coalesce(c.noupload, false)                                   AS no_upload,
    BLANK(c.failuremsg)                                           AS failure_msg,
    BLANK(c.stdz_failuremsg)                                      AS failure_group,
    BLANK(c.esuberror)                                            AS esub_error,
    coalesce(c.ruleerrorbypass, false) OR coalesce(c.ruleerrorbypassocebulkupload, false) AS error_bypassed,
    c.upload_bid,
    c.field_ref,
    coalesce(c.upload_bid <> c.field_bid, false)                  AS ids_differ,
    CASE WHEN coalesce(c.upload_bid <> c.field_bid, false) THEN concat(c.upload_bid, ' / ', c.field_ref)
         ELSE coalesce(c.upload_bid, c.field_ref) END             AS bundle_ref,
    coalesce(b1.is_live, 0) = 1 OR coalesce(b2.is_live, 0) = 1    AS linked_live,
    CASE WHEN b1.is_live = 1 THEN b1.bid
         WHEN b2.is_live = 1 THEN b2.bid
         ELSE coalesce(b1.bid, b2.bid) END                        AS matched_bid
  FROM comms c
  LEFT JOIN bund b1 ON c.upload_bid = b1.bid
  LEFT JOIN bund b2 ON c.field_bid = b2.bid
),
letters AS (
  SELECT
    l.letter_id, l.letter_type, l.issuedate, l.industryduedate, l.letter_date, l.file_name, l.doc_guid, l.no_upload,
    l.failure_msg, l.failure_group, l.esub_error, l.error_bypassed, l.upload_bid, l.field_ref, l.ids_differ,
    l.bundle_ref, l.linked_live, l.matched_bid,
    bd.is_live        AS bundle_live,
    bd.bundle_name,
    bd.stdz_type,
    bd.office,
    bd.received_start
  FROM letters0 l
  LEFT JOIN bund bd ON l.matched_bid = bd.bid
)"""
)

# (bundle, product, STN) product links from the two Rhapsody product lists that carry a bundle, one row per source
# row. Confirmed on real data (Oct 2026): 99.5% of bundle IDs on the regular links are live bundles (K13, K14). The
# bulk list holds 24.4M rows on only 23 bundles, all live (K16), and 10 STNs, all Rhapsody submissions (K47). It
# shares no product IDs with the regular list except one (K15). The caller reduces this to one row per
# (bundle, product) before any match to another list, so the bulk rows are read once and never multiplied. The
# reduced row keeps its row count, so a placeholder product ID (see PLACEHOLDER_ID_ROWS) can be counted row by row.
_BUNDLE_PRODUCT_LINKS = r"""
    SELECT ANY_KEY(bundle_id) AS bid, ANY_KEY(product_id) AS pid, STN_KEY(stn) AS stn_norm
    FROM ${core}.rhapsody_submission_products
    WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT NUM_KEY(bundle_id), ANY_KEY(product_id), STN_KEY(stn)
    FROM ${core}.rhapsody_bulk_products
    WHERE coalesce(halo_is_deleted, false) = false
"""

# Distinct (bundle, STN) pairs from the same two lists. Each list is reduced on its own before the union, so the
# 24.4M bulk rows collapse to at most a few hundred pairs (23 bundles x 10 STNs) before anything else touches them.
_BUNDLE_STNS = r"""
    SELECT DISTINCT ANY_KEY(bundle_id) AS bid, STN_KEY(stn) AS stn_norm
    FROM ${core}.rhapsody_submission_products
    WHERE coalesce(halo_is_deleted, false) = false
    UNION
    SELECT DISTINCT NUM_KEY(bundle_id), STN_KEY(stn)
    FROM ${core}.rhapsody_bulk_products
    WHERE coalesce(halo_is_deleted, false) = false
"""

# PLACEHOLDER PRODUCT ID. Confirmed on real data (Oct 2026), P4 U07: one Rhapsody product_id value sits on 14,359
# rows of the regular + bulk product lists, each a different product (different names); every other product ID is
# unique. COUNT(DISTINCT product_id) folds all of those products into one, so a product ID on more than
# PLACEHOLDER_ID_ROWS product rows is a placeholder and its rows are counted one by one. The placeholder values are
# found from the data on every run (never hard-coded). Same threshold as dq/checks/rhapsody_pms.py.
PLACEHOLDER_ID_ROWS = 100

# Every Rhapsody product ID on the regular + bulk product lists, one row per ANY_KEY(product_id), with the number of
# product rows that carry it and whether that makes it a placeholder.
_PRODUCT_IDS = (
    r"""
prod_ids AS (
  SELECT pid, count(*) AS n_rows, count(*) > """ + str(PLACEHOLDER_ID_ROWS) + r""" AS is_placeholder
  FROM (
    SELECT ANY_KEY(product_id) AS pid FROM ${core}.rhapsody_products WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT ANY_KEY(product_id) AS pid FROM ${core}.rhapsody_bulk_products WHERE coalesce(halo_is_deleted, false) = false
  ) u
  WHERE pid IS NOT NULL
  GROUP BY pid
)"""
)

# Placeholder product IDs only, one row per ID, so a match to it can never multiply rows.
_PLACEHOLDER_IDS = r"""
ph AS (
  SELECT pid, n_rows FROM prod_ids WHERE is_placeholder
)"""


CHECKS: list[Check] = [
    # ------------------------------------------------------------------ #
    # Completeness
    # ------------------------------------------------------------------ #
    Check(
        id="RHAP_SUB_MISSING_COUNT_FIELDS",
        title="Submissions missing STN, type, date or organization",
        systems=("rhapsody",),
        entity="Submissions",
        dimension="Completeness",
        severity="High",
        question="Does every Rhapsody submission have an STN, a recognized submission type, a received date and an "
                 "organization?",
        why="These four details place a submission in the right official counts and review clocks. "
            "A blank one drops the submission from reports by organization or type. "
            "Without a received date, nobody can measure its review time.",
        how_to_fix="Open the submission in Rhapsody and fill in the blank field from the eSUB / iTRAC record. "
                   "If the type is filled in but not recognized, check its spelling against the standard type list. "
                   "If it is a new type, ask the data team to add it.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Source: `slvr_ctp_core.rhapsody_submissions`, rows with `halo_is_deleted` excluded (the table has no source "
            "delete flag), de-duplicated to the latest copy (`halo_update_ts`) per STN_KEY(stn); blank-STN rows are all "
            "kept and share the record ID '(no STN)'. The standard type counts as recognized when TXT(`stdz_submission_type`) "
            "is filled in and is not a placeholder (UNKNOWN / UNMAPPED / NOT MAPPED / N/A / NA / NONE / NOT PROVIDED / "
            "INVALID). Flags: blank `stn`; no recognized standard type and blank `submission_type` (= blank type); no "
            "recognized standard type but `submission_type` filled in (= not recognized); blank `date_received`; blank "
            "`organization`. Denominator: all live Rhapsody submissions. `record_date` = first of received / submitted / "
            "created between 2009-06-22 and today, else the load date (`halo_insert_ts`). Confirmed on real data (Oct "
            "2026): `organization` is the applicant company (3,679 distinct values such as 'Philip Morris USA Inc.', "
            "P2.17), shown to stewards as Organization. It is still output in the `office` contract field. Standard "
            "types seen are SE, PMTA, Other, EX and MRTPA (P2.16)."
        ),
        assumptions=(
            "When a submission has a type but no standard type, the type did not match the standard list. "
            "A late update of the standard list is not the cause.",
            "Every submission should have an organization and a received date once it reaches Rhapsody. "
            "Amendments are no exception.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
f AS (
  SELECT
    stn, raw_type, stdz_type, office, company, date_received, date_submitted, date_created, rec_date,
    stn IS NULL                                                       AS no_stn,
    raw_type IS NULL
      AND NOT coalesce(TXT(stdz_type) NOT IN """ + _TYPE_PLACEHOLDERS + r""", false) AS no_type,
    raw_type IS NOT NULL
      AND NOT coalesce(TXT(stdz_type) NOT IN """ + _TYPE_PLACEHOLDERS + r""", false) AS type_unrecognized,
    date_received IS NULL                                             AS no_received,
    office IS NULL                                                    AS no_office
  FROM s
),
g AS (
  SELECT
    stn, raw_type, stdz_type, office, company, date_received, date_submitted, date_created, rec_date,
    no_stn, no_type, type_unrecognized, no_received, no_office,
    (CASE WHEN no_stn THEN 1 ELSE 0 END) + (CASE WHEN no_type THEN 1 ELSE 0 END)
      + (CASE WHEN type_unrecognized THEN 1 ELSE 0 END) + (CASE WHEN no_received THEN 1 ELSE 0 END)
      + (CASE WHEN no_office THEN 1 ELSE 0 END)                       AS n_problems,
    nullif(concat_ws(', ',
      CASE WHEN no_stn THEN 'STN' END,
      CASE WHEN no_type THEN 'submission type' END,
      CASE WHEN no_received THEN 'received date' END,
      CASE WHEN no_office THEN 'organization' END), '')               AS blank_list
  FROM f
)
SELECT
  coalesce(stn, '(no STN)')                                           AS record_key,
  CAST(rec_date AS DATE)                                              AS record_date,
  CAST(stdz_type AS STRING)                                           AS submission_type,
  CAST(office AS STRING)                                              AS office,
  coalesce(n_problems > 0, false)                                     AS is_issue,
  CASE
    WHEN n_problems > 1 THEN 'Several problems'
    WHEN no_stn THEN 'Blank STN'
    WHEN no_type THEN 'Blank type'
    WHEN type_unrecognized THEN 'Type not recognized'
    WHEN no_received THEN 'Blank received date'
    WHEN no_office THEN 'Blank organization'
  END                                                                 AS issue_category,
  CASE WHEN n_problems > 0 THEN concat_ws(' ',
    CASE WHEN blank_list IS NOT NULL THEN concat('Blank in Rhapsody: ', blank_list, '.') END,
    CASE WHEN type_unrecognized THEN
      concat('Submission type "', raw_type, '" is not on the standard list.') END)
  END                                                                 AS issue_reason,
  stn                                                                 AS `STN`,
  company                                                             AS `Company`,
  raw_type                                                            AS `Type as entered`,
  stdz_type                                                           AS `Standard type`,
  date_received                                                       AS `Received date`,
  date_submitted                                                      AS `Submitted date`,
  office                                                              AS `Organization`,
  date_created                                                        AS `Created in Rhapsody`
FROM g
""",
    ),
    Check(
        id="RHAP_SUB_MISSING_COMPANY_CONTACT_RHPM",
        title="Submissions missing contact details or project manager",
        systems=("rhapsody",),
        entity="Companies & contacts",
        dimension="Completeness",
        severity="Medium",
        question="Does every Rhapsody submission name the company and a contact person with a usable email? "
                 "After 30 days, does it also have a project manager?",
        why="A blank company hides the submission from company searches and reports. A missing contact or project "
            "manager leaves letters with no recipient and the review with no owner.",
        how_to_fix="Open the submission in Rhapsody. Copy the company and contact details from the eSUB / iTRAC record, "
                   "and fix any mistyped email. If no project manager is assigned, ask the review team lead to "
                   "assign one.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Source: `slvr_ctp_core.rhapsody_submissions` (`halo_is_deleted` excluded, latest copy per STN_KEY(stn)). "
            "Company is blank when BLANK() of `abpc_company_name`, `itrac_company`, `abc_company` and "
            "`mvw_submission_org_company` are all blank. Contact: `abpp_first_name` / `abpp_last_name` / `abpp_email`; "
            "an email counts as bad when it does not start with `something@something.something` (a list of several "
            "addresses passes if the first is well formed). Project manager (`rhpm`) is required only when `record_date` "
            "(first plausible of received / submitted / created, else load date) is more than 30 days ago and "
            "`submission_rejected_desc` is blank or a no-value word (N/A, NONE, NO, ...). Directory names such as "
            "'cn=jdoe,...' are shown as the cn part. Denominator: all live Rhapsody submissions."
        ),
        assumptions=(
            "CTP assigns a project manager within 30 days of receipt, so newer submissions are not checked for one.",
            "Submissions with a rejection reason never get a project manager, so they are not flagged for one.",
            "The contact person fields hold the company's contact for this submission, and every submission should have one.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
f AS (
  SELECT
    stn, stdz_type, office, company, rhpm, date_received, rec_date, contact_first, contact_last, contact_email,
    company IS NULL                                                         AS no_company,
    contact_first IS NULL AND contact_last IS NULL AND contact_email IS NULL AS no_contact,
    (contact_first IS NOT NULL OR contact_last IS NOT NULL) AND contact_email IS NULL AS no_email,
    contact_email IS NOT NULL
      AND NOT coalesce(contact_email rlike '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+', false) AS bad_email,
    rhpm IS NULL AND NOT is_rejected
      AND (rec_date IS NULL OR rec_date <= date_sub(current_date(), 30))  AS no_rhpm
  FROM s
),
g AS (
  SELECT
    stn, stdz_type, office, company, rhpm, date_received, rec_date, contact_first, contact_last, contact_email,
    no_company, no_contact, no_email, bad_email, no_rhpm,
    (CASE WHEN no_company THEN 1 ELSE 0 END) + (CASE WHEN no_contact THEN 1 ELSE 0 END)
      + (CASE WHEN no_email THEN 1 ELSE 0 END) + (CASE WHEN bad_email THEN 1 ELSE 0 END)
      + (CASE WHEN no_rhpm THEN 1 ELSE 0 END)                               AS n_problems,
    nullif(concat_ws(', ',
      CASE WHEN no_company THEN 'company' END,
      CASE WHEN no_contact THEN 'contact person' END,
      CASE WHEN no_email THEN 'contact email' END), '')                     AS blank_list
  FROM f
)
SELECT
  coalesce(stn, '(no STN)')                                                 AS record_key,
  CAST(rec_date AS DATE)                                                    AS record_date,
  CAST(stdz_type AS STRING)                                                 AS submission_type,
  CAST(office AS STRING)                                                    AS office,
  coalesce(n_problems > 0, false)                                           AS is_issue,
  CASE
    WHEN n_problems > 1 THEN 'Several problems'
    WHEN no_company THEN 'Blank company'
    WHEN no_contact THEN 'No contact person'
    WHEN no_email THEN 'No contact email'
    WHEN bad_email THEN 'Bad contact email'
    WHEN no_rhpm THEN 'No project manager'
  END                                                                       AS issue_category,
  CASE WHEN n_problems > 0 THEN concat_ws(' ',
    CASE WHEN blank_list IS NOT NULL THEN concat('Blank in Rhapsody: ', blank_list, '.') END,
    CASE WHEN bad_email THEN concat('Contact email "', contact_email, '" is not a valid email address.') END,
    CASE WHEN no_rhpm THEN concat('Received ', coalesce(CAST(date_received AS STRING), 'on an unknown date'),
                                  ' and still has no project manager.') END)
  END                                                                       AS issue_reason,
  stn                                                                       AS `STN`,
  company                                                                   AS `Company`,
  nullif(concat_ws(' ', contact_first, contact_last), '')                   AS `Contact person`,
  contact_email                                                             AS `Contact email`,
  """ + _person("rhpm") + r"""                                              AS `Project manager`,
  date_received                                                             AS `Received date`,
  stdz_type                                                                 AS `Submission type`,
  office                                                                    AS `Organization`
FROM g
""",
    ),
    Check(
        id="RHAP_SUB_NO_PRODUCTS",
        title="Submissions with no linked products",
        systems=("rhapsody",),
        entity="Submissions",
        dimension="Completeness",
        severity="High",
        question="Does every Rhapsody submission older than 30 days have a linked product, unless it is marked "
                 "'no products'?",
        why="Products under review and decisions per product are official metrics. A submission with no linked "
            "products is missing from every product count. A submission marked 'no products' that has products "
            "drops out the same way.",
        how_to_fix="Open the submission in Rhapsody and compare its product list with the eSUB submission. "
                   "Link the missing products, or tick 'no products' if the submission truly has none. "
                   "If it is marked 'no products' but has products, clear the mark.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: live `rhapsody_submissions` (latest copy per STN_KEY(stn), non-blank STN) whose `record_date` "
            "(first plausible of received / submitted / created, else load date) is more than 30 days ago. Submissions "
            "with a rejection reason are excluded. So are standard (or raw) types matching "
            "MEETING|CORRESPOND*|MASTER FILE(S)|TPMF|MF|GC as whole words. MR is deliberately not excluded because "
            "modified-risk applications use it and have products. Products linked = distinct ANY_KEY(product id) for "
            "the STN_KEY across `rhapsody_submission_products`, `rhapsody_bulk_products` and "
            "`rhapsody_submission_product_wrapper` (`untitled_relationship_stn` / `untitled_relationship_product_id`, "
            "temporary copies excluded). The three lists are unioned and reduced to one row per (STN, product ID) with "
            "its row count on the link lists (regular + bulk) and on the wrapper, then summed to one row per STN before "
            "the match to submissions, so the match cannot multiply rows and a product on two lists counts once. "
            "Placeholder product ID (P4 U07): one product_id value sits on 14,359 product rows, each a different "
            "product, so COUNT(DISTINCT product_id) would count all of them as one. A product ID on more than "
            "PLACEHOLDER_ID_ROWS (100) rows of `rhapsody_products` + `rhapsody_bulk_products` is found from the data "
            "(CTEs `prod_ids` / `ph`, one row per product ID, so the match to it cannot multiply rows) and its rows are "
            "counted one by one: the larger of its link-list and wrapper row counts for that STN. The larger count, "
            "not the sum, is used because the wrapper is assumed to repeat links already on the other lists. Every "
            "other product ID counts once per STN. Issue when 0 products and `no_products` is not true, or > 0 products and `no_products` is "
            "true. Amendments (`version_type` containing AMEND) get their own problem type. Confirmed on real data "
            "(Oct 2026): every STN on the product links and the product wrapper is a Rhapsody submission (K11, K18), and "
            "every product ID on them is on the regular or bulk product list (K12, K17). The bulk list holds 24.4M rows "
            "on only 10 STNs, all Rhapsody submissions (K47), and shares no product IDs with the regular list except one "
            "(K15), so those 10 STNs show product counts in the millions. Only 25,869 of 46,821 submission STNs have a "
            "regular product link (K11), so the type and no-products exclusions carry much of this check."
        ),
        assumptions=(
            "Staff link products within 30 days of receipt, so newer submissions are not checked yet.",
            "Meeting requests and master files never list products. General correspondence never does either. "
            "Words in the submission type identify these records.",
            "Submissions with a rejection reason are not expected to have products.",
            "Amendments should list their own products. If they never do, drop amendments from this check. "
            "The 'amendment' problem type shows how many records that affects.",
            "Products that share the one placeholder product ID are counted once per row. Rhapsody's product wrapper "
            "list is taken to repeat links already on the other lists, so only the larger count is used.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
""" + _PRODUCT_IDS + r""",
""" + _PLACEHOLDER_IDS + r""",
lp AS (
  SELECT stn_norm, pid,
         count_if(NOT on_wrapper) AS n_link_rows,
         count_if(on_wrapper)     AS n_wrapper_rows
  FROM (
    SELECT STN_KEY(stn) AS stn_norm, ANY_KEY(product_id) AS pid, false AS on_wrapper
    FROM ${core}.rhapsody_submission_products
    WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT STN_KEY(stn), ANY_KEY(product_id), false
    FROM ${core}.rhapsody_bulk_products
    WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT STN_KEY(untitled_relationship_stn), ANY_KEY(untitled_relationship_product_id), true
    FROM ${core}.rhapsody_submission_product_wrapper
    WHERE coalesce(halo_is_deleted, false) = false AND coalesce(s_is_temporary_copy, false) = false
  ) u
  WHERE stn_norm IS NOT NULL AND pid IS NOT NULL
  GROUP BY stn_norm, pid
),
links AS (
  -- One row per STN. A product ID counts once, except a placeholder ID, whose rows are separate products.
  SELECT lp.stn_norm,
         sum(CASE WHEN ph.pid IS NULL THEN 1 ELSE greatest(lp.n_link_rows, lp.n_wrapper_rows) END) AS n_products
  FROM lp
  LEFT JOIN ph ON lp.pid = ph.pid
  GROUP BY lp.stn_norm
),
f AS (
  SELECT
    s.stn, s.stdz_type, s.raw_type, s.office, s.company, s.date_received, s.rec_date, s.version_type,
    s.no_products                                                       AS flagged_no_products,
    coalesce(TXT(s.version_type) rlike 'AMEND', false)                  AS is_amendment,
    coalesce(l.n_products, 0)                                           AS n_products
  FROM s
  LEFT JOIN links l ON s.stn_norm = l.stn_norm
  WHERE s.stn_norm IS NOT NULL
    AND NOT s.is_rejected
    AND (s.rec_date IS NULL OR s.rec_date <= date_sub(current_date(), 30))
    AND NOT coalesce(upper(coalesce(s.stdz_type, s.raw_type, '')) rlike '""" + _NON_REVIEW_TYPES_RE + r"""', false)
)
SELECT
  stn                                                               AS record_key,
  CAST(rec_date AS DATE)                                            AS record_date,
  CAST(stdz_type AS STRING)                                         AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce((n_products = 0 AND NOT flagged_no_products) OR (n_products > 0 AND flagged_no_products), false) AS is_issue,
  CASE
    WHEN n_products = 0 AND NOT flagged_no_products AND is_amendment THEN 'No products (amendment)'
    WHEN n_products = 0 AND NOT flagged_no_products THEN 'No products linked'
    WHEN n_products > 0 AND flagged_no_products THEN 'Wrong no-products mark'
  END                                                               AS issue_category,
  CASE
    WHEN n_products = 0 AND NOT flagged_no_products THEN
      concat(CASE WHEN is_amendment THEN 'Amendment received ' ELSE 'Received ' END,
             coalesce(CAST(date_received AS STRING), 'on an unknown date'),
             ' and still has no products linked in Rhapsody.')
    WHEN n_products > 0 AND flagged_no_products THEN
      concat('Marked "no products", but ', CAST(n_products AS STRING), ' product(s) are linked.')
  END                                                               AS issue_reason,
  stn                                                               AS `STN`,
  company                                                           AS `Company`,
  coalesce(stdz_type, raw_type)                                     AS `Submission type`,
  version_type                                                      AS `Original or amendment`,
  date_received                                                     AS `Received date`,
  n_products                                                        AS `Products linked`,
  CASE WHEN flagged_no_products THEN 'Yes' ELSE 'No' END            AS `Marked no products`,
  office                                                            AS `Organization`
FROM f
""",
    ),
    Check(
        id="RHAP_SUB_PRODUCT_LINKS_BROKEN",
        title="Submissions linked to missing products or bundles",
        systems=("rhapsody",),
        entity="Products",
        dimension="Completeness",
        severity="Medium",
        question="Does every Rhapsody product link point to a product and a bundle that exist? "
                 "Does its STN appear in the submission list?",
        why="A link to a missing product or bundle shows as a blank in reports. Product counts per submission and "
            "per bundle then come out wrong.",
        how_to_fix="Open the STN in Rhapsody and review its product list. Re-link each missing product from the current "
                   "product list, since it may have a new product ID. Move links to the right bundle, or remove "
                   "leftover links. If the STN itself is missing from Rhapsody, report it to the Rhapsody support team.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: every distinct STN_KEY(stn) in `rhapsody_submission_products` (`halo_is_deleted` excluded). Each "
            "link's ANY_KEY(`product_id`) is matched against ANY_KEY(`product_id`) of live `rhapsody_products` UNION ALL "
            "`rhapsody_bulk_products`, reduced to one row per product ID with its row count (CTE `prod_ids`); each link's ANY_KEY(`bundle_id`) (STRING) is matched against NUM_KEY(`rhapsody_bundle.id`) "
            "(one row per ID, temporary copies ignored, deleted bundles kept so they can be reported as deleted). Product "
            "and bundle lists are de-duplicated before matching. Links are then aggregated per STN and matched to the "
            "de-duplicated `rhapsody_submissions` list. Links with a blank `bundle_id` are not flagged (not yet "
            "bundled). Inactive products (`active` / `inactive_date`) still count as existing. `record_date` = the "
            "submission's record date, else the date the first link was loaded. Confirmed on real data (Oct 2026): "
            "every STN on the product links is a Rhapsody submission (K11) and every linked product ID is on the "
            "regular or bulk product list (K12), so 'Unknown STN' and 'Unknown product' should stay near zero. 99.5% of "
            "linked bundle IDs are live bundles, and reading the text ID as a number changes nothing (K13 and K14 "
            "match), so most flags here are links to deleted or missing bundles. The bulk list's product IDs are kept "
            "in the product lookup because a link may name one (K12 checks both lists together). Products linked = "
            "distinct product IDs per STN, except the placeholder product ID (P4 U07: one product_id value on 14,359 "
            "product rows, each a different product). A product ID on more than PLACEHOLDER_ID_ROWS (100) rows of the "
            "two product lists is a placeholder (found from the data in `prod_ids`, never hard-coded), and each link "
            "row that names it counts as one product, since COUNT(DISTINCT product_id) would fold them into one. A "
            "placeholder ID is on the product list, so it is never counted as 'Unknown product'."
        ),
        assumptions=(
            "A link with a blank bundle is a product not yet placed in a bundle. It is not an error.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
""" + _BUND_LOOKUP + r""",
lnk AS (
  SELECT
    STN_KEY(stn)                                AS stn_norm,
    BLANK(stn)                                  AS stn,
    BLANK(product_id)                           AS product_id,
    ANY_KEY(product_id)                         AS pid,
    BLANK(bundle_id)                            AS bundle_id,
    ANY_KEY(bundle_id)                          AS bid,
    CAST(halo_insert_ts AS DATE)                AS load_dt
  FROM ${core}.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
),
""" + _PRODUCT_IDS + r""",
chk AS (
  SELECT
    l.stn_norm, l.stn, l.product_id, l.pid, l.bundle_id, l.bid, l.load_dt,
    p.pid IS NOT NULL                      AS product_found,
    coalesce(p.is_placeholder, false)      AS is_placeholder,
    bd.bid IS NOT NULL                     AS bundle_found,
    coalesce(bd.is_live, 0) = 1            AS bundle_live
  FROM lnk l
  LEFT JOIN prod_ids p ON l.pid = p.pid
  LEFT JOIN bund bd ON l.bid = bd.bid
  WHERE l.stn_norm IS NOT NULL
),
g AS (
  SELECT
    stn_norm,
    min(stn)                                                                          AS stn,
    min(load_dt)                                                                      AS first_loaded,
    -- a product ID counts once, except a placeholder ID, whose link rows are separate products
    count(DISTINCT CASE WHEN NOT is_placeholder THEN pid END) + count_if(is_placeholder) AS n_products,
    count(DISTINCT CASE WHEN pid IS NOT NULL AND NOT product_found THEN pid END)      AS n_bad_products,
    array_join(slice(array_sort(collect_set(
      CASE WHEN pid IS NOT NULL AND NOT product_found THEN product_id END)), 1, 10), ', ') AS bad_products,
    count(DISTINCT CASE WHEN bid IS NOT NULL AND NOT bundle_found THEN bid END)       AS n_bad_bundles,
    array_join(slice(array_sort(collect_set(
      CASE WHEN bid IS NOT NULL AND NOT bundle_found THEN bundle_id END)), 1, 10), ', ') AS bad_bundles,
    count(DISTINCT CASE WHEN bundle_found AND NOT bundle_live THEN bid END)           AS n_deleted_bundles,
    array_join(slice(array_sort(collect_set(
      CASE WHEN bundle_found AND NOT bundle_live THEN bundle_id END)), 1, 10), ', ')  AS deleted_bundles
  FROM chk
  GROUP BY stn_norm
),
f AS (
  SELECT
    g.stn_norm, coalesce(s.stn, g.stn) AS stn, g.n_products, g.n_bad_products, g.bad_products, g.n_bad_bundles,
    g.bad_bundles, g.n_deleted_bundles, g.deleted_bundles,
    s.stn_norm IS NULL                                            AS sub_missing,
    s.stdz_type, s.office, s.company, s.date_received,
    coalesce(s.rec_date, g.first_loaded)                          AS rec_date,
    (CASE WHEN s.stn_norm IS NULL THEN 1 ELSE 0 END) + (CASE WHEN g.n_bad_products > 0 THEN 1 ELSE 0 END)
      + (CASE WHEN g.n_bad_bundles > 0 THEN 1 ELSE 0 END) + (CASE WHEN g.n_deleted_bundles > 0 THEN 1 ELSE 0 END)
                                                                  AS n_problem_kinds
  FROM g
  LEFT JOIN s ON g.stn_norm = s.stn_norm
)
SELECT
  coalesce(stn, stn_norm)                                         AS record_key,
  CAST(rec_date AS DATE)                                          AS record_date,
  CAST(stdz_type AS STRING)                                       AS submission_type,
  CAST(office AS STRING)                                          AS office,
  coalesce(n_problem_kinds > 0, false)                            AS is_issue,
  CASE
    WHEN n_problem_kinds > 1 THEN 'Several broken links'
    WHEN sub_missing THEN 'Unknown STN'
    WHEN n_bad_products > 0 THEN 'Unknown product'
    WHEN n_bad_bundles > 0 THEN 'Unknown bundle'
    WHEN n_deleted_bundles > 0 THEN 'Deleted bundle'
  END                                                             AS issue_category,
  CASE WHEN n_problem_kinds > 0 THEN concat_ws(' ',
    CASE WHEN sub_missing THEN 'This STN has product links but is not in the Rhapsody submission list.' END,
    CASE WHEN n_bad_products > 0 THEN
      concat(CAST(n_bad_products AS STRING), ' of ', CAST(n_products AS STRING),
             ' linked product(s) are not in the Rhapsody product list: ', bad_products,
             CASE WHEN n_bad_products > 10 THEN ' and more' ELSE '' END, '.') END,
    CASE WHEN n_bad_bundles > 0 THEN
      concat('Products are linked to bundle ID(s) ', bad_bundles,
             CASE WHEN n_bad_bundles > 10 THEN ' and more' ELSE '' END, ' that do not exist in Rhapsody.') END,
    CASE WHEN n_deleted_bundles > 0 THEN
      concat('Products are still linked to deleted bundle ID(s) ', deleted_bundles,
             CASE WHEN n_deleted_bundles > 10 THEN ' and more' ELSE '' END, '.') END)
  END                                                             AS issue_reason,
  coalesce(stn, stn_norm)                                         AS `STN`,
  company                                                         AS `Company`,
  n_products                                                      AS `Products linked`,
  n_bad_products                                                  AS `Products not found`,
  bad_products                                                    AS `Missing product IDs`,
  nullif(concat_ws(', ', bad_bundles, deleted_bundles), '')       AS `Missing or deleted bundles`,
  date_received                                                   AS `Received date`,
  office                                                          AS `Organization`
FROM f
""",
    ),
    Check(
        id="RHAP_LETTER_NO_BUNDLE",
        title="Letters not linked to an existing bundle",
        systems=("rhapsody",),
        entity="Letters & communications",
        dimension="Completeness",
        severity="High",
        question="Is every Rhapsody letter linked to a bundle that still exists?",
        why="Letters are counted under their bundle. A letter with a missing or deleted bundle drops out of official "
            "letter counts. It also loses its tie to the submission it answered.",
        how_to_fix="Find the letter in Rhapsody by its letter ID or file name, and attach it to the right bundle. "
                   "If its old bundle was deleted on purpose, move the letter to the replacement bundle.",
        record_label="letters",
        filters=_ALL_FILTERS,
        date_label="Letter issue or upload date",
        technical_note=(
            "Source: `rhapsody_communications`, excluding `halo_is_deleted`, `deleted` = true and temporary copies "
            "(`s_is_temporary_copy`), de-duplicated to one row per `id` (latest `halo_update_ts`) before any bundle "
            "lookup. A letter's bundle is looked up through both NUM_KEY(`uploadcommunicationstobundle_id`) (BIGINT) "
            "and ANY_KEY(`bundleid`) (STRING) against NUM_KEY(`rhapsody_bundle.id`), using a bundle list with one row "
            "per ID that ignores temporary copies but keeps deleted bundles. Each lookup returns at most one bundle, so "
            "a letter whose two fields name different bundles still gives one row. Passes when EITHER field reaches a "
            "live bundle. The bundle used for the name, submission type and organization (bundle `organization`, the "
            "applicant company) is the upload-to-bundle one when it is live, else the `bundleid` one when it is live, "
            "else whichever deleted bundle a field names (upload-to-bundle first). Letters whose bundle is missing show "
            "as '(not recorded)' under the type and organization filters. When the two fields differ, 'Bundle ID on "
            "letter' shows both (upload-to-bundle first). `record_date` = first plausible (2009-06-22..today) of "
            "`issuedate`, `uploadeddate`, `s_lastmodified`, else the load date. Confirmed on real data (Oct 2026): the "
            "upload-to-bundle number reaches 97.4% of live bundles (K21) and the `bundleid` text 75% (K19, K20); about "
            "97-98% of the values in each field name a live bundle. A letter is linked when either field matches a "
            "live bundle. 98.1% of letters in the 100 largest letter types are linked this way (P2.21); types with "
            "1,000 or more letters range from 94.1% (Epidemiology Review) to 100%."
        ),
        assumptions=(
            "Every letter here should belong to a bundle. Rhapsody keeps meeting and refuse-to-accept letters in a "
            "separate place.",
        ),
        sql=r"""
WITH """ + _BUND_LOOKUP + r""",
""" + _LETTERS + r"""
SELECT
  CAST(letter_id AS STRING)                                       AS record_key,
  letter_date                                                     AS record_date,
  CAST(stdz_type AS STRING)                                       AS submission_type,
  CAST(office AS STRING)                                          AS office,
  coalesce(NOT linked_live, true)                                 AS is_issue,
  CASE WHEN NOT linked_live THEN
    CASE
      WHEN bundle_ref IS NULL THEN 'No bundle'
      WHEN matched_bid IS NULL THEN 'Unknown bundle'
      ELSE 'Deleted bundle'
    END
  END                                                             AS issue_category,
  CASE WHEN NOT linked_live THEN
    concat(coalesce(letter_type, 'Letter'), ' ', CAST(letter_id AS STRING),
      CASE
        WHEN bundle_ref IS NULL THEN
          concat(' (issued ', coalesce(CAST(CAST(issuedate AS DATE) AS STRING), 'on an unknown date'),
                 ') has no bundle ID.')
        WHEN matched_bid IS NULL AND ids_differ THEN
          concat(' points to bundles ', upload_bid, ' and ', field_ref, ', and neither is in Rhapsody.')
        WHEN matched_bid IS NULL THEN
          concat(' points to bundle ', bundle_ref, ', which is not in Rhapsody.')
        ELSE
          concat(' points to bundle ', matched_bid, coalesce(concat(' (', bundle_name, ')'), ''), ', which was deleted.',
                 CASE WHEN ids_differ THEN
                   concat(' Its other bundle ID, ', CASE WHEN matched_bid <=> upload_bid THEN field_ref ELSE upload_bid END,
                          ', is not a live bundle either.')
                 ELSE '' END)
      END)
  END                                                             AS issue_reason,
  CAST(letter_id AS STRING)                                       AS `Letter ID`,
  letter_type                                                     AS `Letter type`,
  CAST(issuedate AS DATE)                                         AS `Issue date`,
  bundle_ref                                                      AS `Bundle ID on letter`,
  bundle_name                                                     AS `Bundle name`,
  file_name                                                       AS `File name`
FROM letters
""",
    ),
    Check(
        id="RHAP_LETTER_SEND_ERROR",
        title="Letters with a sending or upload error",
        systems=("rhapsody",),
        entity="Letters & communications",
        dimension="Completeness",
        severity="High",
        question="Did every Rhapsody letter go out without an error message?",
        why="A letter with an error may never have reached the company or eSUB. The company's response deadline and "
            "CTP's review clock both depend on delivery.",
        how_to_fix="Open the letter in Rhapsody and read the error. Check in eSUB whether the company received it. "
                   "Fix the cause and resend the letter if it did not arrive. The cause is often a missing file or "
                   "contact. "
                   "After it goes out, ask the Rhapsody support team to clear the old error.",
        record_label="letters",
        filters=_ALL_FILTERS,
        date_label="Letter issue or upload date",
        technical_note=(
            "Source: `rhapsody_communications`, set up exactly as in RHAP_LETTER_NO_BUNDLE. Issue when "
            "TXT(`failuremsg`) or TXT(`esuberror`) is non-blank and not a no-error value (N, NO, FALSE, F, 0, NONE, "
            "NULL, N/A, NA, SUCCESS, SUCCESSFUL, OK, -). Letters with `ruleerrorbypass` or "
            "`ruleerrorbypassocebulkupload` set to true pass. Problem type = `stdz_failuremsg` (the existing standard grouping of "
            "failure messages, first 60 characters) when `failuremsg` holds the error, 'eSUB error' when only `esuberror` "
            "does. Submission type and organization come from the letter's one chosen bundle, as in "
            "RHAP_LETTER_NO_BUNDLE (the upload-to-bundle number first, then `bundleid`: K19-K21). Denominator: all live "
            "letters, one row per letter ID (P4 U06)."
        ),
        assumptions=(
            "A successful re-send clears the error or creates a new letter record. An error that still shows means "
            "the problem is still open.",
            "A letter whose rule error was bypassed on purpose is not a problem. The same goes for the bulk-upload "
            "bypass.",
        ),
        sql=r"""
WITH """ + _BUND_LOOKUP + r""",
""" + _LETTERS + r""",
f AS (
  SELECT
    letter_id, letter_type, issuedate, letter_date, file_name, failure_msg, failure_group, esub_error,
    error_bypassed, bundle_ref, bundle_name, stdz_type, office,
    coalesce(TXT(failure_msg) NOT IN """ + _NO_ERROR_VALUES + r""", false) AS has_failure,
    coalesce(TXT(esub_error) NOT IN """ + _NO_ERROR_VALUES + r""", false)  AS has_esub_error
  FROM letters
)
SELECT
  CAST(letter_id AS STRING)                                       AS record_key,
  letter_date                                                     AS record_date,
  CAST(stdz_type AS STRING)                                       AS submission_type,
  CAST(office AS STRING)                                          AS office,
  coalesce((has_failure OR has_esub_error) AND NOT error_bypassed, false) AS is_issue,
  CASE WHEN (has_failure OR has_esub_error) AND NOT error_bypassed THEN
    CASE WHEN has_failure THEN substr(coalesce(failure_group, 'Other sending error'), 1, 60) ELSE 'eSUB error' END
  END                                                             AS issue_category,
  CASE WHEN (has_failure OR has_esub_error) AND NOT error_bypassed THEN
    concat(coalesce(letter_type, 'Letter'), ' ', CAST(letter_id AS STRING), ' shows ',
           concat_ws(' and ',
             CASE WHEN has_failure THEN concat('the sending error "', substr(failure_msg, 1, 300), '"') END,
             CASE WHEN has_esub_error THEN concat('the eSUB error "', substr(esub_error, 1, 300), '"') END), '.')
  END                                                             AS issue_reason,
  CAST(letter_id AS STRING)                                       AS `Letter ID`,
  letter_type                                                     AS `Letter type`,
  CAST(issuedate AS DATE)                                         AS `Issue date`,
  CASE WHEN has_failure THEN failure_msg END                      AS `Error message`,
  CASE WHEN has_esub_error THEN esub_error END                    AS `eSUB error`,
  bundle_name                                                     AS `Bundle name`,
  file_name                                                       AS `File name`,
  office                                                          AS `Organization`
FROM f
""",
    ),
    Check(
        id="RHAP_LETTER_NO_FILE",
        title="Letters with no file attached",
        systems=("rhapsody",),
        entity="Letters & communications",
        dimension="Completeness",
        severity="High",
        question="Does every Rhapsody letter older than 7 days have its file attached, unless it is marked "
                 "'no upload'? Only letter types that almost always have a file are checked.",
        why="The attached file is CTP's official copy of the letter. Staff cannot re-send or produce a letter that "
            "has no file. The company may also never have received it.",
        how_to_fix="Find the letter in Rhapsody by its letter ID or bundle, and attach the signed letter. "
                   "If the record has no document, such as a phone call note, mark it 'no upload'. "
                   "If the record was created by mistake, ask the Rhapsody support team to remove it.",
        record_label="letters",
        filters=_ALL_FILTERS,
        date_label="Letter issue or upload date",
        technical_note=(
            "Source: `rhapsody_communications`, set up exactly as in RHAP_LETTER_NO_BUNDLE. A letter has a file when "
            "BLANK() of `finalfilename`, `doc_filename`, `s_filename` or `doc_guid` has a value. Scope by letter type, "
            "using the same type as the other letter checks: coalesce of BLANK(`stdz_communicationstypelocal`), "
            "BLANK(`communicationstypelocal`), BLANK(`communicationtype`); blank types form one group. For each type, "
            "count the live letters (all dates) not marked `noupload` and how many of them have a file. Only types with "
            "at least 50 such letters and a file on at least 90% of them are evaluated. Letters of every other type are "
            "not evaluated and not counted (left out of the denominator). Population: letters of in-scope types whose "
            "`record_date` is more than 7 days ago. Issue when the letter has no file and `noupload` is not true. "
            "Problem type splits letters with an `issuedate` from those without one. Submission type and organization "
            "come from the letter's one chosen bundle, as in RHAP_LETTER_NO_BUNDLE (the upload-to-bundle number first, "
            "then `bundleid`: K19-K21). Confirmed on real data (Oct 2026): the share with a file varies widely by type "
            "(P2.21). Acceptance Letter 6,642 of 6,773 and Filing Letter 2,521 of 2,526 have one, against Telecon "
            "38,093 of 61,677, Memo To File 5,378 of 10,020, Completeness Review 1,322 of 6,441, Public Health Impact "
            "Review 87 of 3,127 and Jurisdiction Review 10 of 3,517. `noupload` marks only 209 telecons."
        ),
        assumptions=(
            "A letter has a file when any file name field or its document ID has a value.",
            "A letter type is checked only when it has at least 50 letters and at least 90% of them have a file. "
            "Below that share, a missing file is normal for the type.",
            "Letters of any other type are not checked and do not count toward the pass rate.",
            "A letter marked 'no upload' has no document on purpose, so it passes. Letters with this mark do not "
            "count toward a type's 90% share.",
            "Letters from the last 7 days may still be in progress, so they are not checked yet.",
        ),
        sql=r"""
WITH """ + _BUND_LOOKUP + r""",
""" + _LETTERS + r""",
ty AS (
  SELECT
    letter_type,
    sum(CASE WHEN NOT no_upload THEN 1 ELSE 0 END)                  AS n_typed,
    sum(CASE WHEN NOT no_upload AND (file_name IS NOT NULL OR doc_guid IS NOT NULL) THEN 1 ELSE 0 END)
                                                                    AS n_with_file
  FROM letters
  GROUP BY letter_type
),
f AS (
  SELECT
    l.letter_id, l.letter_type, l.issuedate, l.letter_date, l.bundle_ref, l.bundle_name, l.stdz_type, l.office,
    round(100.0 * ty.n_with_file / nullif(ty.n_typed, 0), 1)        AS type_file_pct,
    l.file_name IS NULL AND l.doc_guid IS NULL AND NOT l.no_upload  AS no_file
  FROM letters l
  JOIN ty ON l.letter_type <=> ty.letter_type
  WHERE (l.letter_date IS NULL OR l.letter_date <= date_sub(current_date(), 7))
    AND ty.n_typed >= 50
    AND ty.n_with_file >= 0.9 * ty.n_typed
)
SELECT
  CAST(letter_id AS STRING)                                       AS record_key,
  letter_date                                                     AS record_date,
  CAST(stdz_type AS STRING)                                       AS submission_type,
  CAST(office AS STRING)                                          AS office,
  coalesce(no_file, false)                                        AS is_issue,
  CASE WHEN no_file THEN
    CASE WHEN issuedate IS NOT NULL THEN 'No file (issued)' ELSE 'No file (undated)' END
  END                                                             AS issue_category,
  CASE WHEN no_file THEN
    concat(coalesce(letter_type, 'Letter'), ' ', CAST(letter_id AS STRING),
           CASE WHEN issuedate IS NOT NULL THEN concat(' (issued ', CAST(CAST(issuedate AS DATE) AS STRING), ')')
                ELSE ' (no issue date)' END,
           ' has no file attached and is not marked "no upload".')
  END                                                             AS issue_reason,
  CAST(letter_id AS STRING)                                       AS `Letter ID`,
  letter_type                                                     AS `Letter type`,
  type_file_pct                                                   AS `Percent of this type with a file`,
  CAST(issuedate AS DATE)                                         AS `Issue date`,
  bundle_ref                                                      AS `Bundle ID on letter`,
  bundle_name                                                     AS `Bundle name`,
  stdz_type                                                       AS `Submission type`,
  office                                                          AS `Organization`
FROM f
""",
    ),
    Check(
        id="RHAP_BUNDLE_MISSING_KEY_FIELDS",
        title="Bundles missing dates, type, organization or manager",
        systems=("rhapsody",),
        entity="Bundles",
        dimension="Completeness",
        severity="High",
        question="Does every bundle have a received date, an organization, a recognized type, a project manager and a "
                 "goal date? The last two apply only to open bundles older than 30 days.",
        why="These details place a bundle in the right workload and review-time reports. An open bundle with "
            "no goal date never shows as late. One with no project manager has no owner.",
        how_to_fix="Open the bundle in Rhapsody and fill in the blank field. Use the bundle's submissions and their "
                   "eSUB records as the source. If no project manager or goal date is set yet, ask the review "
                   "team lead to set it.",
        record_label="bundles",
        filters=_ALL_FILTERS,
        date_label="Bundle received date",
        technical_note=(
            "Source: live `rhapsody_bundle` (`halo_is_deleted`, `deleted`, `s_is_temporary_copy` excluded, latest row per "
            "`id`), excluding `isduplicate` = true. Flags: blank `datereceivedstart`; no recognized "
            "TXT(`stdz_submission_type`) (blank or a placeholder such as UNKNOWN); blank `organization` (the applicant "
            "company, shown as Organization). For open bundles (same closed rule as RHAP_BUNDLE_PAST_GOAL_DATE, "
            "including REMOVED FROM REVIEW and `s_instance_status` = Complete) whose `record_date` is more than 30 days "
            "ago: blank `rhpm` and blank `torhpmuser_id` (no project manager); blank `goaldate` unless the type matches "
            "MEETING|CORRESPOND*|MASTER FILE(S)|TPMF|MF|GC as whole words. `record_date` = first plausible "
            "(2009-06-22..today) of `datereceivedstart`, `s_createddate`, else the load date. Denominator: all live, "
            "non-duplicate bundles, one row per bundle ID (P4 U05). Confirmed on real data (Oct 2026): the finished "
            "statuses are refuse to accept, refuse to file, withdrawn, administratively withdrawn or closed, cancelled, "
            "rescinded, denied, granted and removed from review (P2.11), and `s_instance_status` = Complete means "
            "finished (P2.13). ACCEPTED, FILED and OCE REVIEW COMPLETED are open."
        ),
        assumptions=(
            "Every bundle should have a received date and an organization. It should also have a submission type, even "
            "after the review ends.",
            "Every bundle open for more than 30 days should have a goal date and a project manager. Meeting requests "
            "and master files do not need a goal date. Neither does general correspondence.",
            "A bundle has a project manager when either the project manager name or the linked user is set.",
            "Bundles marked as duplicates are not real reviews, so they are excluded.",
        ),
        sql=r"""
WITH """ + _BUNDLES + r""",
f AS (
  SELECT
    bundle_label, bundle_name, bid, status, raw_type, stdz_type, office, rhpm, rhpm_user_id, received_dt,
    bundle_rec_date, goal_dt, is_closed,
    datereceivedstart IS NULL                                               AS no_received,
    NOT coalesce(TXT(stdz_type) NOT IN """ + _TYPE_PLACEHOLDERS + r""", false) AS no_type,
    office IS NULL                                                          AS no_office,
    NOT is_closed AND (bundle_rec_date IS NULL OR bundle_rec_date <= date_sub(current_date(), 30)) AS due,
    coalesce(upper(coalesce(stdz_type, raw_type, '')) rlike '""" + _NON_REVIEW_TYPES_RE + r"""', false) AS no_goal_expected
  FROM b
  WHERE NOT is_duplicate
),
g AS (
  SELECT
    bundle_label, bundle_name, bid, status, raw_type, stdz_type, office, rhpm, received_dt, bundle_rec_date, goal_dt,
    no_received, no_type, no_office,
    due AND rhpm IS NULL AND rhpm_user_id IS NULL                           AS no_rhpm,
    due AND goal_dt IS NULL AND NOT no_goal_expected                        AS no_goal
  FROM f
),
h AS (
  SELECT
    bundle_label, bundle_name, bid, status, raw_type, stdz_type, office, rhpm, received_dt, bundle_rec_date, goal_dt,
    no_received, no_type, no_office, no_rhpm, no_goal,
    (CASE WHEN no_received THEN 1 ELSE 0 END) + (CASE WHEN no_type THEN 1 ELSE 0 END)
      + (CASE WHEN no_office THEN 1 ELSE 0 END) + (CASE WHEN no_rhpm THEN 1 ELSE 0 END)
      + (CASE WHEN no_goal THEN 1 ELSE 0 END)                               AS n_problems,
    nullif(concat_ws(', ',
      CASE WHEN no_received THEN 'received date' END,
      CASE WHEN no_type AND stdz_type IS NULL THEN 'submission type' END,
      CASE WHEN no_office THEN 'organization' END,
      CASE WHEN no_rhpm THEN 'project manager' END,
      CASE WHEN no_goal THEN 'goal date' END), '')                          AS blank_list
  FROM g
)
SELECT
  bundle_label                                                     AS record_key,
  bundle_rec_date                                                  AS record_date,
  CAST(stdz_type AS STRING)                                        AS submission_type,
  CAST(office AS STRING)                                           AS office,
  coalesce(n_problems > 0, false)                                  AS is_issue,
  CASE
    WHEN n_problems > 1 THEN 'Several problems'
    WHEN no_received THEN 'Blank received date'
    WHEN no_type THEN 'Type not recognized'
    WHEN no_office THEN 'Blank organization'
    WHEN no_rhpm THEN 'No project manager'
    WHEN no_goal THEN 'No goal date'
  END                                                              AS issue_category,
  CASE WHEN n_problems > 0 THEN concat_ws(' ',
    CASE WHEN blank_list IS NOT NULL THEN concat('Blank in Rhapsody: ', blank_list, '.') END,
    CASE WHEN no_type AND stdz_type IS NOT NULL THEN
      concat('Submission type "', stdz_type, '" is not recognized.') END,
    CASE WHEN no_type AND stdz_type IS NULL AND raw_type IS NOT NULL THEN
      concat('The submission type entered ("', raw_type, '") is not on the standard list.') END,
    CASE WHEN no_rhpm OR no_goal THEN
      concat('The bundle is still open (status "', coalesce(status, 'blank'), '") and was received ',
             coalesce(CAST(received_dt AS STRING), 'on an unknown date'), '.') END)
  END                                                              AS issue_reason,
  bundle_name                                                      AS `Bundle name`,
  bid                                                              AS `Bundle ID`,
  status                                                           AS `Bundle status`,
  received_dt                                                      AS `Bundle received date`,
  goal_dt                                                          AS `Goal date`,
  rhpm                                                             AS `Project manager`,
  coalesce(stdz_type, raw_type)                                    AS `Submission type`,
  office                                                           AS `Organization`
FROM h
""",
    ),
    # ------------------------------------------------------------------ #
    # Consistency
    # ------------------------------------------------------------------ #
    Check(
        id="RHAP_SUB_DUPLICATE_STN",
        title="Duplicate STNs in Rhapsody",
        systems=("rhapsody",),
        entity="Submissions",
        dimension="Consistency",
        severity="High",
        question="Does each STN appear only once in Rhapsody's submission list?",
        why="A submission listed twice is counted twice in official counts. The copies may also hold different "
            "details, so reports can disagree.",
        how_to_fix="Search for the STN in Rhapsody and compare the copies. Keep the correct one and ask the Rhapsody "
                   "support team to remove or merge the other. Where the copies disagree, use the eSUB record to pick "
                   "the right details.",
        record_label="STNs",
        filters=_ALL_FILTERS,
        date_label="Received date",
        warn_below=0.999,
        fail_below=0.99,
        technical_note=(
            "Source: `rhapsody_submissions`, live rows only (`halo_is_deleted` excluded), grouped by STN_KEY(stn). Blank "
            "STNs are left out because RHAP_SUB_MISSING_COUNT_FIELDS covers them. One row per STN; issue when more than "
            "one live row shares it. 'Copies disagree' compares `date_received`, TXT(`stdz_submission_type`), "
            "TXT(`organization`) (the applicant company, shown as Organization) and CO_NAME() of the first non-blank "
            "company field across copies (blank counts as a "
            "value). Copies that agree on those but differ in the contact person (`abpp_person_key`, else email, else "
            "name) get their own problem type. `record_date` = earliest first-plausible date across copies, else load "
            "date. Denominator: distinct non-blank STNs. Confirmed on real data (Oct 2026): all 46,821 live rows have a "
            "filled-in, distinct STN (P4 U04), so this check guards against new duplicates."
        ),
        assumptions=(
            "Every submission has its own STN, amendments included. One STN should never appear twice.",
            "Rhapsody keeps one contact person per submission. If it can keep several, copies that differ only by "
            "contact are not real duplicates. In that case, drop that problem type.",
        ),
        sql=r"""
WITH r AS (
  SELECT
    STN_KEY(stn)                                               AS stn_norm,
    BLANK(stn)                                                 AS stn,
    date_received,
    BLANK(stdz_submission_type)                                AS stdz_type,
    BLANK(organization)                                        AS office,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company),
             BLANK(mvw_submission_org_company))                AS company,
    nullif(concat_ws(' ', BLANK(abpp_first_name), BLANK(abpp_last_name)), '') AS contact,
    coalesce(NUM_KEY(abpp_person_key), TXT(abpp_email),
             TXT(concat_ws(' ', abpp_first_name, abpp_last_name)))  AS contact_id,
    """ + _first_plausible("date_received", "date_submitted", "date_created", load="halo_insert_ts") + r"""
                                                               AS rec_date
  FROM ${core}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
),
g AS (
  SELECT
    stn_norm,
    min(stn)                                                             AS stn,
    count(*)                                                             AS copies,
    count(DISTINCT stn)                                                  AS spellings,
    array_join(array_sort(collect_set(stn)), ' | ')                      AS spelling_list,
    count(DISTINCT coalesce(CAST(date_received AS STRING), '-'))         AS n_received,
    count(DISTINCT coalesce(TXT(stdz_type), '-'))                        AS n_types,
    count(DISTINCT coalesce(TXT(office), '-'))                           AS n_offices,
    count(DISTINCT coalesce(CO_NAME(company), '-'))                      AS n_companies,
    count(DISTINCT coalesce(contact_id, '-'))                            AS n_contacts,
    array_join(array_sort(collect_set(coalesce(CAST(date_received AS STRING), 'blank'))), ', ') AS received_list,
    array_join(array_sort(collect_set(coalesce(stdz_type, 'blank'))), ', ')  AS type_list,
    array_join(array_sort(collect_set(coalesce(office, 'blank'))), ', ')     AS office_list,
    array_join(array_sort(collect_set(coalesce(company, 'blank'))), ' | ')   AS company_list,
    array_join(slice(array_sort(collect_set(coalesce(contact, 'blank'))), 1, 5), ' | ') AS contact_list,
    min(rec_date)                                                        AS rec_date,
    max(stdz_type)                                                       AS stdz_type,
    max(office)                                                          AS office
  FROM r
  WHERE stn_norm IS NOT NULL
  GROUP BY stn_norm
),
f AS (
  SELECT
    stn_norm, stn, copies, spellings, spelling_list, received_list, type_list, office_list, company_list,
    contact_list, rec_date, stdz_type, office, n_received, n_types, n_offices, n_companies, n_contacts,
    n_received > 1 OR n_types > 1 OR n_offices > 1 OR n_companies > 1    AS disagree
  FROM g
)
SELECT
  stn                                                                    AS record_key,
  CAST(rec_date AS DATE)                                                 AS record_date,
  CAST(stdz_type AS STRING)                                              AS submission_type,
  CAST(office AS STRING)                                                 AS office,
  coalesce(copies > 1, false)                                            AS is_issue,
  CASE
    WHEN copies > 1 AND disagree THEN 'Copies disagree'
    WHEN copies > 1 AND n_contacts > 1 THEN 'Only contact differs'
    WHEN copies > 1 AND spellings > 1 THEN 'STN spelled differently'
    WHEN copies > 1 THEN 'Identical copies'
  END                                                                    AS issue_category,
  CASE WHEN copies > 1 THEN
    concat('STN ', stn, ' is listed ', CAST(copies AS STRING), ' times in Rhapsody',
      CASE WHEN disagree THEN concat(' with different ', concat_ws('; ',
        CASE WHEN n_received > 1 THEN concat('received dates (', received_list, ')') END,
        CASE WHEN n_types > 1 THEN concat('submission types (', type_list, ')') END,
        CASE WHEN n_offices > 1 THEN concat('organizations (', office_list, ')') END,
        CASE WHEN n_companies > 1 THEN concat('companies (', company_list, ')') END))
      WHEN n_contacts > 1 THEN concat(', and the copies differ only by contact person (', contact_list, ')')
      WHEN spellings > 1 THEN concat(', written as ', spelling_list)
      ELSE '' END, '.')
  END                                                                    AS issue_reason,
  stn                                                                    AS `STN`,
  copies                                                                 AS `Times listed`,
  received_list                                                          AS `Received dates`,
  type_list                                                              AS `Submission types`,
  office_list                                                            AS `Organizations`,
  company_list                                                           AS `Companies`,
  contact_list                                                           AS `Contacts`,
  spelling_list                                                          AS `STN as written`
FROM f
""",
    ),
    Check(
        id="RHAP_SUB_COMPANY_NAMES_DIFFER",
        title="Conflicting company names on one submission",
        systems=("rhapsody",),
        entity="Companies & contacts",
        dimension="Consistency",
        severity="Medium",
        question="Do all the company names on a Rhapsody submission name the same company?",
        why="Company reports and searches read different company fields. When the names disagree, reports credit the "
            "submission to different companies. Company totals then fail to match.",
        how_to_fix="Open the submission in Rhapsody and compare the company names with the eSUB / iTRAC registration. "
                   "Correct the wrong one. If the company changed its name or was bought, follow the name-change "
                   "process so every field changes together.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Source: `rhapsody_submissions` (live rows, latest copy per STN_KEY(stn), non-blank STN). Compares the "
            "company fields (`abpc_company_name`, `itrac_company`, `abc_company`) after the shared CO_NAME() normalization "
            "(upper-case, punctuation removed, THE / INC / INCORPORATED / LLC / LTD / LIMITED / CO / CORP / CORPORATION / "
            "COMPANY / LP / LLP / PLC removed, spaces collapsed). Population: submissions where at least two of the "
            "names have a value. Issue when any two filled-in names differ. `mvw_submission_org_company` is not compared. "
            "CO_NAME reads '&' as AND and drops periods, so 'R.J.' equals 'RJ'."
        ),
        assumptions=(
            "Each submission stores the company name on its contact record. It also keeps an iTRAC copy and an "
            "address book entry. All of these should name the same applicant.",
            "Names that differ only in punctuation, spacing, capitals or a legal ending such as Inc or LLC are the "
            "same company.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
n AS (
  SELECT
    stn, stdz_type, office, rec_date, date_received, company_contact, company_itrac, company_abc,
    CO_NAME(company_contact) AS n_contact,
    CO_NAME(company_itrac)   AS n_itrac,
    CO_NAME(company_abc)     AS n_abc
  FROM s
  WHERE stn IS NOT NULL
),
c AS (
  SELECT
    stn, stdz_type, office, rec_date, date_received, company_contact, company_itrac, company_abc,
    coalesce(n_contact <> n_itrac, false)                               AS d_ci,
    coalesce(n_contact <> n_abc, false)                                 AS d_ca,
    coalesce(n_itrac <> n_abc, false)                                   AS d_ia,
    (CASE WHEN n_contact IS NOT NULL THEN 1 ELSE 0 END) + (CASE WHEN n_itrac IS NOT NULL THEN 1 ELSE 0 END)
      + (CASE WHEN n_abc IS NOT NULL THEN 1 ELSE 0 END)                 AS n_present
  FROM n
)
SELECT
  stn                                                                   AS record_key,
  CAST(rec_date AS DATE)                                                AS record_date,
  CAST(stdz_type AS STRING)                                             AS submission_type,
  CAST(office AS STRING)                                                AS office,
  coalesce(d_ci OR d_ca OR d_ia, false)                                 AS is_issue,
  CASE
    WHEN d_ci AND d_ca AND d_ia THEN 'All names differ'
    WHEN (d_ci OR d_ca OR d_ia) AND n_present = 2 THEN 'Two names differ'
    WHEN d_ci OR d_ca OR d_ia THEN 'One name differs'
  END                                                                   AS issue_category,
  CASE WHEN d_ci OR d_ca OR d_ia THEN
    concat('Company names do not match: ', concat_ws(', ',
      CASE WHEN company_contact IS NOT NULL THEN concat('"', company_contact, '" on the contact record') END,
      CASE WHEN company_itrac IS NOT NULL THEN concat('"', company_itrac, '" from iTRAC') END,
      CASE WHEN company_abc IS NOT NULL THEN concat('"', company_abc, '" in the address book') END), '.')
  END                                                                   AS issue_reason,
  stn                                                                   AS `STN`,
  company_contact                                                       AS `Contact record company`,
  company_itrac                                                         AS `iTRAC company`,
  company_abc                                                           AS `Address book company`,
  date_received                                                         AS `Received date`,
  stdz_type                                                             AS `Submission type`,
  office                                                                AS `Organization`
FROM c
WHERE n_present >= 2
""",
    ),
    Check(
        id="RHAP_BUNDLE_PRODUCT_COUNT_MISMATCH",
        title="Bundle product count differs from linked products",
        systems=("rhapsody",),
        entity="Bundles",
        dimension="Consistency",
        severity="Medium",
        question="Does each bundle's product count match the number of products linked to it?",
        why="Bundle product counts feed workload and large-bundle reports. If the count and the linked products "
            "disagree, one of them is wrong. Product totals in reports then cannot be trusted.",
        how_to_fix="Open the bundle in Rhapsody and compare its linked products with the count. Link missing products "
                   "or move extra ones to the right bundle. If the links are right, update the count to match.",
        record_label="bundles",
        filters=_ALL_FILTERS,
        date_label="Bundle received date",
        technical_note=(
            "Bundles: live `rhapsody_bundle` (`halo_is_deleted`, `deleted`, `s_is_temporary_copy` excluded, latest row "
            "per `id`, `isduplicate` bundles excluded), matched on NUM_KEY(`id`). Linked products: ANY_KEY(product id) "
            "from `rhapsody_submission_products` (ANY_KEY(`bundle_id`), STRING) UNION ALL `rhapsody_bulk_products` "
            "(NUM_KEY(`bundle_id`), BIGINT). The union is first reduced to one row per (bundle, product) with the number "
            "of distinct STNs that list it (a blank STN counts as one value) and its row count, then summed per bundle: "
            "product count = rows, pair count = sum of STNs (the placeholder product ID is counted differently, see "
            "below). STNs per bundle (shown only) come from a separate distinct (bundle, STN) "
            "list in which each source is reduced before the union. Every step is aggregated before the match to "
            "bundles, so no match multiplies rows, and a product on both lists counts once. Passes when "
            "`totalproductsinbundle` equals either the distinct product count or the distinct (STN, product) pair count. "
            "Population: bundles with a stated count or at least one linked product. `record_date` = first plausible of "
            "`datereceivedstart`, `s_createddate`, else load date. Confirmed on real data (Oct 2026): 99.5% of bundle "
            "IDs on regular product links are live bundles (K13, K14). The bulk list holds 24.4M rows on only 23 "
            "bundles, all live (K16), and 10 STNs (K47), and shares no product IDs with the regular list except one "
            "(K15), so those 23 bundles average about a million products each. One placeholder `product_id` is repeated on "
            "14,359 product rows across the two lists with different product names (P4 U07; every other product ID is "
            "unique), so COUNT(DISTINCT product_id) would count all of its products in a bundle as one. A product ID on "
            "more than PLACEHOLDER_ID_ROWS (100) rows of `rhapsody_products` + `rhapsody_bulk_products` is treated as a "
            "placeholder (found from the data in CTEs `prod_ids` / `ph`, never hard-coded). Its link rows are counted "
            "one by one: each row adds one product and one (STN, product) pair. `ph` has one row per product ID and "
            "`bp` one row per (bundle, product), so the match to `ph` cannot multiply rows. Limitation: a placeholder "
            "product listed under two STNs of the same bundle cannot be told apart, so it counts twice in the "
            "product count."
        ),
        assumptions=(
            "The product count should equal the number of linked products. Counting each product once or once per "
            "submission both pass.",
            "Bundles marked as duplicates are not real reviews, so they are excluded.",
        ),
        sql=r"""
WITH """ + _BUNDLES + r""",
""" + _PRODUCT_IDS + r""",
""" + _PLACEHOLDER_IDS + r""",
bp AS (
  SELECT bid, pid, count(DISTINCT coalesce(stn_norm, '-')) AS n_pairs, count(*) AS n_rows
  FROM (
""" + _BUNDLE_PRODUCT_LINKS + r"""
  ) u
  WHERE bid IS NOT NULL AND pid IS NOT NULL
  GROUP BY bid, pid
),
bs AS (
  SELECT bid, count(*) AS n_stns
  FROM (
""" + _BUNDLE_STNS + r"""
  ) u
  WHERE bid IS NOT NULL AND stn_norm IS NOT NULL
  GROUP BY bid
),
lk AS (
  SELECT p.bid, p.n_products, p.n_pairs, coalesce(bs.n_stns, 0) AS n_stns
  FROM (
    -- A product ID counts once, except a placeholder ID, whose link rows are separate products (and pairs).
    SELECT bp.bid,
           sum(CASE WHEN ph.pid IS NULL THEN 1 ELSE bp.n_rows END)          AS n_products,
           sum(CASE WHEN ph.pid IS NULL THEN bp.n_pairs ELSE bp.n_rows END) AS n_pairs
    FROM bp
    LEFT JOIN ph ON bp.pid = ph.pid
    GROUP BY bp.bid
  ) p
  LEFT JOIN bs ON p.bid = bs.bid
),
f AS (
  SELECT
    b.bundle_label, b.bundle_name, b.bid, b.status, b.stdz_type, b.office, b.bundle_rec_date, b.rhpm,
    b.totalproductsinbundle                          AS stated,
    coalesce(lk.n_products, 0)                       AS n_products,
    coalesce(lk.n_pairs, 0)                          AS n_pairs,
    coalesce(lk.n_stns, 0)                           AS n_stns
  FROM b
  LEFT JOIN lk ON b.bid = lk.bid
  WHERE NOT b.is_duplicate
    AND (b.totalproductsinbundle IS NOT NULL OR lk.bid IS NOT NULL)
)
SELECT
  bundle_label                                                      AS record_key,
  bundle_rec_date                                                   AS record_date,
  CAST(stdz_type AS STRING)                                         AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(stated IS NULL OR (stated <> n_products AND stated <> n_pairs), false) AS is_issue,
  CASE
    WHEN stated IS NULL THEN 'Blank count'
    WHEN stated = n_products OR stated = n_pairs THEN NULL
    WHEN n_products = 0 THEN 'No products linked'
    WHEN stated > n_products THEN 'Count too high'
    ELSE 'Count too low'
  END                                                               AS issue_category,
  CASE
    WHEN stated IS NULL THEN
      concat('The bundle has no product count, but ', CAST(n_products AS STRING), ' product(s) are linked.')
    WHEN stated = n_products OR stated = n_pairs THEN NULL
    ELSE concat('The bundle count says ', CAST(stated AS STRING), ' product(s), but ', CAST(n_products AS STRING),
                ' are linked (', CAST(abs(stated - n_products) AS STRING),
                CASE WHEN stated > n_products THEN ' fewer' ELSE ' more' END, ').')
  END                                                               AS issue_reason,
  bundle_name                                                       AS `Bundle name`,
  bid                                                               AS `Bundle ID`,
  stated                                                            AS `Product count`,
  n_products                                                        AS `Products linked`,
  n_stns                                                            AS `Submissions in bundle`,
  status                                                            AS `Bundle status`,
  rhpm                                                              AS `Project manager`,
  office                                                            AS `Organization`
FROM f
""",
    ),
    Check(
        id="RHAP_BUNDLE_NAME_NONSTANDARD",
        title="Blank or nonstandard bundle names",
        systems=("rhapsody",),
        entity="Bundles",
        dimension="Consistency",
        severity="Low",
        question="Does every bundle have a name that follows the standard naming pattern?",
        why="Stewards and reports find bundles by name. A blank or irregular name hides the bundle and can put it in "
            "the wrong report group.",
        how_to_fix="Open the bundle in Rhapsody and fix the name to match the standard pattern. Look for a mistyped "
                   "STN, missing parts, extra spaces or stray characters. If the problem is unclear, ask the data team.",
        record_label="bundles",
        filters=_ALL_FILTERS,
        date_label="Bundle received date",
        technical_note=(
            "Source: live `rhapsody_bundle` (`halo_is_deleted`, `deleted`, `s_is_temporary_copy` excluded, latest row per "
            "`id`). Issue when BLANK(`bundlename`) is blank, `stdz_bundlename_contains_error` is true (the existing "
            "bundle-name standardization rule), or the name has leading/trailing spaces or a run of two or more "
            "whitespace characters. `record_date` = first plausible of `datereceivedstart`, `s_createddate`, else load "
            "date. Denominator: all live bundles."
        ),
        assumptions=(
            "An existing bundle-name rule flags names that break the naming pattern. The pattern itself is not "
            "redefined here.",
        ),
        sql=r"""
WITH """ + _BUNDLES + r""",
f AS (
  SELECT
    bundle_label, bundle_name, bundle_name_raw, bid, status, stdz_type, office, rhpm, received_dt, bundle_rec_date,
    name_flagged,
    bundle_name IS NULL                                                         AS no_name,
    bundle_name IS NOT NULL
      AND coalesce(bundle_name_raw <> trim(bundle_name_raw) OR bundle_name rlike '\\s{2,}', false) AS extra_spaces
  FROM b
)
SELECT
  bundle_label                                                     AS record_key,
  bundle_rec_date                                                  AS record_date,
  CAST(stdz_type AS STRING)                                        AS submission_type,
  CAST(office AS STRING)                                           AS office,
  coalesce(no_name OR name_flagged OR extra_spaces, false)         AS is_issue,
  CASE
    WHEN no_name THEN 'Blank name'
    WHEN name_flagged THEN 'Breaks naming pattern'
    WHEN extra_spaces THEN 'Extra spaces'
  END                                                              AS issue_category,
  CASE
    WHEN no_name THEN concat('Bundle ID ', bid, ' has no name.')
    WHEN name_flagged THEN concat('Bundle name "', bundle_name_raw, '" fails the automatic naming pattern check.')
    WHEN extra_spaces THEN concat('Bundle name "', bundle_name_raw, '" has extra spaces.')
  END                                                              AS issue_reason,
  bundle_name_raw                                                  AS `Bundle name`,
  bid                                                              AS `Bundle ID`,
  status                                                           AS `Bundle status`,
  stdz_type                                                        AS `Submission type`,
  office                                                           AS `Organization`,
  rhpm                                                             AS `Project manager`,
  received_dt                                                      AS `Bundle received date`
FROM f
""",
    ),
    # ------------------------------------------------------------------ #
    # Timeliness
    # ------------------------------------------------------------------ #
    Check(
        id="RHAP_SUB_DATES_OUT_OF_ORDER",
        title="Submission dates out of order or out of range",
        systems=("rhapsody",),
        entity="Submissions",
        dimension="Timeliness",
        severity="High",
        question="Was each submission received no earlier than it was submitted? Is every date no later than today "
                 "and no earlier than June 22, 2009?",
        why="The received date starts the review clock. An out-of-order or impossible date is almost always a typo. "
            "It skews review-time metrics and date-based counts.",
        how_to_fix="Open the submission in Rhapsody and compare its dates with the eSUB receipt. Correct the wrong "
                   "date. Look for a swapped day and month or a wrong year.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Source: `rhapsody_submissions` (live rows, latest copy per STN_KEY(stn)). Received-before-submitted uses a "
            "1-day tolerance (`datediff(date_submitted, date_received) > 1`) because a company can submit late in the day "
            "in its own time zone. Future = any of `date_received`, `date_submitted`, `date_created` after `current_date()`. "
            "Too early = any of them before 2009-06-22 (the Tobacco Control Act, which created CTP). Population: "
            "submissions with at least one of the three dates. `record_date` uses the first date that is itself "
            "plausible (else the load date), so a mistyped date does not hide the record from the date filter."
        ),
        assumptions=(
            "No real CTP submission date is earlier than June 22, 2009.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
f AS (
  SELECT
    stn, stdz_type, office, company, date_received, date_submitted, date_created, rec_date,
    coalesce(datediff(date_submitted, date_received) > 1, false)        AS recv_before_sub,
    coalesce(date_received > current_date(), false) OR coalesce(date_submitted > current_date(), false)
      OR coalesce(date_created > current_date(), false)                 AS in_future,
    coalesce(date_received < """ + _MIN_DATE + r""", false) OR coalesce(date_submitted < """ + _MIN_DATE + r""", false)
      OR coalesce(date_created < """ + _MIN_DATE + r""", false)         AS too_early
  FROM s
  WHERE date_received IS NOT NULL OR date_submitted IS NOT NULL OR date_created IS NOT NULL
),
g AS (
  SELECT
    stn, stdz_type, office, company, date_received, date_submitted, date_created, rec_date,
    recv_before_sub, in_future, too_early,
    (CASE WHEN recv_before_sub THEN 1 ELSE 0 END) + (CASE WHEN in_future THEN 1 ELSE 0 END)
      + (CASE WHEN too_early THEN 1 ELSE 0 END)                         AS n_problems
  FROM f
)
SELECT
  coalesce(stn, '(no STN)')                                             AS record_key,
  CAST(rec_date AS DATE)                                                AS record_date,
  CAST(stdz_type AS STRING)                                             AS submission_type,
  CAST(office AS STRING)                                                AS office,
  coalesce(n_problems > 0, false)                                       AS is_issue,
  CASE
    WHEN n_problems > 1 THEN 'Several date problems'
    WHEN in_future THEN 'Future date'
    WHEN too_early THEN 'Before June 2009'
    WHEN recv_before_sub THEN 'Received before submitted'
  END                                                                   AS issue_category,
  CASE WHEN n_problems > 0 THEN concat_ws(' ',
    CASE WHEN recv_before_sub THEN
      concat('Received ', CAST(date_received AS STRING), ', ',
             CAST(datediff(date_submitted, date_received) AS STRING),
             ' days before it was submitted (', CAST(date_submitted AS STRING), ').') END,
    CASE WHEN in_future THEN concat('Future date: ', concat_ws(', ',
      CASE WHEN date_received > current_date() THEN concat('received ', CAST(date_received AS STRING)) END,
      CASE WHEN date_submitted > current_date() THEN concat('submitted ', CAST(date_submitted AS STRING)) END,
      CASE WHEN date_created > current_date() THEN concat('created ', CAST(date_created AS STRING)) END), '.') END,
    CASE WHEN too_early THEN concat('Date before June 22, 2009: ', concat_ws(', ',
      CASE WHEN date_received < """ + _MIN_DATE + r""" THEN concat('received ', CAST(date_received AS STRING)) END,
      CASE WHEN date_submitted < """ + _MIN_DATE + r""" THEN concat('submitted ', CAST(date_submitted AS STRING)) END,
      CASE WHEN date_created < """ + _MIN_DATE + r""" THEN concat('created ', CAST(date_created AS STRING)) END), '.') END)
  END                                                                   AS issue_reason,
  stn                                                                   AS `STN`,
  company                                                               AS `Company`,
  date_submitted                                                        AS `Submitted date`,
  date_received                                                         AS `Received date`,
  date_created                                                          AS `Created in Rhapsody`,
  stdz_type                                                             AS `Submission type`,
  office                                                                AS `Organization`
FROM g
""",
    ),
    Check(
        id="RHAP_BUNDLE_PAST_GOAL_DATE",
        title="Open bundles past their goal date",
        systems=("rhapsody",),
        entity="Bundles",
        dimension="Timeliness",
        severity="High",
        question="Is every open bundle in Rhapsody within its goal date?",
        why="Goal dates are CTP's review performance commitments. An open bundle past its goal date is a late review "
            "or a finished review that was never closed. Finished reviews left open make workload and on-time rates "
            "look worse than they are.",
        how_to_fix="Open the bundle in Rhapsody. If the review is finished, set the bundle to its final status. "
                   "Tell the project manager and team lead if the review is still open. Also check whether the goal "
                   "date should have been extended, for example while CTP waited on the company.",
        record_label="open bundles",
        filters=_ALL_FILTERS,
        date_label="Goal date",
        technical_note=(
            "Source: live `rhapsody_bundle` (`halo_is_deleted`, `deleted`, `s_is_temporary_copy` excluded, latest row per "
            "`id`). Duplicate bundles (`isduplicate` = true) are excluded. So are bundles with no `goaldate`, which "
            "RHAP_BUNDLE_MISSING_KEY_FIELDS flags. A bundle counts as closed when upper-cased `bundlestatus` or "
            "`s_lbb_current_state` contains a whole word or phrase matching "
            "`CLOS*|WITHDR*|CANCEL*|RESCIND*|REJECT*|REFUS*|RTA|RTF|DENI*|DECLIN*|TERMINAT*|ARCHIV*|INACTIVE|ABORT*|VOID*|"
            "ORDER(S)|GRANTED|EXEMPT|NSE|DONE|REMOVED FROM REVIEW`, or `s_instance_status` (the workflow engine's own "
            "state) matches `COMPLET*|CLOS*|TERMINAT*|ABORT*|CANCEL*|ARCHIV*|INACTIVE|DONE`. Confirmed on real data (Oct "
            "2026): `bundlestatus` takes 23 values (P2.11): REFUSE TO ACCEPT, ACCEPTED, WITHDRAWN, GRANTED, REMOVED FROM "
            "REVIEW (1,375 bundles), FILED, DENIED, CYCLE 1..5, RECEIVED, REFUSE TO FILE, OCE REVIEW COMPLETED, "
            "CANCELLED, PAUSED, RESCINDED, ADMINISTRATIVELY CLOSED, RFI LETTER ISSUED, OCE REVIEW PENDING, ENVIRONMENTAL "
            "and ADMINISTRATIVELY WITHDRAWN. Finished = refuse to accept, refuse to file, withdrawn, administratively "
            "withdrawn or closed, cancelled, rescinded, denied, granted and removed from review (P2.11); "
            "`s_instance_status` is Complete (78%, finished), In Progress (21%) or New (0.8%) (P2.13). ACCEPTED, FILED "
            "and OCE REVIEW COMPLETED are open. Population: open bundles with a goal date "
            "(future goal dates pass). Issue when `CAST(goaldate AS DATE)` < today. `record_date` = goal date; a goal "
            "date before 2009-06-22 (a typo) falls back to the bundle's received date so it is not hidden by the date "
            "filter."
        ),
        assumptions=(
            "The goal date already includes any pauses or extensions, such as time spent waiting on the company.",
            "Bundles marked as duplicates are not real reviews, so they are excluded.",
        ),
        sql=r"""
WITH """ + _BUNDLES + r""",
o AS (
  SELECT
    bundle_label, bundle_name, bid, status, stdz_type, office, rhpm, received_dt, bundle_rec_date, goal_dt,
    datediff(current_date(), goal_dt)                         AS days_past
  FROM b
  WHERE goal_dt IS NOT NULL
    AND NOT is_duplicate
    AND NOT is_closed
)
SELECT
  bundle_label                                                AS record_key,
  CASE WHEN goal_dt >= """ + _MIN_DATE + r""" THEN goal_dt ELSE bundle_rec_date END AS record_date,
  CAST(stdz_type AS STRING)                                   AS submission_type,
  CAST(office AS STRING)                                      AS office,
  coalesce(days_past > 0, false)                              AS is_issue,
  CASE
    WHEN days_past > 180 THEN '6+ months late'
    WHEN days_past > 30 THEN '1-6 months late'
    WHEN days_past > 0 THEN '1-30 days late'
  END                                                         AS issue_category,
  CASE WHEN days_past > 0 THEN
    concat('Bundle status is "', coalesce(status, 'blank'), '" and its goal date (', CAST(goal_dt AS STRING),
           ') passed ', CAST(days_past AS STRING), ' days ago.')
  END                                                         AS issue_reason,
  bundle_name                                                 AS `Bundle name`,
  bid                                                         AS `Bundle ID`,
  status                                                      AS `Bundle status`,
  goal_dt                                                     AS `Goal date`,
  CASE WHEN days_past > 0 THEN days_past END                  AS `Days past goal`,
  received_dt                                                 AS `Bundle received date`,
  rhpm                                                        AS `Project manager`,
  stdz_type                                                   AS `Submission type`,
  office                                                      AS `Organization`
FROM o
""",
    ),
    Check(
        id="RHAP_TASK_OVERDUE",
        title="Open review tasks past their due date",
        systems=("rhapsody",),
        entity="Review tasks",
        dimension="Timeliness",
        severity="Medium",
        question="Is every open review task in Rhapsody still within its due date?",
        why="An overdue task holds up its review. Tasks left open after the work is done also clutter inboxes and "
            "make workload reports look heavier than they are.",
        how_to_fix="Find the task in Rhapsody by its title and assignee. Complete or cancel it if the work is done. "
                   "If not, remind the assignee. Their team lead can reassign it.",
        record_label="tasks",
        filters=frozenset({FILTER_DATE}),
        date_label="Task due date",
        technical_note=(
            "Source: `rhapsody_notf_task_instance`, latest row per `task_instance_id`. Rows with `halo_is_deleted` or a "
            "non-zero `delete_flag` are excluded. Done = ANY_KEY(`state`) is 5 (Completed) or 8 (Obsolete), or `state` / "
            "`stdz_state_description` contains a whole word matching `COMPLET*|CANCEL*|REVOK*|SKIP*|DELET*|EXPIR*|"
            "TERMINAT*|ABORT*|CLOS*|OBSOLETE|WITHDR*|DONE|FINISH*|ARCHIV*|INACTIVE|RESOLVED`. Open = not done and "
            "`completion_date` is blank. Population: open tasks with a `due_date`. Issue when the due date is before "
            "today. Assignee = `delegated_to`, else `task_owner`, else `target`, with the `cn=` part pulled out of "
            "directory names. Task status shows `stdz_state_description`, else the confirmed name of the `state` code. "
            "Confirmed on real data (Oct 2026): `state` 5 = Completed, 8 = Obsolete (49,165 tasks, which never get a "
            "completion date), 2 = Assigned, 1 = Created; Completed and Obsolete are done (P2.19, P2.20). No review "
            "task title holds an STN (K48: 0 titles match a submission STN), and none of the 61,609 open tasks does "
            "(P4 U20). So a task cannot be tied to a submission: `submission_type` and `office` are output as NULL and "
            "the submission type and organization filters are off for this check. The task's own `organization` field "
            "holds the workflow-engine organization and is not used. `record_date` = due date (a due date before "
            "2009-06-22 falls back to the assigned date, else load date)."
        ),
        assumptions=(
            "A Created or Assigned task with no completion date still needs work, so a passed due date means it is late.",
        ),
        sql=r"""
WITH t AS (
  SELECT
    task_instance_id,
    BLANK(subject)                                                  AS subject,
    BLANK(source_name)                                              AS process_name,
    BLANK(state)                                                    AS state,
    BLANK(stdz_state_description)                                   AS state_desc,
    due_date,
    completion_date,
    assign_on,
    halo_insert_ts,
    coalesce(BLANK(delegated_to), BLANK(task_owner), BLANK(target)) AS assignee_raw
  FROM ${core}.rhapsody_notf_task_instance
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(delete_flag, 0) = 0
    AND task_instance_id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY task_instance_id ORDER BY halo_update_ts DESC NULLS LAST) = 1
),
o AS (
  SELECT
    task_instance_id, subject, process_name, assign_on,
    coalesce(state_desc, """ + _TASK_STATE_NAME.replace("{x}", "state") + r""", state) AS state_name,
    CAST(due_date AS DATE)                                          AS due_dt,
    datediff(current_date(), CAST(due_date AS DATE))                AS days_late,
    CASE WHEN CAST(due_date AS DATE) >= """ + _MIN_DATE + r""" THEN CAST(due_date AS DATE)
         ELSE """ + _first_plausible("assign_on", load="halo_insert_ts") + r""" END AS task_rec_date,
    """ + _person("assignee_raw") + r"""                            AS assignee
  FROM t
  WHERE completion_date IS NULL
    AND due_date IS NOT NULL
    AND NOT coalesce(ANY_KEY(state) IN """ + _TASK_DONE_CODES + r"""
                     OR upper(coalesce(state, '')) rlike '__TASK_CLOSED__'
                     OR upper(coalesce(state_desc, '')) rlike '__TASK_CLOSED__', false)
)
SELECT
  task_instance_id                                                  AS record_key,
  task_rec_date                                                     AS record_date,
  CAST(NULL AS STRING)                                              AS submission_type,
  CAST(NULL AS STRING)                                              AS office,
  coalesce(days_late > 0, false)                                    AS is_issue,
  CASE
    WHEN days_late > 180 THEN '6+ months overdue'
    WHEN days_late > 30 THEN '1-6 months overdue'
    WHEN days_late > 0 THEN '1-30 days overdue'
  END                                                               AS issue_category,
  CASE WHEN days_late > 0 THEN
    concat('Task "', coalesce(subject, '(no title)'), '" assigned to ', coalesce(assignee, 'nobody'),
           ' was due ', CAST(due_dt AS STRING), ' (', CAST(days_late AS STRING), ' days ago) and is still ',
           coalesce(state_name, 'open'), '.')
  END                                                               AS issue_reason,
  subject                                                           AS `Task`,
  assignee                                                          AS `Assignee`,
  state_name                                                        AS `Task status`,
  due_dt                                                            AS `Due date`,
  CASE WHEN days_late > 0 THEN days_late END                        AS `Days overdue`,
  CAST(assign_on AS DATE)                                           AS `Assigned date`,
  process_name                                                      AS `Process`
FROM o
""".replace("__TASK_CLOSED__", _TASK_CLOSED_RE),
    ),
    Check(
        id="RHAP_LETTER_DATES_OUT_OF_ORDER",
        title="Letter dates out of order or out of range",
        systems=("rhapsody",),
        entity="Letters & communications",
        dimension="Timeliness",
        severity="High",
        question="Was every letter issued after its submission arrived and no later than today? Is the company's "
                 "response due on or after the issue date?",
        why="Letter dates drive review-time metrics and the company's response deadline. A letter date out of order "
            "or out of range is an entry error. It gives the company a wrong deadline and skews on-time reports.",
        how_to_fix="Open the letter in Rhapsody. Compare its issue and response due dates with the signed letter and "
                   "the submission's receipt date. Correct the wrong date.",
        record_label="letters",
        filters=_ALL_FILTERS,
        date_label="Letter issue or upload date",
        technical_note=(
            "Letters: live `rhapsody_communications` with an `issuedate`, one row per letter, resolved to ONE bundle as "
            "in RHAP_LETTER_NO_BUNDLE (the upload-to-bundle number when it names a live bundle, else `bundleid`, else a "
            "deleted bundle either field names; K19-K21). Earliest received = the earlier of that bundle's plausible "
            "`datereceivedstart` and the earliest plausible `rhapsody_submissions.date_received` among the STNs linked "
            "to the bundle in `rhapsody_submission_products` or `rhapsody_bulk_products`. Each list is reduced to "
            "distinct (bundle, STN) pairs before the union, so the 24.4M bulk rows shrink to a few hundred pairs, and "
            "the pairs are aggregated per bundle before the match to letters. Flags: issued more than 1 day before the "
            "earliest received date (time-zone tolerance); issue date after today; issue date before 2009-06-22; "
            "`industryduedate` at least 1 day before `issuedate`. Submission type and organization come from the "
            "bundle. `record_date` = first plausible of issue / upload / last-modified date, else load date, so a "
            "mistyped issue date is not hidden by the date filter. Confirmed on real data (Oct 2026): every STN on the "
            "product links and the bulk list is a Rhapsody submission (K11, K47); 99.5% of bundle IDs on regular links "
            "and all 23 bulk bundle IDs are live bundles (K13, K14, K16)."
        ),
        assumptions=(
            "A letter for a bundle cannot be issued before the earliest submission in that bundle was received.",
            "The bundle's received date is the date its first submission arrived.",
            "A letter's response due date should be on or after its issue date.",
        ),
        sql=r"""
WITH s AS (
""" + _SUBS + r"""
),
""" + _BUND_LOOKUP + r""",
""" + _LETTERS + r""",
bs AS (
  SELECT
    l.bid,
    min(s.received_ok)                                              AS first_received,
    count(DISTINCT l.stn_norm)                                      AS n_stns,
    array_join(slice(array_sort(collect_set(coalesce(s.stn, l.stn_norm))), 1, 5), ', ') AS stns,
    max(s.company)                                                  AS company
  FROM (
    SELECT bid, stn_norm
    FROM (
""" + _BUNDLE_STNS + r"""
    ) u
    WHERE bid IS NOT NULL AND stn_norm IS NOT NULL
  ) l
  LEFT JOIN s ON l.stn_norm = s.stn_norm
  GROUP BY l.bid
),
f AS (
  SELECT
    lt.letter_id, lt.letter_type, lt.letter_date, lt.bundle_name, lt.stdz_type, lt.office,
    bs.stns, bs.n_stns, bs.company,
    CAST(lt.issuedate AS DATE)                                      AS issue_dt,
    CAST(lt.industryduedate AS DATE)                                AS due_dt,
    least(bs.first_received, lt.received_start)                     AS earliest_received
  FROM letters lt
  LEFT JOIN bs ON lt.matched_bid = bs.bid
  WHERE lt.issuedate IS NOT NULL
),
g AS (
  SELECT
    letter_id, letter_type, letter_date, bundle_name, stdz_type, office, stns, n_stns, company,
    issue_dt, due_dt, earliest_received,
    coalesce(datediff(earliest_received, issue_dt) > 1, false)      AS before_received,
    coalesce(issue_dt > current_date(), false)                      AS in_future,
    coalesce(issue_dt < """ + _MIN_DATE + r""", false)              AS too_early,
    coalesce(datediff(issue_dt, due_dt) >= 1, false)                AS due_before_issue
  FROM f
),
h AS (
  SELECT
    letter_id, letter_type, letter_date, bundle_name, stdz_type, office, stns, n_stns, company,
    issue_dt, due_dt, earliest_received, before_received, in_future, too_early, due_before_issue,
    (CASE WHEN before_received THEN 1 ELSE 0 END) + (CASE WHEN in_future THEN 1 ELSE 0 END)
      + (CASE WHEN too_early THEN 1 ELSE 0 END) + (CASE WHEN due_before_issue THEN 1 ELSE 0 END) AS n_problems
  FROM g
)
SELECT
  CAST(letter_id AS STRING)                                         AS record_key,
  letter_date                                                       AS record_date,
  CAST(stdz_type AS STRING)                                         AS submission_type,
  CAST(office AS STRING)                                            AS office,
  coalesce(n_problems > 0, false)                                   AS is_issue,
  CASE
    WHEN n_problems > 1 THEN 'Several date problems'
    WHEN before_received THEN 'Issued before received'
    WHEN in_future THEN 'Future issue date'
    WHEN too_early THEN 'Before June 2009'
    WHEN due_before_issue THEN 'Due before issued'
  END                                                               AS issue_category,
  CASE WHEN n_problems > 0 THEN concat_ws(' ',
    CASE WHEN before_received THEN
      concat('Issued ', CAST(issue_dt AS STRING), ', ', CAST(datediff(earliest_received, issue_dt) AS STRING),
             ' days before the submission was received (', CAST(earliest_received AS STRING), ').') END,
    CASE WHEN in_future THEN concat('Issue date ', CAST(issue_dt AS STRING), ' is in the future.') END,
    CASE WHEN too_early THEN concat('Issue date ', CAST(issue_dt AS STRING), ' is before June 22, 2009.') END,
    CASE WHEN due_before_issue THEN
      concat('Company response was due ', CAST(due_dt AS STRING), ', ', CAST(datediff(issue_dt, due_dt) AS STRING),
             ' days before the letter was issued (', CAST(issue_dt AS STRING), ').') END)
  END                                                               AS issue_reason,
  CAST(letter_id AS STRING)                                         AS `Letter ID`,
  letter_type                                                       AS `Letter type`,
  bundle_name                                                       AS `Bundle name`,
  stns                                                              AS `STNs in bundle`,
  company                                                           AS `Company`,
  issue_dt                                                          AS `Issue date`,
  earliest_received                                                 AS `Earliest received date`,
  due_dt                                                            AS `Response due`,
  office                                                            AS `Organization`
FROM h
""",
    ),
]
