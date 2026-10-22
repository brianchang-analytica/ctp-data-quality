"""Cross-system checks: eSUB (submission intake / iTRAC log-in list) <-> Rhapsody (review workflow).

The same submission should look identical in both systems. Every check here matches the two
systems the same way, using the shared building blocks defined below and the shared macros in
dq/sqlfrag.py (STN_KEY, NUM_KEY, ANY_KEY, BLANK, CO_NAME, TXT, FLAG_ON):

* eSUB side   - slvr_ctp_core.esub_itrac_reglist: one row per eSUB log-in record (REGLIST_KEY).
                Rows with halo_is_deleted = true are removed (the table has no DELETED_IND).
                FLAG_ON(INACTIVE_IND) marks a log-in as inactive; it is reported, not silently dropped.
* Rhapsody    - slvr_ctp_core.rhapsody_submissions: one row per STN (latest copy if an STN
                repeats). Rows with halo_is_deleted = true or a blank STN are removed. The table
                has no source `deleted` flag.
* Match       - STN_KEY(stn) on both sides. The eSUB log-in record number (REGLIST_KEY /
                reglist_key) is compared as NUM_KEY on both sides; it is used for its own checks.
                Confirmed on real data (Oct 2026), tools/sql/confirm_match_keys.sql Part 1:
                  K06/K08  Rhapsody STN vs eSUB log-in STN: 93.8% on active log-ins, 99.9% on active and
                           inactive ones. About 2,900 Rhapsody STNs exist only on inactive log-ins, so a
                           check must never call those "missing"; ESUB_RHAP_MISSING_IN_ESUB counts them as
                           found, and ESUB_RHAP_INACTIVE_STILL_OPEN flags the ones still open in Rhapsody.
                  K09/K10  Rhapsody reglist_key is a real eSUB REGLIST_KEY 100% of the time, and 99.9% point
                           at a log-in with the same STN.
                  K02      The log-in STN is the electronic submission tracking number (99.8%), so it is the
                           STN everywhere in eSUB. TRACKING_NUMBER is a different number (K01, 0%).
                  K39      eSUB STN_LINK is NOT an STN (0 of 120,700 values match any STN). No check here
                           reads STN_LINK, and none may use it as an STN or STN fallback.
                  K66      No STN has a company FEI in both systems, so no FEI comparison is possible. The
                           former ESUB_RHAP_FEI_DIFFERS check was retired.
* "pair"      - for each Rhapsody STN found in eSUB, ONE eSUB log-in: an active one first, then the
                one Rhapsody points to, then (check-specific) the one that agrees best with Rhapsody,
                then the most recently updated. No fan-out. Repeat log-ins of one STN are reported by
                ESUB_LOGIN_DUPLICATE_STN, so they are not re-reported here as mismatches.
* Dates       - record_date is the first plausible business date (2009-06-22 .. today) of the
                candidates, falling back to the lakehouse load date, so a mistyped date is never
                hidden by the date filter.

Grace periods (5 days for "missing from Rhapsody", matching the 5-day "late" limit; 3 days for
records just added to Rhapsody; 7 days after an eSUB change; 30 days for product lists) are plain
numbers in the SQL below and in the steward text; change both together.
"""

from dq.model import ALL_FILTERS, FILTER_DATE, Check

# A Rhapsody product_id on more than this many product rows (regular + bulk lists) is a placeholder shared by
# many different products. Confirmed on real data (Oct 2026, P4 U07): one value sits on 14,359 rows, and every
# other product ID is unique. Keep equal to PLACEHOLDER_ID_ROWS in dq/checks/rhapsody_pms.py (not imported, so
# this module loads on its own).
PLACEHOLDER_ID_ROWS = 100

# --------------------------------------------------------------------------- #
# Small SQL helpers (things the shared macros do not cover)
# --------------------------------------------------------------------------- #


def _ok(d: str) -> str:
    """The date when it is a plausible business date (Tobacco Control Act .. today), else NULL."""
    return f"CASE WHEN {d} BETWEEN DATE'2009-06-22' AND current_date() THEN {d} END"


def _first_ok(*dates: str, fallback: str | None = None) -> str:
    """First plausible date of the candidates, then the (unchecked) fallback, as a DATE."""
    parts = [_ok(d) for d in dates] + ([fallback] if fallback else [])
    return "CAST(coalesce(" + ", ".join(parts) + ") AS DATE)"


def _status_shown(x: str) -> str:
    """Rhapsody submission status for display: '1' is read as open, '0' as closed; anything else as stored."""
    return f"CASE {x} WHEN '1' THEN 'Open (1)' WHEN '0' THEN 'Closed (0)' ELSE {x} END"


def _sql(*ctes: str, select: str) -> str:
    """Assemble shared CTE blocks and a final SELECT into one query."""
    return "WITH " + ",".join(ctes).lstrip() + "\n" + select


# --------------------------------------------------------------------------- #
# Shared building blocks (CTEs). Raw strings so regex backslashes survive.
# --------------------------------------------------------------------------- #

_ESUB_LOGIN = r"""
esub_login AS (
  -- One row per eSUB log-in record; soft-deleted rows removed.
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS e_login_id,
    STN_KEY(STN)                                                             AS stn_norm,
    DATE_RECEIVED                                                            AS e_received_ts,
    CAST(DATE_RECEIVED AS DATE)                                              AS e_received,
    CAST(DATE_SUBMITTED AS DATE)                                             AS e_submitted,
    CAST(coalesce(DATE_ENTERED, CREATE_DATE) AS DATE)                        AS e_logged_in,
    CAST(halo_insert_ts AS DATE)                                             AS e_loaded,
    BLANK(NAME)                                                              AS e_company,
    try_cast(PRODUCT_COUNT AS BIGINT)                                        AS e_product_count,
    NUM_KEY(DOC_TYPES_KEY)                                                   AS e_doc_type_id,
    FLAG_ON(INACTIVE_IND)                                                    AS e_inactive,
    BLANK(COMMENTS_INACTIVE)                                                 AS e_inactive_comment,
    CAST(coalesce(UPDATE_DATE, CREATE_DATE) AS DATE)                         AS e_changed,
    coalesce(UPDATE_DATE, halo_update_ts, CREATE_DATE)                       AS e_updated_ts
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
)"""

_ESUB_DOC_TYPE = r"""
esub_doc_type AS (
  -- eSUB document type names (one row per document type).
  SELECT
    NUM_KEY(DOC_TYPES_KEY)                                                   AS e_doc_type_id,
    coalesce(BLANK(DOC_TYPE), BLANK(CODE))                                   AS e_doc_type_label
  FROM ${core}.esub_itrac_doc_types
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(DOC_TYPES_KEY) IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY NUM_KEY(DOC_TYPES_KEY) ORDER BY UPDATE_DATE DESC NULLS LAST, CREATE_DATE DESC NULLS LAST
  ) = 1
)"""

_RHAP_SUB = r"""
rhap_sub AS (
  -- One row per Rhapsody STN (latest copy if the STN repeats); soft-deleted and blank-STN rows removed.
  -- organization is the applicant company (Rhapsody's Organization field), not a CTP office. It feeds the
  -- contract column still named `office`.
  -- submission_status holds only '1' (read as open), '0' (read as closed or inactive) or a blank.
  SELECT
    STN_KEY(stn)                                                             AS stn_norm,
    NUM_KEY(reglist_key)                                                     AS r_login_id,
    date_received                                                            AS r_received,
    date_submitted                                                           AS r_submitted,
    date_created                                                             AS r_created,
    CAST(halo_insert_ts AS DATE)                                             AS r_loaded,
    BLANK(itrac_company)                                                     AS r_itrac_company,
    BLANK(abpc_company_name)                                                 AS r_company_name,
    BLANK(abpc_company_alias)                                                AS r_company_alias,
    BLANK(abc_company)                                                       AS r_abc_company,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company)) AS r_company,
    BLANK(stdz_submission_type)                                              AS r_type,
    BLANK(organization)                                                      AS r_org,
    BLANK(submission_status)                                                 AS r_status,
    coalesce(no_products, false)                                             AS r_no_products,
    upper(coalesce(BLANK(submission_rejected_desc), 'N/A'))
      NOT IN ('N/A', 'NA', 'NONE', 'NULL', '-', '0', 'N', 'NO', 'FALSE', 'F') AS r_rejected
  FROM ${core}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY STN_KEY(stn)
    ORDER BY halo_update_ts DESC NULLS LAST, date_created DESC NULLS LAST, reglist_key DESC NULLS LAST
  ) = 1
)"""

_ESUB_STN = r"""
esub_stn AS (
  -- eSUB log-in records summarized per STN.
  SELECT
    stn_norm,
    count(*)                                                                 AS e_logins,
    count_if(NOT e_inactive)                                                 AS e_active_logins,
    array_join(slice(array_sort(collect_set(e_login_id)), 1, 10), ', ')      AS e_login_list,
    array_join(slice(array_sort(collect_set(CASE WHEN NOT e_inactive THEN e_login_id END)), 1, 10), ', ')
                                                                             AS e_active_login_list,
    max(e_changed)                                                           AS e_last_changed
  FROM esub_login
  WHERE stn_norm IS NOT NULL
  GROUP BY stn_norm
)"""

_ESUB_BY_ID = r"""
esub_by_id AS (
  -- eSUB log-in records by record number (one row each).
  SELECT e_login_id, stn_norm AS e_stn_norm, e_received, e_company, e_inactive
  FROM esub_login
  WHERE e_login_id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY e_login_id ORDER BY e_updated_ts DESC NULLS LAST) = 1
)"""


def _pair(best: str = "") -> str:
    """Rhapsody submissions found in eSUB (same STN), each with ONE eSUB log-in.

    `best` is an optional check-specific ORDER BY term (with a trailing comma) that, among equally
    preferred log-ins, picks the one agreeing best with Rhapsody.
    """
    return r"""
pair AS (
  -- Submissions in both systems (same STN): the Rhapsody record plus ONE eSUB log-in, preferring an
  -- active log-in, then the one Rhapsody points to, then the closest match, then the latest updated.
  SELECT
    r.stn_norm, r.r_login_id, r.r_received, r.r_submitted, r.r_created, r.r_loaded,
    r.r_itrac_company, r.r_company_name, r.r_company_alias, r.r_abc_company, r.r_company,
    r.r_type, r.r_org, r.r_status, r.r_no_products, r.r_rejected,
    e.e_login_id, e.e_received_ts, e.e_received, e.e_submitted, e.e_logged_in, e.e_loaded,
    e.e_company, e.e_product_count, e.e_doc_type_id, e.e_inactive,
    e.e_inactive_comment, e.e_changed,
    datediff(current_date(), """ + _first_ok("r.r_created", "r.r_received", fallback="r.r_loaded") + r""") AS r_age_days
  FROM rhap_sub r
  JOIN esub_login e ON e.stn_norm = r.stn_norm
  QUALIFY row_number() OVER (
    PARTITION BY r.stn_norm
    ORDER BY CASE WHEN e.e_inactive THEN 1 ELSE 0 END,
             CASE WHEN e.e_login_id = r.r_login_id THEN 0 ELSE 1 END,
             """ + best + r"""
             e.e_updated_ts DESC NULLS LAST,
             try_cast(e.e_login_id AS BIGINT) DESC NULLS LAST
  ) = 1
)"""


_RHAP_START = r"""
rhap_start AS (
  -- When Rhapsody went into real use: the first month it created at least 20 submissions
  -- (a few test records do not count; placeholder dates outside 2009-06-22 .. today are ignored).
  -- Earlier eSUB receipts are not checked.
  SELECT min(created_month) AS start_date
  FROM (
    SELECT CAST(date_trunc('MONTH', r_created) AS DATE) AS created_month, count(*) AS n
    FROM rhap_sub
    WHERE r_created BETWEEN DATE'2009-06-22' AND current_date()
    GROUP BY CAST(date_trunc('MONTH', r_created) AS DATE)
  ) m
  WHERE n >= 20
)"""

# record_date for checks over "pair": received date (eSUB, else Rhapsody), else submitted, else load date.
_PAIR_RECORD_DATE = _first_ok("e_received", "r_received", "e_submitted", "r_submitted",
                              fallback="coalesce(r_loaded, e_loaded)")
# Pair checks skip Rhapsody records added in the last 3 days, while their details are still being entered.
_SETTLED = "coalesce(r_age_days > 3, true)"

# --------------------------------------------------------------------------- #
# 1. Logged in eSUB but missing from Rhapsody
# --------------------------------------------------------------------------- #

_MISSING_IN_RHAPSODY_SQL = _sql(
    _ESUB_LOGIN,
    _ESUB_DOC_TYPE,
    _RHAP_SUB,
    _RHAP_START,
    r"""
esub_latest AS (
  -- The most recent ACTIVE eSUB log-in for each STN, with its log-in day (for the grace period) and
  -- its era day (received, for "was Rhapsody in use yet").
  SELECT
    stn_norm, e_login_id, e_received, e_logged_in, e_company, e_doc_type_id,
    """ + _first_ok("e_logged_in", "e_received", fallback="e_loaded") + r"""    AS e_login_day,
    """ + _first_ok("e_received", "e_logged_in", fallback="e_loaded") + r"""    AS e_era_day
  FROM esub_login
  WHERE stn_norm IS NOT NULL AND NOT e_inactive
  QUALIFY row_number() OVER (
    PARTITION BY stn_norm
    ORDER BY """ + _first_ok("e_logged_in", "e_received", fallback="e_loaded") + r""" DESC NULLS LAST,
             e_updated_ts DESC NULLS LAST, try_cast(e_login_id AS BIGINT) DESC NULLS LAST
  ) = 1
)""",
    r"""
rhap_by_login AS (
  SELECT r_login_id, min(stn_norm) AS r_stn_for_login
  FROM rhap_sub
  WHERE r_login_id IS NOT NULL
  GROUP BY r_login_id
)""",
    r"""
login_elsewhere AS (
  -- A Rhapsody submission points at one of this STN's eSUB log-ins but carries another STN
  -- (reglist_key is a real REGLIST_KEY 100% of the time, K09).
  SELECT e.stn_norm, min(l.r_stn_for_login) AS other_rhap_stn, min(e.e_login_id) AS other_login_id
  FROM esub_login e
  JOIN rhap_by_login l ON l.r_login_id = e.e_login_id
  WHERE e.stn_norm IS NOT NULL AND l.r_stn_for_login <> e.stn_norm
  GROUP BY e.stn_norm
)""",
    r"""
candidates AS (
  -- Active eSUB STNs logged in more than 5 days ago and received after Rhapsody went into use.
  SELECT
    e.stn_norm, e.e_login_id, e.e_received, e.e_logged_in, e.e_company, e.e_doc_type_id,
    e.e_login_day, e.e_era_day,
    coalesce(e.e_doc_type_id, '(none)')                                      AS doc_group,
    datediff(current_date(), e.e_login_day)                                  AS days_since,
    r.stn_norm IS NOT NULL                                                   AS in_rhapsody,
    r.r_type, r.r_org,
    le.other_rhap_stn, le.other_login_id
  FROM esub_latest e
  LEFT JOIN rhap_sub r ON r.stn_norm = e.stn_norm
  LEFT JOIN login_elsewhere le ON le.stn_norm = e.stn_norm
  WHERE datediff(current_date(), e.e_login_day) > 5
    AND e.e_era_day >= (SELECT start_date FROM rhap_start)
)""",
    r"""
type_start AS (
  -- The month each eSUB document type started reaching Rhapsody: the month of its 5th log-in found
  -- there (a few early test links are ignored). Earlier log-ins of that type are not expected there.
  SELECT doc_group, CAST(date_trunc('MONTH', e_era_day) AS DATE) AS type_start
  FROM (
    SELECT doc_group, e_era_day,
           row_number() OVER (PARTITION BY doc_group ORDER BY e_era_day, stn_norm) AS rn
    FROM candidates
    WHERE in_rhapsody
  ) f
  WHERE rn = 5
)""",
    r"""
evaluated AS (
  SELECT
    c.stn_norm, c.e_login_id, c.e_received, c.e_logged_in, c.e_company, c.e_doc_type_id, c.doc_group,
    c.e_era_day, c.days_since, c.in_rhapsody, c.r_type, c.r_org, c.other_rhap_stn, c.other_login_id
  FROM candidates c
  JOIN type_start ts ON ts.doc_group = c.doc_group
  WHERE c.e_era_day >= ts.type_start
)""",
    r"""
type_scope AS (
  -- Only eSUB document types that normally reach Rhapsody (>= 10 and >= half already found there).
  SELECT doc_group, count(*) AS n_all, count_if(in_rhapsody) AS n_found
  FROM evaluated
  GROUP BY doc_group
)""",
    select=r"""
SELECT
  v.stn_norm                                                                 AS record_key,
  """ + _first_ok("v.e_received", "v.e_logged_in", fallback="v.e_era_day") + r""" AS record_date,
  v.r_type                                                                   AS submission_type,
  v.r_org                                                                    AS office,
  coalesce(NOT v.in_rhapsody, false)                                         AS is_issue,
  CASE WHEN NOT v.in_rhapsody THEN
    CASE WHEN v.other_rhap_stn IS NOT NULL THEN 'Under another STN' ELSE 'Only in eSUB' END
  END                                                                        AS issue_category,
  CASE
    WHEN v.in_rhapsody THEN CAST(NULL AS STRING)
    WHEN v.other_rhap_stn IS NOT NULL THEN concat(
      'No Rhapsody submission has STN ', v.stn_norm, '. Rhapsody STN ', v.other_rhap_stn,
      ' is linked to its eSUB log-in record ', coalesce(v.other_login_id, '?'),
      ', so one STN is likely mistyped.')
    ELSE concat(
      'Logged in eSUB on ', coalesce(CAST(v.e_logged_in AS STRING), '(date blank)'), ', ',
      CAST(v.days_since AS STRING), ' days ago (received ', coalesce(CAST(v.e_received AS STRING), '(date blank)'),
      '). Rhapsody has no submission with STN ', v.stn_norm, '.')
  END                                                                        AS issue_reason,
  v.stn_norm                                                                 AS `STN`,
  v.e_company                                                                AS `Company`,
  coalesce(d.e_doc_type_label, CASE WHEN v.e_doc_type_id IS NOT NULL THEN concat('Type ', v.e_doc_type_id) END)
                                                                             AS `Document type (eSUB)`,
  v.e_received                                                               AS `Received (eSUB)`,
  v.e_logged_in                                                              AS `Log-in date (eSUB)`,
  v.days_since                                                               AS `Days since log-in`,
  v.e_login_id                                                               AS `Log-in record`,
  v.other_rhap_stn                                                           AS `Linked Rhapsody STN`
FROM evaluated v
JOIN type_scope t ON t.doc_group = v.doc_group
LEFT JOIN esub_doc_type d ON d.e_doc_type_id = v.e_doc_type_id
WHERE t.n_found >= 10 AND t.n_found * 2 >= t.n_all
""",
)

# --------------------------------------------------------------------------- #
# 2. In Rhapsody but no eSUB log-in
# --------------------------------------------------------------------------- #

_MISSING_IN_ESUB_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _ESUB_STN,
    _ESUB_BY_ID,
    r"""
esub_deleted AS (
  -- STNs whose eSUB log-in exists only as a deleted record (used to explain the gap, not to match).
  SELECT DISTINCT STN_KEY(STN) AS stn_norm
  FROM ${core}.esub_itrac_reglist
  WHERE halo_is_deleted = true
    AND STN_KEY(STN) IS NOT NULL
)""",
    r"""
evaluated AS (
  -- in_esub: the STN is on any non-deleted eSUB log-in, active or inactive. It is the pass test and the
  -- type scope. An STN only on inactive log-ins counts as found (K06 93.8% active vs K08 99.9% active +
  -- inactive); ESUB_RHAP_INACTIVE_STILL_OPEN flags the ones still open in Rhapsody.
  SELECT
    r.stn_norm, r.r_login_id, r.r_received, r.r_submitted, r.r_created, r.r_loaded, r.r_type, r.r_org,
    r.r_status, r.r_company,
    s.stn_norm IS NOT NULL                                                   AS in_esub,
    x.stn_norm IS NOT NULL                                                   AS deleted_in_esub,
    b.e_login_id IS NOT NULL                                                 AS login_found,
    b.e_stn_norm                                                             AS login_stn
  FROM rhap_sub r
  LEFT JOIN esub_stn s ON s.stn_norm = r.stn_norm
  LEFT JOIN esub_deleted x ON x.stn_norm = r.stn_norm
  LEFT JOIN esub_by_id b ON b.e_login_id = r.r_login_id
  WHERE coalesce(datediff(current_date(), """ + _first_ok("r.r_created", "r.r_received") + r""") > 3, true)
)""",
    r"""
type_scope AS (
  -- Only Rhapsody submission types that normally come through eSUB (>= 10 and >= half found there).
  SELECT coalesce(TXT(r_type), '(none)') AS type_group,
         count(*) AS n_all, count_if(in_esub) AS n_found
  FROM evaluated
  GROUP BY coalesce(TXT(r_type), '(none)')
)""",
    select=r"""
SELECT
  v.stn_norm                                                                 AS record_key,
  """ + _first_ok("v.r_received", "v.r_submitted", "v.r_created", fallback="v.r_loaded") + r""" AS record_date,
  v.r_type                                                                   AS submission_type,
  v.r_org                                                                    AS office,
  coalesce(NOT v.in_esub, false)                                             AS is_issue,
  CASE
    WHEN v.in_esub THEN CAST(NULL AS STRING)
    WHEN v.login_found THEN 'Under another STN'
    WHEN v.deleted_in_esub THEN 'Deleted from eSUB'
    ELSE 'Only in Rhapsody'
  END                                                                        AS issue_category,
  CASE
    WHEN v.in_esub THEN CAST(NULL AS STRING)
    WHEN v.login_found AND v.login_stn IS NULL THEN concat(
      'No eSUB log-in has STN ', v.stn_norm, '. Its linked eSUB log-in record ', v.r_login_id,
      ' has a blank STN, so the STN was likely never entered.')
    WHEN v.login_found THEN concat(
      'No eSUB log-in has STN ', v.stn_norm, '. Its linked eSUB log-in record ', v.r_login_id,
      ' has STN ', v.login_stn, ', so one STN is likely mistyped.')
    WHEN v.deleted_in_esub THEN concat(
      'The eSUB log-in for ', v.stn_norm, ' was deleted, but the submission is still in Rhapsody.')
    ELSE concat(
      'Rhapsody submission ', v.stn_norm, ' (received ', coalesce(CAST(v.r_received AS STRING), '(date blank)'),
      ') has no eSUB log-in record.')
  END                                                                        AS issue_reason,
  v.stn_norm                                                                 AS `STN`,
  v.r_company                                                                AS `Company`,
  v.r_type                                                                   AS `Submission type (Rhapsody)`,
  v.r_received                                                               AS `Received (Rhapsody)`,
  v.r_created                                                                AS `Created (Rhapsody)`,
  """ + _status_shown("v.r_status") + r"""                                     AS `Status (Rhapsody)`,
  v.r_login_id                                                               AS `Linked log-in record`,
  v.login_stn                                                                AS `STN on linked record`
FROM evaluated v
JOIN type_scope t ON t.type_group = coalesce(TXT(v.r_type), '(none)')
WHERE t.n_found >= 10 AND t.n_found * 2 >= t.n_all
""",
)

# --------------------------------------------------------------------------- #
# 3. Same eSUB log-in record, different STN
# --------------------------------------------------------------------------- #

_SAME_LOGIN_DIFFERENT_STN_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _ESUB_STN,
    _ESUB_BY_ID,
    r"""
rhap_login_share AS (
  SELECT r_login_id, count(*) AS r_subs_on_login
  FROM rhap_sub
  WHERE r_login_id IS NOT NULL
  GROUP BY r_login_id
)""",
    r"""
evaluated AS (
  SELECT
    r.stn_norm, r.r_login_id, r.r_received, r.r_submitted, r.r_created, r.r_loaded, r.r_type, r.r_org,
    r.r_company,
    b.e_stn_norm, b.e_received, b.e_company,
    s.stn_norm IS NOT NULL                                                   AS r_stn_in_esub,
    c.r_subs_on_login
  FROM rhap_sub r
  JOIN esub_by_id b ON b.e_login_id = r.r_login_id
  LEFT JOIN esub_stn s ON s.stn_norm = r.stn_norm
  LEFT JOIN rhap_login_share c ON c.r_login_id = r.r_login_id
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _first_ok("r_received", "e_received", "r_submitted", "r_created", fallback="r_loaded") + r""" AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  coalesce(NOT (stn_norm <=> e_stn_norm), false)                             AS is_issue,
  CASE
    WHEN stn_norm <=> e_stn_norm THEN CAST(NULL AS STRING)
    WHEN e_stn_norm IS NULL THEN 'Blank in eSUB'
    ELSE 'STN differs'
  END                                                                        AS issue_category,
  CASE
    WHEN stn_norm <=> e_stn_norm THEN CAST(NULL AS STRING)
    WHEN e_stn_norm IS NULL THEN concat(
      'Rhapsody submission ', stn_norm, ' is linked to eSUB log-in record ', r_login_id,
      ', which has a blank STN.')
    ELSE concat(
      'Rhapsody has STN ', stn_norm, ', but its eSUB log-in record ', r_login_id, ' has STN ', e_stn_norm, '.',
      CASE WHEN r_stn_in_esub THEN concat(' eSUB also has ', stn_norm, ' on another log-in record.')
           ELSE concat(' eSUB has no log-in for ', stn_norm, '.') END,
      CASE WHEN r_subs_on_login > 1
           THEN concat(' ', CAST(r_subs_on_login AS STRING), ' Rhapsody submissions share this log-in record.')
           ELSE '' END)
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN (Rhapsody)`,
  e_stn_norm                                                                 AS `STN (eSUB)`,
  r_login_id                                                                 AS `Log-in record`,
  r_company                                                                  AS `Company (Rhapsody)`,
  e_company                                                                  AS `Company (eSUB)`,
  r_received                                                                 AS `Received (Rhapsody)`,
  e_received                                                                 AS `Received (eSUB)`,
  CASE WHEN r_stn_in_esub THEN 'Yes' ELSE 'No' END                           AS `Rhapsody STN in eSUB`,
  r_subs_on_login                                                            AS `Submissions sharing log-in`
FROM evaluated
""",
)

# --------------------------------------------------------------------------- #
# 4. Rhapsody not linked to the (active) eSUB log-in record of the same STN
# --------------------------------------------------------------------------- #

_LOGIN_LINK_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _ESUB_STN,
    _ESUB_BY_ID,
    r"""
evaluated AS (
  SELECT
    r.stn_norm, r.r_login_id, r.r_received, r.r_submitted, r.r_created, r.r_loaded, r.r_type, r.r_org,
    r.r_company,
    s.e_logins, s.e_active_logins, s.e_login_list, s.e_active_login_list,
    b.e_login_id IS NOT NULL                                                 AS login_found,
    b.e_stn_norm                                                             AS login_stn,
    coalesce(b.e_inactive, false)                                            AS login_inactive
  FROM rhap_sub r
  JOIN esub_stn s ON s.stn_norm = r.stn_norm
  LEFT JOIN esub_by_id b ON b.e_login_id = r.r_login_id
  WHERE coalesce(datediff(current_date(), """ + _first_ok("r.r_created", "r.r_received") + r""") > 3, true)
)""",
    r"""
flagged AS (
  SELECT
    v.*,
    CASE
      WHEN r_login_id IS NULL THEN 'Blank in Rhapsody'
      WHEN NOT login_found THEN 'Unknown eSUB record'
      WHEN NOT (login_stn <=> stn_norm) THEN 'STN differs'
      WHEN login_inactive AND e_active_logins > 0 THEN 'Inactive log-in'
    END                                                                      AS category
  FROM evaluated v
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _first_ok("r_received", "r_submitted", "r_created", fallback="r_loaded") + r""" AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  category IS NOT NULL                                                       AS is_issue,
  category                                                                   AS issue_category,
  CASE category
    WHEN 'Blank in Rhapsody' THEN concat(
      'The eSUB link for ', stn_norm, ' is blank in Rhapsody. eSUB has ', CAST(e_logins AS STRING),
      ' log-in record(s) for this STN: ', coalesce(e_login_list, '?'), '.')
    WHEN 'Unknown eSUB record' THEN concat(
      'Rhapsody links ', stn_norm, ' to eSUB log-in record ', r_login_id,
      ', which is missing or deleted in eSUB. This STN is on record(s) ', coalesce(e_login_list, '?'), '.')
    WHEN 'STN differs' THEN concat(
      'Rhapsody links ', stn_norm, ' to eSUB log-in record ', r_login_id, ', which has STN ',
      coalesce(login_stn, '(blank)'), '. This STN is on record(s) ', coalesce(e_login_list, '?'), '.')
    WHEN 'Inactive log-in' THEN concat(
      'Rhapsody links ', stn_norm, ' to inactive eSUB log-in record ', r_login_id,
      '. Active record(s) for this STN: ', coalesce(e_active_login_list, '?'), '.')
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  r_company                                                                  AS `Company`,
  r_login_id                                                                 AS `Linked log-in record`,
  e_login_list                                                               AS `All log-in records`,
  e_active_login_list                                                        AS `Active log-in records`,
  login_stn                                                                  AS `STN on linked record`,
  r_received                                                                 AS `Received (Rhapsody)`,
  r_type                                                                     AS `Submission type (Rhapsody)`
FROM flagged
""",
)

# --------------------------------------------------------------------------- #
# 5-6. Dates differ
# --------------------------------------------------------------------------- #

_RECEIVED_DATE_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _pair("abs(datediff(r.r_received, e.e_received)) ASC NULLS LAST,"),
    r"""
evaluated AS (
  SELECT p.*, datediff(r_received, e_received) AS diff
  FROM pair p
  WHERE (e_received IS NOT NULL OR r_received IS NOT NULL)
    AND """ + _SETTLED + r"""
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _PAIR_RECORD_DATE + r"""                                             AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  -- Exact match: no day of tolerance (Confirmed on real data (Oct 2026): no time-zone shift, P3.1/P3.2).
  coalesce(e_received IS NULL OR r_received IS NULL OR diff <> 0, false)     AS is_issue,
  CASE
    WHEN r_received IS NULL THEN 'Blank in Rhapsody'
    WHEN e_received IS NULL THEN 'Blank in eSUB'
    WHEN diff > 0 THEN 'Later in Rhapsody'
    WHEN diff < 0 THEN 'Earlier in Rhapsody'
  END                                                                        AS issue_category,
  CASE
    WHEN r_received IS NULL THEN concat(
      'eSUB shows received ', CAST(e_received AS STRING), ', but the Rhapsody received date is blank.')
    WHEN e_received IS NULL THEN concat(
      'Rhapsody shows received ', CAST(r_received AS STRING), ', but the eSUB received date is blank.')
    WHEN diff <> 0 THEN concat(
      'Received ', CAST(e_received AS STRING), ' in eSUB but ', CAST(r_received AS STRING), ' in Rhapsody, ',
      CAST(abs(diff) AS STRING), CASE WHEN abs(diff) = 1 THEN ' day' ELSE ' days' END,
      CASE WHEN diff > 0 THEN ' later.' ELSE ' earlier.' END)
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  coalesce(r_company, e_company)                                             AS `Company`,
  e_received                                                                 AS `Received (eSUB)`,
  r_received                                                                 AS `Received (Rhapsody)`,
  abs(diff)                                                                  AS `Days apart`,
  CAST(e_received_ts AS STRING)                                              AS `Received time (eSUB)`,
  e_submitted                                                                AS `Submitted (eSUB)`,
  r_submitted                                                                AS `Submitted (Rhapsody)`,
  e_login_id                                                                 AS `Log-in record`
FROM evaluated
""",
)

_SUBMITTED_DATE_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _pair("abs(datediff(r.r_submitted, e.e_submitted)) ASC NULLS LAST,"),
    r"""
evaluated AS (
  SELECT p.*, datediff(r_submitted, e_submitted) AS diff
  FROM pair p
  WHERE (e_submitted IS NOT NULL OR r_submitted IS NOT NULL)
    AND """ + _SETTLED + r"""
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _PAIR_RECORD_DATE + r"""                                             AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  coalesce(e_submitted IS NULL OR r_submitted IS NULL OR abs(diff) > 1, false) AS is_issue,
  CASE
    WHEN r_submitted IS NULL THEN 'Blank in Rhapsody'
    WHEN e_submitted IS NULL THEN 'Blank in eSUB'
    WHEN diff > 1 THEN 'Later in Rhapsody'
    WHEN diff < -1 THEN 'Earlier in Rhapsody'
  END                                                                        AS issue_category,
  CASE
    WHEN r_submitted IS NULL THEN concat(
      'eSUB shows submitted ', CAST(e_submitted AS STRING), ', but the Rhapsody submitted date is blank.')
    WHEN e_submitted IS NULL THEN concat(
      'Rhapsody shows submitted ', CAST(r_submitted AS STRING), ', but the eSUB submitted date is blank.')
    WHEN abs(diff) > 1 THEN concat(
      'Submitted ', CAST(e_submitted AS STRING), ' in eSUB but ', CAST(r_submitted AS STRING), ' in Rhapsody, ',
      CAST(abs(diff) AS STRING), CASE WHEN diff > 0 THEN ' days later.' ELSE ' days earlier.' END)
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  coalesce(r_company, e_company)                                             AS `Company`,
  e_submitted                                                                AS `Submitted (eSUB)`,
  r_submitted                                                                AS `Submitted (Rhapsody)`,
  abs(diff)                                                                  AS `Days apart`,
  e_received                                                                 AS `Received (eSUB)`,
  r_received                                                                 AS `Received (Rhapsody)`,
  e_login_id                                                                 AS `Log-in record`
FROM evaluated
""",
)

# --------------------------------------------------------------------------- #
# 7. Company name differs
# --------------------------------------------------------------------------- #

# Pass when the eSUB name equals ANY Rhapsody company field after CO_NAME normalization.
_NAMES_MATCH = (
    "(coalesce(CO_NAME(e.e_company) = CO_NAME(r.r_itrac_company), false)"
    " OR coalesce(CO_NAME(e.e_company) = CO_NAME(r.r_company_name), false)"
    " OR coalesce(CO_NAME(e.e_company) = CO_NAME(r.r_company_alias), false)"
    " OR coalesce(CO_NAME(e.e_company) = CO_NAME(r.r_abc_company), false))"
)

_COMPANY_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _pair("CASE WHEN " + _NAMES_MATCH + " THEN 0 WHEN e.e_company IS NOT NULL THEN 1 ELSE 2 END,"),
    r"""
named AS (
  SELECT
    p.*,
    CO_NAME(e_company)                                                       AS e_norm,
    CO_NAME(r_itrac_company)                                                 AS r_norm_itrac,
    CO_NAME(r_company_name)                                                  AS r_norm_name,
    CO_NAME(r_company_alias)                                                 AS r_norm_alias,
    CO_NAME(r_abc_company)                                                   AS r_norm_abc
  FROM pair p
  WHERE """ + _SETTLED + r"""
)""",
    r"""
evaluated AS (
  SELECT
    n.*,
    coalesce(r_itrac_company, r_company_name, r_abc_company, r_company_alias) AS r_company_shown,
    coalesce(r_norm_itrac, r_norm_name, r_norm_alias, r_norm_abc) IS NOT NULL AS r_has_name,
    (coalesce(e_norm = r_norm_itrac, false) OR coalesce(e_norm = r_norm_name, false)
      OR coalesce(e_norm = r_norm_alias, false) OR coalesce(e_norm = r_norm_abc, false)) AS names_match
  FROM named n
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _PAIR_RECORD_DATE + r"""                                             AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  coalesce(NOT names_match, false)                                           AS is_issue,
  CASE
    WHEN names_match THEN CAST(NULL AS STRING)
    WHEN e_norm IS NULL THEN 'Blank in eSUB'
    WHEN NOT r_has_name THEN 'Blank in Rhapsody'
    ELSE 'Name differs'
  END                                                                        AS issue_category,
  CASE
    WHEN names_match THEN CAST(NULL AS STRING)
    WHEN e_norm IS NULL THEN concat(
      'Rhapsody shows company "', r_company_shown, '", but the eSUB company is ',
      CASE WHEN e_company IS NULL THEN 'blank.' ELSE concat('only "', e_company, '".') END)
    WHEN NOT r_has_name THEN concat(
      'eSUB shows company "', e_company, '", but all Rhapsody company fields are blank.')
    ELSE concat(
      'eSUB shows company "', e_company, '", but Rhapsody shows "', r_company_shown, '"',
      CASE WHEN r_company_name IS NOT NULL AND r_itrac_company IS NOT NULL
                AND NOT (r_norm_name <=> r_norm_itrac)
           THEN concat(' (registered company "', r_company_name, '")') ELSE '' END,
      '.')
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  e_company                                                                  AS `Company (eSUB)`,
  r_itrac_company                                                            AS `iTRAC company (Rhapsody)`,
  r_company_name                                                             AS `Registered company (Rhapsody)`,
  r_company_alias                                                            AS `Company alias (Rhapsody)`,
  r_abc_company                                                              AS `Other company (Rhapsody)`,
  coalesce(e_received, r_received)                                           AS `Received date`,
  e_login_id                                                                 AS `Log-in record`
FROM evaluated
WHERE e_norm IS NOT NULL OR r_has_name
""",
)

# --------------------------------------------------------------------------- #
# 8. Number of products differs
# --------------------------------------------------------------------------- #

_PRODUCT_COUNT_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _pair("CASE WHEN e.e_product_count > 0 THEN 0 ELSE 1 END,"),
    r"""
rhap_product_links AS (
  SELECT STN_KEY(stn) AS stn_norm, ANY_KEY(product_id) AS product_norm, 'linked' AS source
  FROM ${core}.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
  UNION ALL
  SELECT STN_KEY(stn), ANY_KEY(product_id), 'bulk'
  FROM ${core}.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
  UNION ALL
  SELECT STN_KEY(untitled_relationship_stn), ANY_KEY(untitled_relationship_product_id), 'wrapper'
  FROM ${core}.rhapsody_submission_product_wrapper
  WHERE coalesce(halo_is_deleted, false) = false AND coalesce(s_is_temporary_copy, false) = false
)""",
    r"""
rhap_placeholder_id AS (
  -- Placeholder product IDs: a value on more than """ + str(PLACEHOLDER_ID_ROWS) + r""" product rows of Rhapsody's regular + bulk
  -- product lists. Confirmed on real data (Oct 2026, P4 U07): one product_id sits on 14,359 rows of different
  -- products, and every other ID is unique. Found from the data on every run, never hard-coded. One row each.
  SELECT product_norm
  FROM (
    SELECT ANY_KEY(product_id) AS product_norm
    FROM ${core}.rhapsody_products
    WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT ANY_KEY(product_id)
    FROM ${core}.rhapsody_bulk_products
    WHERE coalesce(halo_is_deleted, false) = false
  ) p
  WHERE product_norm IS NOT NULL
  GROUP BY product_norm
  HAVING count(*) > """ + str(PLACEHOLDER_ID_ROWS) + r"""
)""",
    r"""
rhap_products AS (
  -- Products per STN across Rhapsody's three product lists. An ordinary product ID counts once, however many
  -- lists name it. A placeholder ID stands for many different products, so each of its rows counts as one
  -- product: per STN, the most placeholder rows found in any one list (the lists can repeat the same link, so
  -- their placeholder rows are not added together).
  SELECT
    stn_norm,
    count(DISTINCT CASE WHEN NOT is_placeholder THEN product_norm END)
      + greatest(count_if(is_placeholder AND source = 'linked'),
                 count_if(is_placeholder AND source = 'bulk'),
                 count_if(is_placeholder AND source = 'wrapper'))            AS r_products,
    count(DISTINCT CASE WHEN source = 'bulk' AND NOT is_placeholder THEN product_norm END)
      + count_if(is_placeholder AND source = 'bulk')                         AS r_bulk
  FROM (
    SELECT l.stn_norm, l.product_norm, l.source, x.product_norm IS NOT NULL AS is_placeholder
    FROM rhap_product_links l
    LEFT JOIN rhap_placeholder_id x ON x.product_norm = l.product_norm
    WHERE l.product_norm IS NOT NULL AND l.stn_norm IS NOT NULL
  ) k
  GROUP BY stn_norm
)""",
    r"""
evaluated AS (
  SELECT
    p.stn_norm, p.r_type, p.r_org, p.r_received, p.r_submitted, p.r_loaded, p.e_received, p.e_submitted,
    p.e_loaded, p.e_login_id, p.r_no_products,
    coalesce(p.r_company, p.e_company)                                       AS company,
    p.e_product_count                                                        AS e_products,
    coalesce(rp.r_products, 0)                                               AS r_products,
    coalesce(rp.r_bulk, 0)                                                   AS r_bulk
  FROM pair p
  LEFT JOIN rhap_products rp ON rp.stn_norm = p.stn_norm
  WHERE p.e_product_count > 0
    AND NOT p.r_rejected
    AND coalesce(datediff(current_date(), """ + _first_ok("p.e_received", "p.r_received") + r""") > 30, true)
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _PAIR_RECORD_DATE + r"""                                             AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  coalesce(e_products <> r_products, false)                                  AS is_issue,
  CASE
    WHEN e_products = r_products THEN CAST(NULL AS STRING)
    WHEN r_products = 0 THEN 'None in Rhapsody'
    WHEN r_products < e_products THEN 'Fewer in Rhapsody'
    ELSE 'More in Rhapsody'
  END                                                                        AS issue_category,
  CASE
    WHEN e_products = r_products THEN CAST(NULL AS STRING)
    ELSE concat(
      'eSUB logged ', CAST(e_products AS STRING), ' product(s), but Rhapsody has ', CAST(r_products AS STRING), '.',
      CASE WHEN r_no_products THEN ' Rhapsody also marks it as having no products.' ELSE '' END)
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  company                                                                    AS `Company`,
  r_type                                                                     AS `Submission type (Rhapsody)`,
  e_products                                                                 AS `Products (eSUB)`,
  r_products                                                                 AS `Products (Rhapsody)`,
  r_products - e_products                                                    AS `Rhapsody minus eSUB`,
  r_bulk                                                                     AS `Bulk-list products`,
  CASE WHEN r_no_products THEN 'Yes' ELSE 'No' END                           AS `Marked "no products"`,
  coalesce(e_received, r_received)                                           AS `Received date`
FROM evaluated
""",
)

# --------------------------------------------------------------------------- #
# 9. Inactive in eSUB but still open in Rhapsody
# --------------------------------------------------------------------------- #

# Rhapsody submission_status holds no text values (Confirmed on real data (Oct 2026), P2.14: 75% blank, '1' on
# 7,511 rows, '0' on 3,967). '0' is read as closed or inactive and '1' as open (assumed). A blank status is not
# judged. A Rhapsody rejection reason also counts as closed. Population: STNs found only on inactive eSUB
# log-ins (about 2,900, K06 vs K08).

_INACTIVE_STILL_OPEN_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _ESUB_STN,
    _pair(),
    r"""
evaluated AS (
  SELECT
    p.stn_norm, p.r_type, p.r_org, p.r_received, p.r_submitted, p.r_loaded, p.e_received, p.e_submitted,
    p.e_loaded, p.r_status, p.e_login_id, p.e_inactive_comment,
    s.e_last_changed,
    coalesce(p.r_company, p.e_company)                                       AS company,
    s.e_logins,
    coalesce(p.r_rejected OR p.r_status = '0', false)                        AS r_closed
  FROM pair p
  JOIN esub_stn s ON s.stn_norm = p.stn_norm
  WHERE s.e_active_logins = 0
    AND p.r_status IN ('0', '1')
    AND coalesce(datediff(current_date(), s.e_last_changed) > 7, true)
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  """ + _PAIR_RECORD_DATE + r"""                                             AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  coalesce(NOT r_closed, false)                                              AS is_issue,
  CASE WHEN NOT r_closed THEN 'Still open' END                               AS issue_category,
  CASE WHEN NOT r_closed THEN concat(
    CASE WHEN e_logins > 1 THEN concat('All ', CAST(e_logins AS STRING), ' eSUB log-in records for ')
         ELSE 'The eSUB log-in record for ' END,
    stn_norm, CASE WHEN e_logins > 1 THEN ' are' ELSE ' is' END, ' marked inactive',
    CASE WHEN e_inactive_comment IS NOT NULL THEN concat(' ("', substr(e_inactive_comment, 1, 150), '")') ELSE '' END,
    ', but its Rhapsody status is still open (', r_status, ').')
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  company                                                                    AS `Company`,
  """ + _status_shown("r_status") + r"""                                       AS `Status (Rhapsody)`,
  substr(e_inactive_comment, 1, 300)                                         AS `Inactive comment (eSUB)`,
  e_last_changed                                                             AS `Last changed (eSUB)`,
  coalesce(e_received, r_received)                                           AS `Received date`,
  r_type                                                                     AS `Submission type (Rhapsody)`,
  e_login_id                                                                 AS `Log-in record`
FROM evaluated
""",
)

# --------------------------------------------------------------------------- #
# 10. Slow to reach Rhapsody
# --------------------------------------------------------------------------- #

_SLOW_TO_RHAPSODY_SQL = _sql(
    _ESUB_LOGIN,
    _RHAP_SUB,
    _pair("abs(datediff(r.r_created, e.e_received)) ASC NULLS LAST,"),
    _RHAP_START,
    r"""
evaluated AS (
  SELECT
    p.stn_norm, p.r_type, p.r_org, p.e_received, p.e_logged_in, p.r_created, p.e_login_id,
    coalesce(p.r_company, p.e_company)                                       AS company,
    datediff(p.r_created, p.e_received)                                      AS days_to_rhap,
    datediff(p.e_logged_in, p.e_received)                                    AS days_to_login,
    datediff(p.r_created, p.e_logged_in)                                     AS days_login_to_rhap
  FROM pair p
  WHERE p.r_created <= current_date()
    AND p.e_received BETWEEN (SELECT start_date FROM rhap_start) AND current_date()
)""",
    select=r"""
SELECT
  stn_norm                                                                   AS record_key,
  CAST(e_received AS DATE)                                                   AS record_date,
  r_type                                                                     AS submission_type,
  r_org                                                                      AS office,
  coalesce(days_to_rhap > 5, false)                                          AS is_issue,
  CASE WHEN days_to_rhap > 5 THEN
    CASE WHEN days_to_login IS NOT NULL AND days_to_login > coalesce(days_login_to_rhap, 0)
         THEN 'Late eSUB log-in' ELSE 'Late Rhapsody setup' END
  END                                                                        AS issue_category,
  CASE WHEN days_to_rhap > 5 THEN concat(
    'Received ', CAST(e_received AS STRING), ', logged in eSUB ', coalesce(CAST(e_logged_in AS STRING), '(date blank)'),
    ', but created in Rhapsody only on ', CAST(r_created AS STRING), ', ', CAST(days_to_rhap AS STRING),
    ' days after receipt.')
  END                                                                        AS issue_reason,
  stn_norm                                                                   AS `STN`,
  company                                                                    AS `Company`,
  e_received                                                                 AS `Received (eSUB)`,
  e_logged_in                                                                AS `Log-in date (eSUB)`,
  r_created                                                                  AS `Created (Rhapsody)`,
  days_to_rhap                                                               AS `Days to Rhapsody`,
  days_to_login                                                              AS `Days to log-in`,
  r_type                                                                     AS `Submission type (Rhapsody)`
FROM evaluated
""",
)

# --------------------------------------------------------------------------- #
# 11. Submission type unusual for the eSUB document type
# --------------------------------------------------------------------------- #

_TYPE_UNUSUAL_SQL = _sql(
    _ESUB_LOGIN,
    _ESUB_DOC_TYPE,
    _RHAP_SUB,
    _pair(),
    r"""
typed AS (
  SELECT
    stn_norm, r_type, r_org, e_received, r_received, e_submitted, r_submitted, r_loaded, e_loaded,
    e_login_id, e_doc_type_id,
    coalesce(r_company, e_company)                                           AS company,
    TXT(r_type)                                                              AS r_type_norm
  FROM pair
  WHERE e_doc_type_id IS NOT NULL AND TXT(r_type) IS NOT NULL
)""",
    r"""
pairing AS (
  SELECT e_doc_type_id, r_type_norm, count(*) AS n, min(r_type) AS r_type_label
  FROM typed
  GROUP BY e_doc_type_id, r_type_norm
)""",
    r"""
usual AS (
  -- The Rhapsody type most often given to each eSUB document type, and how dominant it is.
  SELECT
    e_doc_type_id, r_type_norm AS usual_type_norm, r_type_label AS usual_type, n AS usual_n,
    sum(n) OVER (PARTITION BY e_doc_type_id)                                 AS total_n
  FROM pairing
  QUALIFY row_number() OVER (PARTITION BY e_doc_type_id ORDER BY n DESC, r_type_norm) = 1
)""",
    select=r"""
SELECT
  t.stn_norm                                                                 AS record_key,
  """ + _first_ok("t.e_received", "t.r_received", "t.e_submitted", "t.r_submitted",
                  fallback="coalesce(t.r_loaded, t.e_loaded)") + r"""         AS record_date,
  t.r_type                                                                   AS submission_type,
  t.r_org                                                                    AS office,
  coalesce(t.r_type_norm <> u.usual_type_norm, false)                        AS is_issue,
  CASE WHEN t.r_type_norm <> u.usual_type_norm THEN 'Unusual type' END     AS issue_category,
  CASE WHEN t.r_type_norm <> u.usual_type_norm THEN concat(
    'eSUB type "', coalesce(d.e_doc_type_label, concat('type ', t.e_doc_type_id)),
    '" is "', u.usual_type, '" in Rhapsody for ',
    CAST(CAST(round(u.usual_n * 100.0 / nullif(u.total_n, 0)) AS INT) AS STRING), '% of ',
    CAST(u.total_n AS STRING), ' submissions. This one is "', t.r_type, '".')
  END                                                                        AS issue_reason,
  t.stn_norm                                                                 AS `STN`,
  t.company                                                                  AS `Company`,
  coalesce(d.e_doc_type_label, concat('Type ', t.e_doc_type_id))             AS `Document type (eSUB)`,
  t.r_type                                                                   AS `Submission type (Rhapsody)`,
  u.usual_type                                                               AS `Usual Rhapsody type`,
  coalesce(t.e_received, t.r_received)                                       AS `Received date`,
  t.e_login_id                                                               AS `Log-in record`
FROM typed t
JOIN usual u ON u.e_doc_type_id = t.e_doc_type_id
LEFT JOIN esub_doc_type d ON d.e_doc_type_id = t.e_doc_type_id
WHERE u.total_n >= 20 AND u.usual_n * 10 >= u.total_n * 9
""",
)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

_MATCH_NOTE = (
    "Match: STN_KEY(stn) on both sides. STN_KEY upper-cases the STN and removes all whitespace, including "
    "non-breaking spaces. eSUB = slvr_ctp_core.esub_itrac_reglist. Rows with halo_is_deleted are removed. The "
    "table has no DELETED_IND, and FLAG_ON reads INACTIVE_IND. Rhapsody = slvr_ctp_core.rhapsody_submissions, "
    "one row per STN (latest halo_update_ts). Rows with halo_is_deleted or a blank STN are removed, and the table "
    "has no source deleted flag. Log-in record numbers are compared as NUM_KEY on both sides. "
    "Confirmed on real data (Oct 2026): STN is the shared ID. 93.8% of Rhapsody STNs are on an active eSUB "
    "log-in (K06) and 99.9% on an active or inactive one (K08), so about 2,900 Rhapsody STNs exist only on "
    "inactive log-ins. Ignoring punctuation adds no matches (K07). "
    "Confirmed on real data (Oct 2026): the eSUB log-in STN is the electronic submission tracking number "
    "(K02, 99.8%), so it is the STN across eSUB. The log-in TRACKING_NUMBER is a different number (K01, 0%), and "
    "STN_LINK is not an STN (K39, 0 of 120,700 values match any STN). Neither is read by these checks. "
    "Confirmed on real data (Oct 2026): STNs are clean in every system (P2.33: no spaces, lowercase letters, "
    "dashes or dots), so STN_KEY only guards new entries. The submission type and Organization filters use the "
    "trimmed Rhapsody stdz_submission_type and organization. Confirmed on real data (Oct 2026): organization is "
    "the applicant company (P2.17: 3,679 distinct values such as Philip Morris USA Inc.), not a CTP office. It is "
    "the Organization field stewards see in Rhapsody; the output column keeps the name office. record_date is the "
    "first listed date between 2009-06-22 and today, else the lakehouse load date, so the date filter never hides "
    "a mistyped date. "
)
# The eSUB log-in link (Rhapsody reglist_key -> eSUB REGLIST_KEY), used by the log-in-link checks and hints.
_LOGIN_LINK_NOTE = (
    "Confirmed on real data (Oct 2026): every Rhapsody reglist_key is a real eSUB REGLIST_KEY, active or inactive "
    "(K09, 46,778 of 46,778 values). The number and the STN agree for 99.9% of Rhapsody submissions (K10, 46,768 "
    "of 46,817 pairs), and the field is filled on nearly every submission. "
)
_PAIR_NOTE = (
    _MATCH_NOTE
    + "Population: Rhapsody STNs found in eSUB, each paired with ONE eSUB log-in (QUALIFY row_number per STN). "
    "Pick order: an active log-in, then REGLIST_KEY = Rhapsody reglist_key, then the closest match for the check, "
    "then the latest UPDATE_DATE. ESUB_LOGIN_DUPLICATE_STN reports repeat log-ins. An STN found only on inactive "
    "log-ins (K06 vs K08) is still paired and compared, never treated as missing. "
)
_SETTLED_NOTE = (
    "Rhapsody records created (else received, else loaded) in the last 3 days are skipped. "
)
_DATE_NOTE = (
    "eSUB dates are TIMESTAMPs cast to DATE in the SQL warehouse session time zone. Rhapsody dates are DATEs. "
)
# Received date: tested on real data (Part 3 of tools/sql/confirm_match_keys.sql), so no tolerance.
_RECEIVED_DATE_NOTE = (
    _DATE_NOTE
    + "Tolerance: none, the calendar dates must be equal. Confirmed on real data (Oct 2026): eSUB DATE_RECEIVED "
    "is stored at midnight (P3.2: 90% of log-ins at exactly 00:00). For the 43,906 STNs in both systems, the eSUB "
    "and Rhapsody received dates fall on the same calendar date 100% of the time (P3.1). Reading eSUB as UTC "
    "would have put 86% of them one day apart, so there is no time-zone shift. "
)
# Submitted date: a separate query that was NOT tested, so it keeps the 1-day allowance for now.
_SUBMITTED_DATE_NOTE = (
    _DATE_NOTE
    + "Tolerance: |datediff| <= 1 day. Only the received date was tested on real data (Oct 2026, P3.1), and it "
    "showed no time-zone shift. DATE_SUBMITTED was not tested, so the allowance stays until the Part 3 queries of "
    "tools/sql/confirm_match_keys.sql are repeated for it. Remove the allowance if the dates also match exactly. "
)
_SETTLED_TEXT = "Rhapsody records from the last 3 days are skipped while staff finish data entry."

CHECKS: list[Check] = [
    Check(
        id="ESUB_RHAP_MISSING_IN_RHAPSODY",
        title="Submissions missing from Rhapsody",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Completeness",
        severity="High",
        question="Does every submission logged in eSUB appear in Rhapsody?",
        why="A submission that never reaches Rhapsody is never reviewed, and its review clock runs unnoticed. "
            "eSUB and Rhapsody then report different counts of submissions received.",
        how_to_fix="Search Rhapsody for the STN. For 'Under another STN', compare both STNs with the eSUB log-in "
                   "record. Correct the mistyped one. For 'Only in eSUB', ask the Rhapsody intake team to create "
                   "the submission. If the eSUB log-in was a mistake or a duplicate, mark it inactive in eSUB "
                   "instead. Log-ins from the last 5 days are skipped, the same limit as 'Submissions slow to "
                   "reach Rhapsody (over 5 days)'.",
        record_label="submissions",
        filters=frozenset({FILTER_DATE}),
        date_label="Received date",
        warn_below=0.995,
        fail_below=0.98,
        technical_note=_MATCH_NOTE
        + "Population: the most recent active eSUB log-in per STN (FLAG_ON(INACTIVE_IND) is false). Blank-STN "
        "log-ins are excluded, since an internal eSUB check covers them. Log-in day = first plausible of "
        "coalesce(DATE_ENTERED, CREATE_DATE) and DATE_RECEIVED, falling back to the load date. Grace: "
        "datediff(current_date, log-in day) > 5. Era day = first plausible of DATE_RECEIVED and the log-in date, "
        "falling back to the load date. The era day must fall on or after both of these months. (a) The first "
        "month with >= 20 rhapsody_submissions.date_created between 2009-06-22 and today. (b) For each eSUB "
        "DOC_TYPES_KEY, the month of its 5th log-in found in Rhapsody. Scope: DOC_TYPES_KEY groups with >= 10 "
        "log-ins found in Rhapsody AND >= 50% found within their era. Types that never go to Rhapsody are "
        "excluded. Inactive log-ins are left out here; a Rhapsody STN found only on inactive log-ins (about 2,900, "
        "K06 vs K08) counts as found in ESUB_RHAP_MISSING_IN_ESUB; ESUB_RHAP_INACTIVE_STILL_OPEN flags the ones "
        "still open in Rhapsody. Hint category: a Rhapsody "
        "submission has reglist_key equal to one of this STN's REGLIST_KEYs but a different STN. "
        + _LOGIN_LINK_NOTE
        + "Only the date filter is offered. Records only in eSUB have no Rhapsody submission type or organization, "
        "so those filters would hide every problem.",
        assumptions=(
            "eSUB log-ins marked inactive are not expected in Rhapsody.",
            "If most log-ins of an eSUB document type reach Rhapsody, all of them should. Types that rarely reach "
            "Rhapsody are assumed to be handled elsewhere. A fixed list from the data owners would be better.",
            "A document type is expected in Rhapsody only from the month its log-ins first appear there.",
            "Five calendar days after log-in is enough time for a submission to reach Rhapsody.",
            "Submissions received before Rhapsody was in regular use were never meant to be there. Regular use "
            "starts with the first month Rhapsody created 20 or more submissions.",
        ),
        sql=_MISSING_IN_RHAPSODY_SQL,
    ),
    Check(
        id="ESUB_RHAP_MISSING_IN_ESUB",
        title="Rhapsody submissions missing from eSUB",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Completeness",
        severity="High",
        question="Does every Rhapsody submission have an eSUB log-in with the same STN?",
        why="Every submission should be logged in eSUB before review. A Rhapsody submission with no eSUB log-in "
            "may have a mistyped STN, or it may be a test or duplicate record. Sometimes the receipt was never "
            "officially recorded. Official counts then disagree.",
        how_to_fix="Search eSUB for the STN. A submission whose eSUB log-ins are all inactive is still in eSUB, so "
                   "it passes here. If it is still open in Rhapsody, it appears in 'Inactive in eSUB but still "
                   "open in Rhapsody'. For 'Under another STN', "
                   "compare both STNs. Correct the mistyped one, or enter the STN in eSUB if it is blank there. "
                   "For 'Deleted from eSUB', ask the eSUB log-in team whether the submission is real. If it is "
                   "not, close it in Rhapsody. For 'Only in Rhapsody', ask the eSUB log-in team to record it in "
                   "eSUB. Records added in the last 3 days are skipped.",
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        warn_below=0.99,
        fail_below=0.95,
        technical_note=_MATCH_NOTE
        + "Population: Rhapsody STNs created (else received) more than 3 days ago. An STN with no plausible date "
        "is included. Scope: TXT(stdz_submission_type) groups with >= 10 STNs found in eSUB AND >= 50% found, "
        "where found = any non-deleted eSUB log-in, active or inactive. Types that never come through eSUB are "
        "excluded. Pass = the STN is on at least one non-deleted eSUB log-in, active or inactive (it is in "
        "esub_stn). Inactive-only matches count as found. Confirmed on real data (Oct 2026): about 2,900 Rhapsody "
        "STNs are only on inactive log-ins (K06 93.8% on active log-ins vs K08 99.9% on active and inactive ones). "
        "They pass here so they are not counted twice; ESUB_RHAP_INACTIVE_STILL_OPEN flags the ones still open in "
        "Rhapsody. Categories, in order: 'Under another STN' = no log-in has the STN, and reglist_key points at an "
        "eSUB log-in with another or a blank STN. " + _LOGIN_LINK_NOTE
        + "'Deleted from eSUB' = the STN exists only on halo_is_deleted eSUB rows. Anything else is 'Only in "
        "Rhapsody'. record_date = Rhapsody received date, else submitted, else created, else the load date "
        "(plausible dates only). The status detail shows submission_status '1' as Open (1) and '0' as Closed (0); "
        "a blank stays blank.",
        assumptions=(
            "If most Rhapsody submissions of a type have an eSUB log-in, every submission of that type should. "
            "Types that rarely have one are assumed to start directly in Rhapsody.",
        ),
        sql=_MISSING_IN_ESUB_SQL,
    ),
    Check(
        id="ESUB_RHAP_SAME_LOGIN_DIFFERENT_STN",
        title="STN differs: Rhapsody vs linked eSUB record",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Consistency",
        severity="High",
        question="When a Rhapsody submission points to an eSUB log-in record, do both show the same STN?",
        why="Everyone finds a submission by its STN. Different STNs on one log-in send searches and reports to the "
            "wrong submission, or they miss it entirely.",
        how_to_fix="Open the eSUB log-in record and the Rhapsody submission side by side. Check the STN on the "
                   "cover letter or receipt, then correct the system that is wrong. If several Rhapsody submissions "
                   "point to one log-in, one of them is likely linked to the wrong record.",
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        warn_below=0.999,
        fail_below=0.99,
        technical_note=_MATCH_NOTE
        + "Population: Rhapsody STNs whose NUM_KEY(reglist_key) matches a non-deleted eSUB NUM_KEY(REGLIST_KEY), "
        "with eSUB deduplicated per number. Issue: NOT (Rhapsody STN_KEY <=> eSUB STN_KEY), so a blank eSUB STN is "
        "an issue. The details show whether the Rhapsody STN exists elsewhere in eSUB. They also show how many "
        "Rhapsody submissions share the log-in record. The Rhapsody STN counts as in eSUB when it is on any "
        "non-deleted log-in, active or inactive (K08). record_date = Rhapsody received, else eSUB received, else "
        "Rhapsody submitted or created, else the load date. " + _LOGIN_LINK_NOTE
        + "So reglist_key holds the eSUB (iTRAC) log-in record number, and the 0.1% that disagree are what this "
        "check reports. Every eSUB REGLIST_KEY is unique (P4 U01).",
        sql=_SAME_LOGIN_DIFFERENT_STN_SQL,
    ),
    Check(
        id="ESUB_RHAP_LOGIN_LINK_MISMATCH",
        title="Rhapsody's eSUB log-in link is wrong or blank",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Consistency",
        severity="Medium",
        question="Does each Rhapsody submission link to an active eSUB log-in record with the same STN?",
        why="Rhapsody uses this link to pull intake details from eSUB. A blank or wrong link can make reports show "
            "another submission's dates and products.",
        how_to_fix="In Rhapsody, find the eSUB log-in record linked to the submission. Compare it with the eSUB "
                   "log-in records for the STN, listed in the details. Ask the Rhapsody support team to link the "
                   "correct active record. Records in the 'STN differs' group also appear in 'STN differs: "
                   "Rhapsody vs linked eSUB record'. Fix them there first. " + _SETTLED_TEXT,
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        technical_note=_MATCH_NOTE
        + "Population: Rhapsody STNs with any non-deleted eSUB log-in, created (else received) more than 3 days "
        "ago. Issue: reglist_key is blank, is not a non-deleted eSUB REGLIST_KEY, is a REGLIST_KEY whose STN "
        "differs, or is an inactive log-in while the STN has an active one. A link to an inactive log-in passes "
        "when the STN has no active log-in (K06 vs K08: about 2,900 such STNs). ESUB_RHAP_INACTIVE_STILL_OPEN "
        "flags the ones still open in Rhapsody. Keys are compared as NUM_KEY. The details list up to 10 eSUB "
        "log-in numbers per STN (collect_set). " + _LOGIN_LINK_NOTE
        + "So the 'Blank in Rhapsody' group is expected to stay small, and the check needs no narrowing by type "
        "or year.",
        assumptions=(
            "When an STN has both inactive and active eSUB log-ins, the active one is the current record. Rhapsody "
            "should link to the active one.",
        ),
        sql=_LOGIN_LINK_SQL,
    ),
    Check(
        id="ESUB_RHAP_RECEIVED_DATE_DIFFERS",
        title="Received date differs: eSUB vs Rhapsody",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Consistency",
        severity="High",
        question="Does each submission have the same received date in eSUB and Rhapsody?",
        why="The received date starts the review clock and sets the reporting period. A mismatch makes deadlines "
            "and official counts depend on which system you check.",
        how_to_fix="Check the gateway acknowledgment or date stamp on the official receipt. Correct the system that "
                   "is wrong. Any difference is flagged, even a single day. A test on the real data showed that "
                   "both systems record the same calendar date, so a gap is an entry error. " + _SETTLED_TEXT,
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        warn_below=0.99,
        fail_below=0.95,
        technical_note=_PAIR_NOTE
        + "Compares CAST(esub DATE_RECEIVED AS DATE) with rhapsody date_received. Among equally preferred log-ins, "
        "the closest received date wins. " + _RECEIVED_DATE_NOTE
        + "Blank on one side is an issue. Blank on both sides is not evaluated. " + _SETTLED_NOTE
        + "record_date = eSUB received date, else Rhapsody received date, else the submitted dates, else the load "
        "date.",
        sql=_RECEIVED_DATE_SQL,
    ),
    Check(
        id="ESUB_RHAP_SUBMITTED_DATE_DIFFERS",
        title="Submitted date differs: eSUB vs Rhapsody",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Consistency",
        severity="Medium",
        question="Does each submission have the same submitted date in eSUB and Rhapsody?",
        why="The submitted date is the day the company sent the submission. A mismatch makes timing reports and "
            "timelines shared with the company unreliable.",
        how_to_fix="Check the date on the cover letter or the gateway acknowledgment. Correct the system that is "
                   "wrong. A one-day difference is not flagged yet. The received date was tested on the real data "
                   "and showed no time-zone shift, but the submitted date has not been tested. " + _SETTLED_TEXT,
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        technical_note=_PAIR_NOTE
        + "Compares CAST(esub DATE_SUBMITTED AS DATE) with rhapsody date_submitted. Among equally preferred "
        "log-ins, the closest submitted date wins. " + _SUBMITTED_DATE_NOTE
        + "Blank on one side is an issue. Blank on both sides is not evaluated. " + _SETTLED_NOTE
        + "record_date = received date (eSUB, else Rhapsody), else the submitted dates, else the load date.",
        assumptions=(
            "Rhapsody's submitted date should equal eSUB's submitted date.",
            "A time-zone difference may move the submitted date by one day, so a one-day gap is allowed. Only "
            "the received date was tested on the real data, and it showed no such shift.",
        ),
        sql=_SUBMITTED_DATE_SQL,
    ),
    Check(
        id="ESUB_RHAP_COMPANY_DIFFERS",
        title="Company name differs: eSUB vs Rhapsody",
        systems=("esub", "rhapsody"),
        entity="Companies & contacts",
        dimension="Consistency",
        severity="Medium",
        question="Does each submission show the same company in eSUB and Rhapsody?",
        why="Stewards and reviewers search and report by company. A mismatch undercounts a firm's submissions and "
            "can send letters to the wrong firm.",
        how_to_fix="Compare the eSUB log-in record with the company on the cover letter. Correct the company in the "
                   "system that is wrong. If the firm changed its name or was acquired, use the current registered "
                   "name. Capital letters and punctuation are ignored. Endings such as Inc, LLC, Co or Corp are "
                   "also ignored. A match with any Rhapsody company field counts. " + _SETTLED_TEXT,
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        technical_note=_PAIR_NOTE
        + "eSUB company = esub_itrac_reglist.NAME. Rhapsody names = itrac_company, abpc_company_name, "
        "abpc_company_alias, abc_company. Normalization: CO_NAME macro (upper, punctuation -> space, words "
        "THE/INC/INCORPORATED/LLC/LTD/LIMITED/CO/CORP/CORPORATION/COMPANY/LP/LLP/PLC removed, spaces collapsed). "
        "Pass when the eSUB name equals ANY Rhapsody name after normalization. Among equally preferred log-ins, a "
        "matching one is used. Population: pairs where at least one side has a name. " + _SETTLED_NOTE,
        assumptions=(
            "eSUB's NAME field on the log-in record is the company (applicant) name, not a person.",
            "Rhapsody's iTRAC company field is copied from eSUB. A match with Rhapsody's registered company name "
            "or alias is also acceptable.",
        ),
        sql=_COMPANY_SQL,
    ),
    Check(
        id="ESUB_RHAP_PRODUCT_COUNT_DIFFERS",
        title="Product count differs: eSUB vs Rhapsody",
        systems=("esub", "rhapsody"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does Rhapsody have the same number of products as eSUB for each submission?",
        why="Product counts drive workload planning and the published count of products under review. Fewer "
            "products in Rhapsody can mean some were never set up for review.",
        how_to_fix="Open the submission in Rhapsody. Compare its product list with the eSUB count and the products "
                   "listed in the submission itself. Add missing products in Rhapsody or remove ones linked by "
                   "mistake. If the eSUB count was entered wrong, correct it in eSUB. Submissions received in the "
                   "last 30 days are skipped, because products are often added after the log-in date. Submissions "
                   "that Rhapsody marks as rejected are also skipped.",
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        technical_note=_PAIR_NOTE
        + "eSUB count = esub_itrac_reglist.PRODUCT_COUNT. Among equally preferred log-ins, one with a count > 0 is "
        "used. Rhapsody count = COUNT(DISTINCT ANY_KEY(product_id)) per STN_KEY over rhapsody_submission_products "
        "and rhapsody_bulk_products, plus the placeholder rows described below. The count also includes "
        "rhapsody_submission_product_wrapper "
        "(untitled_relationship_stn / untitled_relationship_product_id, temporary copies excluded). halo_is_deleted "
        "rows are removed, and counts are pre-aggregated per STN. Population: pairs with eSUB PRODUCT_COUNT > 0 "
        "and no Rhapsody rejection reason (submission_rejected_desc). A blank or 0 count means not recorded. Pairs "
        "received in the last 30 days are skipped (first plausible eSUB received date, else Rhapsody's). Exact "
        "match required. The no_products flag is shown for context. Confirmed on real data (Oct 2026): the three "
        "Rhapsody lists share one set of product IDs. Every linked and wrapper product ID is on the regular or bulk "
        "product list (K12, K17), and every linked STN is a Rhapsody submission (K11, K18). The bulk list holds 24.4M "
        "rows on only 10 STNs (K47) and does not overlap the regular list (K15). Confirmed on real data (Oct 2026): "
        "one Rhapsody product_id value is a placeholder repeated on 14,359 rows with different product names "
        "(P4 U07). Every other product ID is unique. COUNT(DISTINCT) would collapse all of an STN's placeholder "
        "products into one, so placeholder rows are counted one by one instead. The placeholder is found from the "
        f"data on every run (CTE rhap_placeholder_id): an ANY_KEY(product_id) on more than {PLACEHOLDER_ID_ROWS} "
        "non-deleted rows of rhapsody_products + rhapsody_bulk_products (PLACEHOLDER_ID_ROWS, the same limit as "
        "the Rhapsody <-> PMS checks). Per STN, Rhapsody count = distinct ordinary product IDs + the largest number of "
        "placeholder link rows in any one list (linked, bulk or wrapper). The lists can repeat the same link, so "
        "placeholder rows are not added across lists. The bulk-list detail counts placeholder bulk rows the same "
        "way. The placeholder CTE has one row per ID, so its LEFT JOIN cannot fan out.",
        assumptions=(
            "eSUB's product count is the number of distinct products in the submission when it was logged.",
            "A blank or zero eSUB product count means the count was not recorded, not that there are no products.",
            "Rhapsody does not set up products for submissions it marks as rejected.",
            "Each Rhapsody row with the shared placeholder product ID is a different product. Rhapsody lists each "
            "such product once in every product list that has it.",
        ),
        sql=_PRODUCT_COUNT_SQL,
    ),
    Check(
        id="ESUB_RHAP_INACTIVE_STILL_OPEN",
        title="Inactive in eSUB but still open in Rhapsody",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Consistency",
        severity="Medium",
        question="Is the submission marked closed in Rhapsody when all its eSUB log-ins are inactive?",
        why="A submission withdrawn or replaced in eSUB can stay open in Rhapsody. It then inflates the review queue "
            "and pending counts. Staff may also review it for nothing.",
        how_to_fix="Read the eSUB inactive comment to see why the log-in became inactive. If the submission was "
                   "withdrawn or voided, close it in Rhapsody. If it is still active, ask the eSUB log-in team to "
                   "reactivate the eSUB record. eSUB changes from the last 7 days are skipped. Submissions with a "
                   "blank Rhapsody status are not checked.",
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        technical_note=_PAIR_NOTE
        + "Population: pairs where EVERY non-deleted eSUB log-in for the STN has FLAG_ON(INACTIVE_IND) and the "
        "trimmed Rhapsody submission_status is '0' or '1'. The latest eSUB coalesce(UPDATE_DATE, CREATE_DATE) "
        "across the STN's log-ins must be more than 7 days ago. Closed = status '0', or a Rhapsody rejection "
        "reason (submission_rejected_desc). Open = status '1' with no rejection reason, which is the issue. "
        "Confirmed on real data (Oct 2026): submission_status is blank on 75% of rows, '1' on 7,511 (16%) and "
        "'0' on 3,967 (8.5%), with no text values (P2.14). A blank status is not judged. The earlier word list for "
        "closed text statuses was dropped, since no text values exist. Confirmed on real data (Oct 2026): about "
        "2,900 Rhapsody STNs exist only on inactive eSUB log-ins (K06 93.8% on active log-ins vs K08 99.9% on "
        "active and inactive ones). This check looks at the open ones. ESUB_RHAP_MISSING_IN_ESUB counts all of "
        "them as found in eSUB, so only this check flags them.",
        assumptions=(
            "An inactive eSUB log-in means the submission was withdrawn, voided, duplicated or sent in error.",
            "Rhapsody's submission status 1 means open, and 0 means closed or inactive. The real data shows only "
            "these two codes and blanks, but the meaning of each code still needs confirmation.",
            "A blank Rhapsody status gives no answer, so those submissions are not judged.",
        ),
        sql=_INACTIVE_STILL_OPEN_SQL,
    ),
    Check(
        id="ESUB_RHAP_SLOW_TO_RHAPSODY",
        title="Submissions slow to reach Rhapsody (over 5 days)",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Timeliness",
        severity="Medium",
        question="Was each submission created in Rhapsody within 5 calendar days of being received?",
        why="The review clock starts at receipt. Days lost before a submission appears in Rhapsody cut the time "
            "reviewers have before the deadline.",
        how_to_fix="Look at the 'Type of problem' breakdown. 'Late eSUB log-in' points to the intake step. 'Late "
                   "Rhapsody setup' points to creating the submission in Rhapsody. Raise repeated delays with the "
                   "team that owns that step. Also confirm the received date is right. Submissions not in Rhapsody "
                   "at all appear in 'Submissions missing from Rhapsody'.",
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        technical_note=_PAIR_NOTE
        + "Lag = datediff(rhapsody date_created, CAST(eSUB DATE_RECEIVED AS DATE)). Issue when the lag is > 5 "
        "calendar days, weekends included. Among equally preferred log-ins, the one received closest to the "
        "Rhapsody created date is used. Population: pairs with a Rhapsody created date no later than today. The "
        "eSUB received date must fall between today and the first month in which Rhapsody created >= 20 "
        "submissions. Older records loaded into Rhapsody later are therefore not counted as late. The date checks "
        "cover impossible future dates. Confirmed on real data (Oct 2026): eSUB received dates carry no time-zone "
        "shift (P3.1, P3.2: they equal Rhapsody's received date for all 43,906 shared STNs). The Rhapsody created date was "
        "not tested. Category compares receipt to log-in (DATE_ENTERED, else CREATE_DATE) with "
        "log-in to Rhapsody created date.",
        assumptions=(
            "Rhapsody's created date is the day the submission first appeared in Rhapsody.",
            "Five calendar days, weekends included, is a reasonable limit. The data owners may prefer another "
            "limit or business days.",
            "Submissions received before Rhapsody was in regular use were loaded into it later and are not late.",
        ),
        sql=_SLOW_TO_RHAPSODY_SQL,
    ),
    Check(
        id="ESUB_RHAP_TYPE_UNUSUAL",
        title="Rhapsody type unusual for eSUB document type",
        systems=("esub", "rhapsody"),
        entity="Submissions",
        dimension="Consistency",
        severity="Medium",
        question="Does each submission have the usual Rhapsody type for its eSUB document type?",
        why="The submission type sets the review pathway and the deadlines. A type that does not fit the eSUB "
            "document type is often an entry error.",
        how_to_fix="Compare the eSUB document type and the Rhapsody submission type with the cover letter. Correct "
                   "the one that is wrong. Some unusual pairs are valid, and those need no change.",
        record_label="submissions",
        filters=ALL_FILTERS,
        date_label="Received date",
        warn_below=0.99,
        fail_below=0.97,
        technical_note=_PAIR_NOTE
        + "No official mapping exists from eSUB DOC_TYPES_KEY to Rhapsody stdz_submission_type. The usual type is "
        "learned from the data: the most common TXT-normalized Rhapsody type among matched pairs for each eSUB "
        "document type. Only document types with >= 20 matched pairs where the usual type covers >= 90% are "
        "evaluated. Issue: TXT(Rhapsody type) differs from the usual one. The pass rate cannot drop below 90%, so "
        "the thresholds are tighter.",
        assumptions=(
            "The expected type is the one most submissions of the same eSUB document type have in Rhapsody. It is "
            "not an official mapping. A confirmed mapping from the data owners would make the result exact.",
            "eSUB document types spread over several Rhapsody types, such as amendments, are not checked.",
        ),
        sql=_TYPE_UNUSUAL_SQL,
    ),
]
