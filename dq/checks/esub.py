"""Checks inside eSUB (submission intake / iTRAC log-in list).

Every check here is about eSUB's own records, so stewards see them under the eSUB system card.

Shared conventions used in every query below:
  * Keys, names, blanks and Y/N flags use the shared macros in dq/sqlfrag.py (STN_KEY, NUM_KEY, TXT,
    CO_NAME, BLANK, FLAG_ON), so a record matches here exactly as it does in every other check.
  * Active log-in record = esub_itrac_reglist row with halo_is_deleted not true and INACTIVE_IND not set
    (FLAG_ON). Electronic submissions (esub_tob_submission) additionally drop DELETED_IND / INACTIVE_IND.
  * eSUB numeric IDs (DECIMAL / LONG / INT) are compared as NUM_KEY(x).
  * record_date is the first plausible date (22 June 2009, the Tobacco Control Act, to today) among the
    check's candidate dates, falling back to the load date (halo_insert_ts), so a mistyped 2099 or 1900
    date is never hidden by the date filter.
  * Submission type / organization: eSUB has no standardized submission type or Rhapsody Organization
    field, so both are taken from Rhapsody's record with the same STN (rhapsody_submissions, latest copy per
    STN_KEY; one row per STN, so no fan-out). Confirmed on real data (Oct 2026): rhapsody_submissions.organization
    is the applicant company stewards see as Organization in Rhapsody (P2.17: 3,679 distinct values such as
    "Philip Morris USA Inc."). eSUB records with no STN, or not yet in Rhapsody, are blank and match the
    "(not recorded)" filter value.
  * record_key is the STN; when one STN has several evaluated records, the eSUB record number is appended
    so every row has its own key.

How eSUB's own records link. Confirmed on real data (Oct 2026), tools/sql/confirm_match_keys.sql Part 1:
  * Log-in STN = electronic submission tracking number (esub_itrac_reglist.STN =
    esub_tob_submission.SUBMISSION_TRACKING_NUMBER): 99.8% of electronic submissions match (K02).
  * Log-in TRACKING_NUMBER is a different number: 0% (K01). It is shown for context only, never matched.
  * PACKAGE_KEY ties 100% of electronic submissions to a log-in record (K03).
  * esub_itrac_reglist.STN_LINK is NOT an STN: 0 of 120,700 values match any STN (K39). No check uses it as an
    STN, a fallback for a blank STN or a matching key. A record with no STN simply has no STN.
  * esub_tob_submission.STN_BEING_AMENDED never yields an STN when split (K40), so no check uses it. The
    former ESUB_AMENDED_STN_NOT_FOUND check was retired (Oct 2026) because it evaluated nothing.
  * File list -> electronic submissions 100% (K41); file list -> document list 100% of 24.9M documents (K42);
    99.2% of electronic submission packages have documents in the document list (K43).
  * Application links -> applications 99.0% (K44) and -> log-in record numbers 99.2% (K45).
  * Every document type number on a log-in record is on the type list (K46: 28 of 28).
"""

from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check

_ALL_FILTERS = frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE})

# --------------------------------------------------------------------------- #
# Module-local helpers (only for what the shared macros do not cover)
# --------------------------------------------------------------------------- #


def _ok(col: str) -> str:
    """The value as a DATE when it is a plausible business date (22 June 2009 to today), else NULL."""
    return f"CASE WHEN CAST({col} AS DATE) BETWEEN DATE'2009-06-22' AND current_date() THEN CAST({col} AS DATE) END"


def _first_plausible(*cols: str, fallback: str = "halo_insert_ts") -> str:
    """First plausible date among the candidates, else the load date (AUTHORING 'Naming and dates')."""
    return "coalesce(" + ", ".join([_ok(c) for c in cols] + [f"CAST({fallback} AS DATE)"]) + ")"


def _q(sql: str, **parts: str) -> str:
    """Fill <<name>> slots in a query with shared fragments."""
    for name, text in parts.items():
        sql = sql.replace(f"<<{name}>>", text)
    if "<<" in sql:
        raise ValueError("unfilled <<slot>> in eSUB check SQL")
    return sql


# Active (not soft-deleted, not inactive) eSUB log-in record.
_ACTIVE_LOGIN = "coalesce(halo_is_deleted, false) = false\n    AND NOT FLAG_ON(INACTIVE_IND)"

# Business date of a log-in record: received, else submitted, else logged in, else created, else load date.
_LOGIN_DATE = _first_plausible("DATE_RECEIVED", "DATE_SUBMITTED", "DATE_ENTERED", "CREATE_DATE")

# A received date may fall up to this many days after the log-in date (next-business-day receipt across weekends
# and holidays). ESUB_LOGIN_DATES_IMPOSSIBLE uses it both for "logged in before received" and for "received date
# in the future", so a received date the rule allows is never flagged.
_RECEIVED_AFTER_LOGIN_DAYS = 5

# A type-list flag that is explicitly switched off (blank means still on). FLAG_ON covers only "set".
_FLAG_OFF = "coalesce(upper(trim(CAST({x} AS STRING))) IN ('N', 'NO', '0', 'F', 'FALSE'), false)"

_RHAP_TAG = r"""
rhap_tag AS (
  -- Rhapsody's standard submission type, organization (applicant) and company for each STN (latest copy, one row
  -- per STN), so eSUB records can be filtered and broken down like every other check.
  SELECT
    STN_KEY(stn)                                                              AS stn_key,
    BLANK(stdz_submission_type)                                               AS r_type,
    BLANK(organization)                                                       AS r_org,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company),
             BLANK(mvw_submission_org_company))                               AS r_company
  FROM ${core}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY STN_KEY(stn)
    ORDER BY halo_update_ts DESC NULLS LAST, halo_insert_ts DESC NULLS LAST, date_created DESC NULLS LAST
  ) = 1
)"""

_KNOWN_STNS = r"""
known_stns AS (
  -- Every STN eSUB has: on a log-in record (active or inactive) or on an electronic submission.
  SELECT
    stn_key,
    max(on_login)                                                             AS on_login,
    max(on_active_login)                                                      AS on_active_login,
    max(on_esub)                                                              AS on_esub
  FROM (
    SELECT STN_KEY(STN) AS stn_key, 1 AS on_login,
           CASE WHEN FLAG_ON(INACTIVE_IND) THEN 0 ELSE 1 END AS on_active_login, 0 AS on_esub
    FROM ${core}.esub_itrac_reglist
    WHERE coalesce(halo_is_deleted, false) = false
    UNION ALL
    SELECT STN_KEY(SUBMISSION_TRACKING_NUMBER) AS stn_key, 0 AS on_login, 0 AS on_active_login, 1 AS on_esub
    FROM ${core}.esub_tob_submission
    WHERE coalesce(halo_is_deleted, false) = false
      AND NOT FLAG_ON(DELETED_IND)
  ) AS all_stns
  WHERE stn_key IS NOT NULL
  GROUP BY stn_key
)"""

_INACTIVE_ASSUMPTION = "An inactive log-in record was withdrawn or replaced, so these counts exclude it."
_TAG_ASSUMPTION = (
    "Submission type and organization come from the Rhapsody record with the same STN. Records with no STN "
    "or no Rhapsody match show '(not recorded)' in those filters."
)
_TAG_NOTE = (
    " Submission type / organization = Rhapsody stdz_submission_type / organization for the record's own "
    "STN_KEY; a record with no STN has none, and STN_LINK is never used in its place. Confirmed on real data "
    "(Oct 2026): STN_LINK is not an STN (K39). Confirmed on real data (Oct 2026): organization is the applicant "
    "company stewards see as Organization in Rhapsody (P2.17: 3,679 distinct values), not a CTP "
    "office. Source: the latest non-deleted rhapsody_submissions row per "
    "STN (halo_is_deleted excluded), so each STN matches one row. Blank when there is no Rhapsody match. "
    "record_date = first date between 2009-06-22 and today among the listed candidates, else "
    "CAST(halo_insert_ts AS DATE)."
)

CHECKS: list[Check] = [
    # ------------------------------------------------------------------ #
    # Completeness
    # ------------------------------------------------------------------ #
    Check(
        id="ESUB_LOGIN_REQUIRED_FIELDS_BLANK",
        title="Log-in records missing required fields",
        systems=("esub",),
        entity="Submissions",
        dimension="Completeness",
        severity="High",
        question="Does every active eSUB log-in record have an STN, received date, company and document type?",
        why="Staff find and count submissions by these four fields. A blank received date also leaves the "
            "review clock with no start date.",
        how_to_fix="Find the log-in record in eSUB by STN or eSUB record number. Fill in the blank field from "
                   "the cover letter or receipt. If the record should not exist, ask the Document Control "
                   "Center to mark it inactive. A blank STN is flagged even when the record has an STN link, "
                   "because that eSUB field does not hold an STN.",
        record_label="log-in records",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: active slvr_ctp_core.esub_itrac_reglist rows (halo_is_deleted not true, NOT "
            "FLAG_ON(INACTIVE_IND)); one row per REGLIST_KEY (an STN logged in twice appears twice, with the "
            "record number in record_key; see ESUB_LOGIN_DUPLICATE_STN). STN blank = STN_KEY(STN) NULL, only for "
            "records logged in (DATE_ENTERED, else CREATE_DATE) more than 3 days ago or with no log-in date. "
            "Confirmed on real data (Oct 2026): STN_LINK is not an STN (K39: 0 of 120,700 values match any "
            "log-in STN), so it no longer excuses a blank STN and is not shown as one. Received date blank = "
            "DATE_RECEIVED NULL. Company = NAME. "
            "It is blank when BLANK(NAME) is NULL or has no letter or digit in any alphabet. It is also blank "
            "when TXT(NAME) without punctuation is NA / NONE / UNKNOWN / TBD / NULL / NOTAPPLICABLE (names in "
            "non-Latin scripts are not blank). Document type blank = NUM_KEY(DOC_TYPES_KEY) NULL; the name comes "
            "from esub_itrac_doc_types grouped per key (no duplicate rows). Confirmed on real data (Oct 2026): "
            "every type number used is on the list (K46: 28 of 28). TRACKING_NUMBER is shown for context only. "
            "Confirmed on real data (Oct 2026): it is not the electronic submission tracking number (K01: 0%). "
            "Record dates: DATE_RECEIVED, DATE_SUBMITTED, DATE_ENTERED, CREATE_DATE."
            + _TAG_NOTE
        ),
        assumptions=(
            "NAME on a log-in record is the company (applicant). SUBMITTER_NAME is the person who sent it.",
            "STNs are assigned within 3 days of the log-in date. Newer records with no STN are not flagged.",
            _INACTIVE_ASSUMPTION,
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
logins AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    STN_KEY(STN)                                                             AS stn_key,
    BLANK(STN)                                                               AS stn,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number,
    BLANK(NAME)                                                              AS company,
    coalesce(BLANK(NAME) IS NULL
             OR NOT (NAME rlike '[\\p{L}\\p{N}]')
             OR regexp_replace(TXT(NAME), '[^A-Z0-9]', '')
                IN ('NA', 'NONE', 'UNKNOWN', 'TBD', 'NULL', 'NOTAPPLICABLE'), false) AS blank_company,
    BLANK(SUBMITTER_NAME)                                                    AS submitter,
    NUM_KEY(DOC_TYPES_KEY)                                                   AS doc_type_code,
    CAST(DATE_SUBMITTED AS DATE)                                             AS date_submitted,
    CAST(DATE_RECEIVED AS DATE)                                              AS date_received,
    CAST(coalesce(DATE_ENTERED, CREATE_DATE) AS DATE)                        AS date_logged_in,
    <<login_date>>                                                           AS record_date
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
),
doc_types AS (
  SELECT
    NUM_KEY(DOC_TYPES_KEY)                                                   AS doc_type_code,
    max(BLANK(DOC_TYPE))                                                     AS doc_type_name
  FROM ${core}.esub_itrac_doc_types
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY NUM_KEY(DOC_TYPES_KEY)
),
flagged AS (
  SELECT
    l.record_no, l.stn_key, l.stn, l.tracking_number, l.company, l.submitter,
    l.doc_type_code, d.doc_type_name, l.date_submitted, l.date_received, l.date_logged_in, l.record_date,
    (l.stn_key IS NULL
       AND coalesce(datediff(current_date(), l.date_logged_in), 999) > 3)    AS blank_stn,
    (l.date_received IS NULL)                                                AS blank_received,
    l.blank_company,
    (l.doc_type_code IS NULL)                                                AS blank_doc_type,
    count(*) OVER (PARTITION BY l.stn_key)                                   AS stn_records
  FROM logins l
  LEFT JOIN doc_types d ON d.doc_type_code = l.doc_type_code
),
scored AS (
  SELECT
    f.*,
    CAST(f.blank_stn AS INT) + CAST(f.blank_received AS INT)
      + CAST(f.blank_company AS INT) + CAST(f.blank_doc_type AS INT)        AS n_blank,
    concat_ws(', ',
      CASE WHEN f.blank_stn      THEN 'STN' END,
      CASE WHEN f.blank_received THEN 'received date' END,
      CASE WHEN f.blank_company AND f.company IS NULL THEN 'company name'
           WHEN f.blank_company THEN concat('company name (shown as "', f.company, '")') END,
      CASE WHEN f.blank_doc_type THEN 'document type' END)                   AS blank_list,
    concat_ws(', ',
      CASE WHEN f.tracking_number IS NOT NULL THEN concat('tracking number ', f.tracking_number) END,
      CASE WHEN f.date_logged_in IS NOT NULL
           THEN concat('logged in ', CAST(f.date_logged_in AS STRING)) END) AS context
  FROM flagged f
)
SELECT
  CASE WHEN s.stn_key IS NULL THEN concat('(no STN) eSUB record ', coalesce(s.record_no, '?'))
       WHEN s.stn_records > 1 THEN concat(s.stn_key, ' (eSUB record ', coalesce(s.record_no, '?'), ')')
       ELSE s.stn_key END                                            AS record_key,
  s.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(s.n_blank > 0, false)                                     AS is_issue,
  CASE WHEN s.n_blank > 1     THEN 'Several blanks'
       WHEN s.blank_stn       THEN 'Blank STN'
       WHEN s.blank_received  THEN 'Blank received date'
       WHEN s.blank_company   THEN 'Blank company'
       WHEN s.blank_doc_type  THEN 'Blank document type'
  END                                                                AS issue_category,
  CASE WHEN s.n_blank > 0 THEN
    concat('Blank: ', s.blank_list, '.',
           CASE WHEN s.context <> '' THEN concat(' ', upper(substr(s.context, 1, 1)), substr(s.context, 2), '.')
                ELSE '' END)
  END                                                                AS issue_reason,
  s.stn                                                              AS `STN`,
  s.company                                                          AS `Company`,
  s.submitter                                                        AS `Submitter`,
  coalesce(s.doc_type_name, s.doc_type_code)                         AS `Document type`,
  s.date_submitted                                                   AS `Submitted date`,
  s.date_received                                                    AS `Received date`,
  s.date_logged_in                                                   AS `Logged-in date`,
  s.tracking_number                                                  AS `Tracking number`,
  s.record_no                                                        AS `eSUB record number`
FROM scored s
LEFT JOIN rhap_tag rt ON rt.stn_key = s.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, login_date=_LOGIN_DATE),
    ),
    Check(
        id="ESUB_SUBMISSION_NO_FILES",
        title="Electronic submissions with no files attached",
        systems=("esub",),
        entity="Documents",
        dimension="Completeness",
        severity="High",
        question="Does every logged-in electronic submission in eSUB have at least one file attached?",
        why="Reviewers work from the files. A submission with no files cannot be reviewed, and FDA's record "
            "of what the company sent is incomplete.",
        how_to_fix="Open the submission in eSUB and check its files. If the files are in the document repository "
                   "but not attached, ask eSUB support to re-attach them. If nothing arrived, ask the Document "
                   "Control Center to contact the submitter.",
        record_label="submissions",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: slvr_ctp_core.esub_tob_submission rows with halo_is_deleted not true and neither "
            "FLAG_ON(DELETED_IND) nor FLAG_ON(INACTIVE_IND). Each must match an active esub_itrac_reglist log-in by "
            "STN_KEY(SUBMISSION_TRACKING_NUMBER) = STN_KEY(STN) or by NUM_KEY(PACKAGE_KEY). Its earliest "
            "received date must be more than 2 days ago (or unknown). Files come from esub_submission_document "
            "links (by SUBMISSION_KEY) whose DOCUMENT_KEY exists in non-deleted esub_document. Files also come "
            "from non-deleted esub_document rows in the submission's PACKAGE_KEY or in the PACKAGE_KEY of any "
            "active log-in with the same STN. Each source is pre-aggregated to one row per key (no duplicate "
            "rows). A submission with a cover-letter file name is not flagged. Output is one row per STN_KEY (several "
            "electronic records with the same STN are combined and flagged only if none of them has files); "
            "records with no STN matched only by PACKAGE_KEY are one row per SUBMISSION_KEY. Record dates: "
            "earliest plausible log-in DATE_RECEIVED (by STN, else by package), else submission CREATE_DATE. "
            "Confirmed on real data (Oct 2026): the log-in STN is the electronic submission's "
            "SUBMISSION_TRACKING_NUMBER (K02: 99.8% of electronic submissions match), the log-in TRACKING_NUMBER "
            "is a different number and is not used (K01: 0%), and PACKAGE_KEY ties 100% of electronic "
            "submissions to a log-in (K03). Confirmed on real data (Oct 2026): every submission-file row points "
            "at an electronic submission (K41: 100%) and at a document in esub_document (K42: 100% of 23.8M; "
            "24.9M documents), and 99.2% of electronic submission packages have documents (K43)."
            + _TAG_NOTE
        ),
        assumptions=(
            "eSUB's document list records every file for an electronic submission. No files live only in "
            "another repository.",
            "Files can take up to 2 days to appear after receipt. Newer submissions are not checked yet.",
            "A submission that has only a cover letter is not flagged.",
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
active_logins AS (
  SELECT
    STN_KEY(STN)                                                             AS stn_key,
    NUM_KEY(PACKAGE_KEY)                                                     AS package_no,
    <<received_ok>>                                                          AS received_ok,
    BLANK(NAME)                                                              AS company
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
),
login_by_stn AS (
  SELECT stn_key, min(received_ok) AS first_received, max(company) AS company
  FROM active_logins
  WHERE stn_key IS NOT NULL
  GROUP BY stn_key
),
login_by_package AS (
  SELECT package_no, min(received_ok) AS first_received, max(company) AS company
  FROM active_logins
  WHERE package_no IS NOT NULL
  GROUP BY package_no
),
subs AS (
  SELECT
    NUM_KEY(SUBMISSION_KEY)                                                  AS submission_no,
    STN_KEY(SUBMISSION_TRACKING_NUMBER)                                      AS stn_key,
    BLANK(SUBMISSION_TRACKING_NUMBER)                                        AS stn,
    NUM_KEY(PACKAGE_KEY)                                                     AS package_no,
    BLANK(SUBMISSION_TYPE_CODE)                                              AS esub_type_code,
    BLANK(COVER_LETTER_FILE_NAME)                                            AS cover_letter,
    FLAG_ON(HOLD_IND)                                                        AS on_hold,
    <<created_date>>                                                         AS created_date
  FROM ${core}.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
    AND NOT FLAG_ON(DELETED_IND)
    AND NOT FLAG_ON(INACTIVE_IND)
),
docs AS (
  SELECT
    NUM_KEY(DOCUMENT_KEY)                                                    AS document_no,
    NUM_KEY(PACKAGE_KEY)                                                     AS package_no
  FROM ${core}.esub_document
  WHERE coalesce(halo_is_deleted, false) = false
),
doc_numbers AS (
  SELECT DISTINCT document_no FROM docs WHERE document_no IS NOT NULL
),
files_by_submission AS (
  SELECT
    NUM_KEY(sd.SUBMISSION_KEY)                                               AS submission_no,
    count(*)                                                                 AS n_links,
    count(dn.document_no)                                                    AS n_files
  FROM ${core}.esub_submission_document sd
  LEFT JOIN doc_numbers dn ON dn.document_no = NUM_KEY(sd.DOCUMENT_KEY)
  WHERE coalesce(sd.halo_is_deleted, false) = false
  GROUP BY NUM_KEY(sd.SUBMISSION_KEY)
),
files_by_package AS (
  SELECT package_no, count(*) AS n_files
  FROM docs
  WHERE package_no IS NOT NULL
  GROUP BY package_no
),
files_by_login_stn AS (
  -- Files in the packages of the STN's own active log-in records.
  SELECT p.stn_key, sum(fp.n_files) AS n_files
  FROM (SELECT DISTINCT stn_key, package_no FROM active_logins
        WHERE stn_key IS NOT NULL AND package_no IS NOT NULL) AS p
  JOIN files_by_package fp ON fp.package_no = p.package_no
  GROUP BY p.stn_key
),
evaluated AS (
  SELECT
    coalesce(s.stn_key, concat('#', coalesce(s.submission_no, '?')))        AS unit,
    s.submission_no, s.stn_key, s.stn, s.package_no, s.esub_type_code, s.cover_letter, s.on_hold,
    coalesce(ls.company, lp.company)                                         AS company,
    coalesce(ls.first_received, lp.first_received, s.created_date)           AS received_date,
    coalesce(fs.n_links, 0)                                                  AS n_links,
    coalesce(fs.n_files, 0) + coalesce(fp.n_files, 0) + coalesce(fl.n_files, 0) AS n_files
  FROM subs s
  LEFT JOIN login_by_stn ls        ON ls.stn_key = s.stn_key
  LEFT JOIN login_by_package lp    ON lp.package_no = s.package_no
  LEFT JOIN files_by_submission fs ON fs.submission_no = s.submission_no
  LEFT JOIN files_by_package fp    ON fp.package_no = s.package_no
  LEFT JOIN files_by_login_stn fl  ON fl.stn_key = s.stn_key
  WHERE ls.stn_key IS NOT NULL OR lp.package_no IS NOT NULL
),
by_submission AS (
  SELECT
    unit,
    max(stn_key)                                                             AS stn_key,
    max(stn)                                                                 AS stn,
    max(company)                                                             AS company,
    min(received_date)                                                       AS received_date,
    sum(n_links)                                                             AS n_links,
    sum(n_files)                                                             AS n_files,
    max(cover_letter)                                                        AS cover_letter,
    max(CASE WHEN on_hold THEN 1 ELSE 0 END)                                 AS on_hold,
    array_join(array_sort(collect_set(esub_type_code)), ', ')                AS esub_type_codes,
    array_join(slice(array_sort(collect_set(package_no)), 1, 10), ', ')      AS package_numbers,
    array_join(slice(array_sort(collect_set(submission_no)), 1, 10), ', ')   AS submission_numbers
  FROM evaluated
  GROUP BY unit
  HAVING coalesce(datediff(current_date(), min(received_date)), 999) > 2
),
flagged AS (
  SELECT
    b.*,
    coalesce(b.n_files = 0 AND b.cover_letter IS NULL, false)                AS no_files
  FROM by_submission b
)
SELECT
  coalesce(f.stn_key, concat('(no STN) eSUB submission ', f.submission_numbers)) AS record_key,
  f.received_date                                                    AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  f.no_files                                                         AS is_issue,
  CASE WHEN f.no_files AND f.n_links > 0 THEN 'Listed files missing'
       WHEN f.no_files                   THEN 'No files'
  END                                                                AS issue_category,
  CASE WHEN f.no_files AND f.n_links > 0 THEN
         concat('eSUB lists ', CAST(f.n_links AS STRING),
                ' file(s), but none is in its document list. There is no cover letter.')
       WHEN f.no_files THEN
         concat('Logged in', CASE WHEN f.received_date IS NOT NULL
                                  THEN concat(' (received ', CAST(f.received_date AS STRING), ')') ELSE '' END,
                ', but eSUB has no files or cover letter.')
  END                                                                AS issue_reason,
  f.stn                                                              AS `STN`,
  f.company                                                          AS `Company`,
  nullif(f.esub_type_codes, '')                                      AS `eSUB type code`,
  f.received_date                                                    AS `Received date`,
  f.n_links                                                          AS `Files listed`,
  CASE WHEN f.on_hold = 1 THEN 'Yes' ELSE 'No' END                   AS `On hold`,
  nullif(f.package_numbers, '')                                      AS `Package number`,
  f.submission_numbers                                               AS `eSUB submission number`
FROM flagged f
LEFT JOIN rhap_tag rt ON rt.stn_key = f.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, received_ok=_ok("DATE_RECEIVED"),
            created_date=_first_plausible("CREATE_DATE")),
    ),
    Check(
        id="ESUB_SUBMISSION_NOT_LOGGED_IN",
        title="Electronic submissions with no log-in record",
        systems=("esub",),
        entity="Submissions",
        dimension="Completeness",
        severity="High",
        question="Does every electronic submission received through eSUB have a log-in record?",
        why="A submission with no log-in record never reaches CTP reviewers and is missing from official "
            "counts. The company may already be waiting on a decision.",
        how_to_fix="Search the eSUB log-in list for the STN and the package number shown. If the submission was "
                   "logged under a different STN, correct the STN on the log-in record. If there is no log-in "
                   "record, ask the Document Control Center to create one. If it is a test or a duplicate, ask "
                   "eSUB support to mark the electronic submission inactive. Submissions from the last 3 days "
                   "or on hold are not checked.",
        record_label="submissions",
        filters=frozenset({FILTER_DATE}),
        date_label="Date created in eSUB",
        warn_below=0.995,
        fail_below=0.98,
        technical_note=(
            "Population: slvr_ctp_core.esub_tob_submission rows with halo_is_deleted not true and neither "
            "FLAG_ON(DELETED_IND) nor FLAG_ON(INACTIVE_IND). Rows on hold (FLAG_ON(HOLD_IND)) or with no STN "
            "(STN_KEY(SUBMISSION_TRACKING_NUMBER) NULL) are excluded. Rows are combined to one per STN_KEY, "
            "earliest created more than 3 days ago (or unknown). Logged in = some non-deleted esub_itrac_reglist "
            "row (active or inactive) has STN_KEY(STN) equal to it, or NUM_KEY(PACKAGE_KEY) equal to one of its "
            "packages (both lookups DISTINCT, so rows do not multiply). Confirmed on real data (Oct 2026): the "
            "log-in STN is the electronic submission tracking number (K02: 99.8% of electronic submissions "
            "match), the log-in TRACKING_NUMBER is a different number (K01: 0%, never used), and PACKAGE_KEY "
            "ties 100% of electronic submissions to a log-in (K03). Confirmed on real data (Oct 2026): STN_LINK "
            "is not an STN (K39: 0 of 120,700 values match any STN), so the former 'STN or STN link' test (K38: "
            "100% of due electronic submissions found) now uses the log-in STN and the package only. Scope: only "
            "TXT(SUBMISSION_TYPE_CODE) groups where >= 10 submissions are logged in AND they make up >= 50% of the "
            "group. This "
            "excludes electronic forms that never get a log-in record, such as registrations handled elsewhere. "
            "Category 'No log-in, in Rhapsody' = Rhapsody has the same STN_KEY. Record dates: CREATE_DATE. Only "
            "the date filter is offered because submissions with no log-in record rarely have a Rhapsody record "
            "that could supply a type or organization." + _TAG_NOTE
        ),
        assumptions=(
            "An electronic submission gets its STN only after FDA receives it, so unsent drafts are excluded.",
            "If most submissions of one kind have log-in records, every submission of that kind should have "
            "one. Kinds that rarely have log-in records are assumed to go through another process. A fixed "
            "list from the eSUB owners would be better.",
            "A submission on hold needs no log-in record until the hold is lifted.",
            "A new electronic submission should have a log-in record within 3 calendar days.",
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
login_stns AS (
  -- Every STN on a log-in record (active or inactive). STN_LINK is not an STN (K39), so it is not used.
  SELECT DISTINCT STN_KEY(STN) AS stn_key
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(STN) IS NOT NULL
),
login_packages AS (
  SELECT DISTINCT NUM_KEY(PACKAGE_KEY) AS package_no
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(PACKAGE_KEY) IS NOT NULL
),
subs AS (
  SELECT
    NUM_KEY(SUBMISSION_KEY)                                                  AS submission_no,
    STN_KEY(SUBMISSION_TRACKING_NUMBER)                                      AS stn_key,
    BLANK(SUBMISSION_TRACKING_NUMBER)                                        AS stn,
    NUM_KEY(PACKAGE_KEY)                                                     AS package_no,
    coalesce(TXT(SUBMISSION_TYPE_CODE), '(blank)')                           AS type_group,
    BLANK(SUBMISSION_TYPE_CODE)                                              AS esub_type_code,
    <<created_date>>                                                         AS created_date
  FROM ${core}.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
    AND NOT FLAG_ON(DELETED_IND)
    AND NOT FLAG_ON(INACTIVE_IND)
    AND NOT FLAG_ON(HOLD_IND)
    AND STN_KEY(SUBMISSION_TRACKING_NUMBER) IS NOT NULL
),
matched AS (
  SELECT
    s.submission_no, s.stn_key, s.stn, s.package_no, s.type_group, s.esub_type_code, s.created_date,
    (ls.stn_key IS NOT NULL OR lp.package_no IS NOT NULL)                    AS logged_in
  FROM subs s
  LEFT JOIN login_stns ls     ON ls.stn_key = s.stn_key
  LEFT JOIN login_packages lp ON lp.package_no = s.package_no
),
in_scope_types AS (
  SELECT type_group
  FROM matched
  GROUP BY type_group
  HAVING count_if(logged_in) >= 10 AND count_if(logged_in) * 2 >= count(*)
),
by_stn AS (
  SELECT
    m.stn_key,
    max(m.stn)                                                               AS stn,
    max(CASE WHEN m.logged_in THEN 1 ELSE 0 END)                             AS logged_in,
    min(m.created_date)                                                      AS created_date,
    array_join(array_sort(collect_set(m.esub_type_code)), ', ')              AS esub_type_codes,
    array_join(slice(array_sort(collect_set(m.package_no)), 1, 10), ', ')    AS package_numbers,
    array_join(slice(array_sort(collect_set(m.submission_no)), 1, 10), ', ') AS submission_numbers
  FROM matched m
  JOIN in_scope_types t ON t.type_group = m.type_group
  GROUP BY m.stn_key
  HAVING coalesce(datediff(current_date(), min(m.created_date)), 999) > 3
)
SELECT
  b.stn_key                                                          AS record_key,
  b.created_date                                                     AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(b.logged_in = 0, false)                                   AS is_issue,
  CASE WHEN b.logged_in = 0 AND rt.stn_key IS NOT NULL THEN 'No log-in, in Rhapsody'
       WHEN b.logged_in = 0                            THEN 'No log-in'
  END                                                                AS issue_category,
  CASE WHEN b.logged_in = 0 THEN
    concat(coalesce(b.stn, b.stn_key), ' was created in eSUB',
           CASE WHEN b.created_date IS NOT NULL
                THEN concat(' on ', CAST(b.created_date AS STRING), ' (',
                            CAST(datediff(current_date(), b.created_date) AS STRING), ' days ago)')
                ELSE '' END,
           '. No log-in record has this STN or its package',
           CASE WHEN rt.stn_key IS NOT NULL THEN ', but Rhapsody has it' ELSE '' END,
           '.')
  END                                                                AS issue_reason,
  b.stn                                                              AS `STN`,
  rt.r_company                                                       AS `Company (Rhapsody)`,
  nullif(b.esub_type_codes, '')                                      AS `eSUB type code`,
  b.created_date                                                     AS `Created in eSUB`,
  CASE WHEN rt.stn_key IS NOT NULL THEN 'Yes' ELSE 'No' END          AS `In Rhapsody`,
  nullif(b.package_numbers, '')                                      AS `Package number`,
  b.submission_numbers                                               AS `eSUB submission number`
FROM by_stn b
LEFT JOIN rhap_tag rt ON rt.stn_key = b.stn_key
""", rhap_tag=_RHAP_TAG, created_date=_first_plausible("CREATE_DATE")),
    ),
    Check(
        id="ESUB_LINK_STN_NOT_FOUND",
        title="Broken or repeated submission links",
        systems=("esub",),
        entity="Submission links",
        dimension="Completeness",
        severity="Medium",
        question="Does every submission link in eSUB connect two different STNs that eSUB has, with no repeats?",
        why="Links tie amendments and related filings to the original submission. A link to a mistyped STN "
            "hides the related filing from reviewers and makes amendment counts wrong.",
        how_to_fix="Open the STN shown in eSUB and review its linked submissions. Fix a mistyped STN using the "
                   "company's cover letter, or remove a link entered by mistake. Remove any extra copy of a "
                   "repeated link.",
        record_label="links",
        filters=_ALL_FILTERS,
        date_label="Link created date",
        technical_note=(
            "Population: slvr_ctp_core.esub_itrac_sub_links rows with halo_is_deleted not true; one row per "
            "SUB_LINKS_KEY. STN and CHILD_STN are compared as STN_KEY and looked up in the set of STNs on any "
            "non-deleted esub_itrac_reglist row (active or inactive) or non-deleted (DELETED_IND) "
            "esub_tob_submission SUBMISSION_TRACKING_NUMBER, grouped to one row per STN (no duplicate rows). Links "
            "to STNs found only on inactive log-ins or only as electronic submissions are not flagged but are "
            "labeled in the detail. Repeat = same STN_KEY pair and TXT(DESCRIPTOR_CODE) more than once. The "
            "earliest copy (by CREATE_DATE, then SUB_LINKS_KEY) is kept and later copies are flagged. record_key = 'STN -> linked "
            "STN', plus the link record number when the pair repeats. Type/organization from the link's STN, else the "
            "linked STN. Record dates: CREATE_DATE, UPDATE_DATE. Confirmed on real data (Oct 2026): 96.6% of "
            "link STNs and 100% of linked STNs are STNs eSUB has (K04, K05). The known-STN set uses only the "
            "log-in STN and SUBMISSION_TRACKING_NUMBER (the same number, K02); STN_LINK is not an STN (K39) and "
            "is never added to it." + _TAG_NOTE
        ),
        assumptions=(
            "Every linked STN should itself be in eSUB. Links to submissions older than eSUB, if any, will "
            "appear here.",
            "STN holds the submission that owns the link. CHILD_STN holds the linked submission.",
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
<<known_stns>>,
links AS (
  SELECT
    NUM_KEY(SUB_LINKS_KEY)                                                   AS link_no,
    BLANK(STN)                                                               AS stn,
    BLANK(CHILD_STN)                                                         AS linked_stn,
    STN_KEY(STN)                                                             AS stn_key,
    STN_KEY(CHILD_STN)                                                       AS linked_key,
    BLANK(DESCRIPTOR_CODE)                                                   AS link_type,
    BLANK(COMMENTS)                                                          AS comments,
    CAST(coalesce(CREATE_DATE, UPDATE_DATE) AS DATE)                         AS link_date,
    <<link_date>>                                                            AS record_date,
    row_number() OVER (
      PARTITION BY STN_KEY(STN), STN_KEY(CHILD_STN), TXT(DESCRIPTOR_CODE)
      ORDER BY CREATE_DATE NULLS LAST, SUB_LINKS_KEY)                        AS copy_number,
    count(*) OVER (PARTITION BY STN_KEY(STN), STN_KEY(CHILD_STN))            AS pair_rows
  FROM ${core}.esub_itrac_sub_links
  WHERE coalesce(halo_is_deleted, false) = false
),
flagged AS (
  SELECT
    l.*,
    p.stn_key                                                                AS found_stn,
    p.on_active_login                                                        AS stn_active,
    p.on_login                                                               AS stn_on_login,
    c.stn_key                                                                AS found_linked,
    c.on_active_login                                                        AS linked_active,
    c.on_login                                                               AS linked_on_login,
    (l.stn_key IS NULL OR l.linked_key IS NULL)                              AS f_blank,
    coalesce(l.stn_key = l.linked_key, false)                                AS f_self,
    (l.stn_key IS NOT NULL AND p.stn_key IS NULL)                            AS f_stn_missing,
    (l.linked_key IS NOT NULL AND c.stn_key IS NULL)                         AS f_linked_missing,
    (l.copy_number > 1)                                                      AS f_repeat
  FROM links l
  LEFT JOIN known_stns p ON p.stn_key = l.stn_key
  LEFT JOIN known_stns c ON c.stn_key = l.linked_key
)
SELECT
  concat(coalesce(f.stn_key, '(no STN)'), ' -> ', coalesce(f.linked_key, '(no STN)'),
         CASE WHEN f.pair_rows > 1 THEN concat(' (link record ', coalesce(f.link_no, '?'), ')') ELSE '' END)
                                                                     AS record_key,
  f.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(f.f_blank OR f.f_self OR f.f_stn_missing OR f.f_linked_missing OR f.f_repeat, false) AS is_issue,
  CASE WHEN f.f_blank                               THEN 'Blank STN'
       WHEN f.f_self                                THEN 'Linked to itself'
       WHEN f.f_stn_missing AND f.f_linked_missing  THEN 'Both STNs unknown'
       WHEN f.f_stn_missing                         THEN 'STN unknown'
       WHEN f.f_linked_missing                      THEN 'Linked STN unknown'
       WHEN f.f_repeat                              THEN 'Repeated link'
  END                                                                AS issue_category,
  CASE WHEN f.f_blank AND f.stn_key IS NULL AND f.linked_key IS NULL THEN
         'The link has no STN on either side.'
       WHEN f.f_blank AND f.stn_key IS NULL THEN
         concat('The link points to ', f.linked_stn, ', but its own STN is blank.')
       WHEN f.f_blank THEN
         concat('The link on ', f.stn, ' does not name a linked STN.')
       WHEN f.f_self THEN
         concat('STN ', f.stn, ' is linked to itself.')
       WHEN f.f_stn_missing AND f.f_linked_missing THEN
         concat('Neither ', f.stn, ' nor ', f.linked_stn, ' is in eSUB.')
       WHEN f.f_stn_missing THEN
         concat(f.stn, ' is not in eSUB, but it has a link to ', f.linked_stn, '.')
       WHEN f.f_linked_missing THEN
         concat(f.stn, ' links to ', f.linked_stn, ', which is not in eSUB.')
       WHEN f.f_repeat THEN
         concat('Extra copy of the link from ', f.stn, ' to ', f.linked_stn,
                CASE WHEN f.link_type IS NOT NULL THEN concat(' (type ', f.link_type, ')') ELSE '' END,
                '. Entered at least ', CAST(f.copy_number AS STRING), ' times.')
  END                                                                AS issue_reason,
  f.stn                                                              AS `STN`,
  f.linked_stn                                                       AS `Linked STN`,
  f.link_type                                                        AS `Link type`,
  CASE WHEN f.stn_key IS NULL      THEN 'Blank'
       WHEN f.found_stn IS NULL    THEN 'Not found'
       WHEN f.stn_active = 1       THEN 'Yes'
       WHEN f.stn_on_login = 1     THEN 'Inactive log-in only'
       ELSE 'Electronic submission only' END                         AS `STN in eSUB`,
  CASE WHEN f.linked_key IS NULL   THEN 'Blank'
       WHEN f.found_linked IS NULL THEN 'Not found'
       WHEN f.linked_active = 1    THEN 'Yes'
       WHEN f.linked_on_login = 1  THEN 'Inactive log-in only'
       ELSE 'Electronic submission only' END                         AS `Linked STN in eSUB`,
  f.link_date                                                        AS `Link created date`,
  f.comments                                                         AS `Link comments`,
  f.link_no                                                          AS `Link record number`
FROM flagged f
LEFT JOIN rhap_tag rt ON rt.stn_key = coalesce(f.stn_key, f.linked_key)
""", rhap_tag=_RHAP_TAG, known_stns=_KNOWN_STNS, link_date=_first_plausible("CREATE_DATE", "UPDATE_DATE")),
    ),
    Check(
        id="ESUB_APPLICATION_LINK_BROKEN",
        title="Application links to missing records",
        systems=("esub",),
        entity="Submission links",
        dimension="Completeness",
        severity="Medium",
        question="Does every application link in eSUB point to an existing application and log-in record?",
        why="eSUB uses these links to group submissions under their application. A broken link drops the "
            "submission from the application's history and makes application counts wrong.",
        how_to_fix="Look up the application number or STN in eSUB. If the application or log-in record was "
                   "removed on purpose, ask eSUB support to delete the leftover link. Otherwise, ask them to "
                   "restore the record. Delete any extra copy of a repeated link.",
        record_label="links",
        filters=_ALL_FILTERS,
        date_label="Link created date",
        technical_note=(
            "Population: slvr_ctp_core.esub_application_submission rows with halo_is_deleted not true; one row per "
            "APPLICATION_SUBMISSION_KEY. APPLICATION_KEY is looked up in esub_application (non-deleted by halo, "
            "grouped per key; 'marked deleted' = every copy has FLAG_ON(DELETED_IND)) and REGLIST_KEY in "
            "esub_itrac_reglist (non-deleted by halo, grouped per key; inactive log-ins count as found but are "
            "labeled). Keys compared as NUM_KEY. Repeat = the same application and log-in pair more than once "
            "(earliest by CREATE_DATE, APPLICATION_SUBMISSION_KEY kept). record_key = 'application / STN', plus "
            "the link record number when that pair is not unique. Type/organization from the linked log-in's STN. "
            "Record dates: CREATE_DATE, UPDATE_DATE. Confirmed on real data (Oct 2026): the link's "
            "APPLICATION_KEY is an esub_application record for 99.0% of links (K44) and its REGLIST_KEY is a "
            "log-in record number for 99.2% (K45), so both lookups use the right fields and the remaining "
            "links are the real gaps this check lists." + _TAG_NOTE
        ),
        assumptions=(
            "An application marked deleted should have no linked submissions.",
            "A submission with an inactive log-in record can keep its application link. That case is shown "
            "but not counted as a problem.",
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
apps AS (
  SELECT
    NUM_KEY(APPLICATION_KEY)                                                 AS app_no,
    max(BLANK(APPLICATION_TRACKING_NUM))                                     AS app_number,
    max(BLANK(APPLICATION_STATUS))                                           AS app_status,
    min(CASE WHEN FLAG_ON(DELETED_IND) THEN 1 ELSE 0 END)                    AS all_deleted
  FROM ${core}.esub_application
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY NUM_KEY(APPLICATION_KEY)
),
logins AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    max(BLANK(STN))                                                          AS stn,
    max(STN_KEY(STN))                                                        AS stn_key,
    max(BLANK(NAME))                                                         AS company,
    min(<<received_ok>>)                                                     AS received_date,
    min(CASE WHEN FLAG_ON(INACTIVE_IND) THEN 1 ELSE 0 END)                   AS all_inactive
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY NUM_KEY(REGLIST_KEY)
),
links AS (
  SELECT
    NUM_KEY(APPLICATION_SUBMISSION_KEY)                                      AS link_no,
    NUM_KEY(APPLICATION_KEY)                                                 AS app_no,
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    CAST(coalesce(CREATE_DATE, UPDATE_DATE) AS DATE)                         AS link_date,
    <<link_date>>                                                            AS record_date,
    row_number() OVER (
      PARTITION BY NUM_KEY(APPLICATION_KEY), NUM_KEY(REGLIST_KEY)
      ORDER BY CREATE_DATE NULLS LAST, APPLICATION_SUBMISSION_KEY)           AS copy_number
  FROM ${core}.esub_application_submission
  WHERE coalesce(halo_is_deleted, false) = false
),
flagged AS (
  SELECT
    k.link_no, k.app_no, k.record_no, k.link_date, k.record_date, k.copy_number,
    a.app_number, a.app_status, l.stn, l.stn_key, l.company, l.received_date, l.all_inactive,
    (a.app_no IS NULL)                                                       AS f_app_missing,
    (l.record_no IS NULL)                                                    AS f_login_missing,
    coalesce(a.all_deleted = 1, false)                                       AS f_app_deleted,
    (k.copy_number > 1)                                                      AS f_repeat,
    concat(coalesce(a.app_number, concat('application record ', k.app_no), '(no application)'), ' / ',
           coalesce(l.stn_key, concat('eSUB record ', k.record_no), '(no log-in)'))  AS base_key
  FROM links k
  LEFT JOIN apps a   ON a.app_no = k.app_no
  LEFT JOIN logins l ON l.record_no = k.record_no
)
SELECT
  CASE WHEN count(*) OVER (PARTITION BY f.base_key) > 1
       THEN concat(f.base_key, ' (link record ', coalesce(f.link_no, '?'), ')')
       ELSE f.base_key END                                           AS record_key,
  f.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(f.f_app_missing OR f.f_login_missing OR f.f_app_deleted OR f.f_repeat, false) AS is_issue,
  CASE WHEN f.f_app_missing AND f.f_login_missing THEN 'Both records missing'
       WHEN f.f_app_missing                        THEN 'Application missing'
       WHEN f.f_login_missing                      THEN 'Log-in missing'
       WHEN f.f_app_deleted                        THEN 'Application deleted'
       WHEN f.f_repeat                             THEN 'Repeated link'
  END                                                                AS issue_category,
  CASE WHEN f.f_app_missing AND f.f_login_missing THEN
         CASE WHEN f.app_no IS NULL AND f.record_no IS NULL
                THEN 'The link names neither an application nor a log-in record.'
              WHEN f.app_no IS NULL
                THEN concat('The link names no application, and log-in record ', f.record_no, ' is not in eSUB.')
              WHEN f.record_no IS NULL
                THEN concat('The link names no log-in record, and application record ', f.app_no, ' is not in eSUB.')
              ELSE concat('The link points to application record ', f.app_no, ' and log-in record ',
                          f.record_no, '. eSUB has neither.') END
       WHEN f.f_app_missing THEN
         concat(coalesce(concat('STN ', f.stn), concat('Log-in record ', f.record_no)),
                CASE WHEN f.app_no IS NULL
                     THEN ' has an application link that names no application.'
                     ELSE concat(' is linked to application record ', f.app_no, ', which eSUB does not have.') END)
       WHEN f.f_login_missing THEN
         concat('Application ', coalesce(f.app_number, f.app_no),
                CASE WHEN f.record_no IS NULL
                     THEN ' has a submission link that names no log-in record.'
                     ELSE concat(' is linked to log-in record ', f.record_no, ', which eSUB does not have.') END)
       WHEN f.f_app_deleted THEN
         concat(coalesce(concat('STN ', f.stn), concat('Log-in record ', f.record_no)),
                ' is still linked to application ', coalesce(f.app_number, f.app_no), ', which is marked deleted.')
       WHEN f.f_repeat THEN
         concat('Extra copy of the link between application ', coalesce(f.app_number, f.app_no), ' and ',
                coalesce(concat('STN ', f.stn), concat('log-in record ', f.record_no)), '.')
  END                                                                AS issue_reason,
  f.stn                                                              AS `STN`,
  f.company                                                          AS `Company`,
  f.app_number                                                       AS `Application number`,
  f.app_status                                                       AS `Application status`,
  CASE WHEN f.f_login_missing    THEN 'Not found'
       WHEN f.all_inactive = 1   THEN 'Inactive'
       ELSE 'Active' END                                             AS `Log-in record status`,
  f.received_date                                                    AS `Received date`,
  f.link_date                                                        AS `Link created date`,
  f.link_no                                                          AS `Link record number`
FROM flagged f
LEFT JOIN rhap_tag rt ON rt.stn_key = f.stn_key
""", rhap_tag=_RHAP_TAG, received_ok=_ok("DATE_RECEIVED"),
            link_date=_first_plausible("CREATE_DATE", "UPDATE_DATE")),
    ),
    Check(
        id="ESUB_LOGIN_INACTIVE_NO_REASON",
        title="Inactive log-in records with no reason",
        systems=("esub",),
        entity="Submissions",
        dimension="Completeness",
        severity="Medium",
        question="Does every inactive log-in record in eSUB give a reason?",
        why="Marking a log-in record inactive removes the submission from counts and from review. Without a "
            "reason, a mistake looks the same as a valid withdrawal.",
        how_to_fix="Open the log-in record in eSUB and pick the inactive reason. Add a short comment such as "
                   "'duplicate of PM0000123'. If the submission should be active, ask the Document Control "
                   "Center to reactivate it.",
        record_label="inactive log-in records",
        filters=frozenset({FILTER_DATE}),
        date_label="Received date",
        technical_note=(
            "Population: slvr_ctp_core.esub_itrac_reglist rows with halo_is_deleted not true and "
            "FLAG_ON(INACTIVE_IND); one row per REGLIST_KEY. Issue when REASON_INACTIVE_CODE is NULL and "
            "BLANK(COMMENTS_INACTIVE) is NULL. Category says whether the same STN_KEY still has an active log-in "
            "(likely a duplicate clean-up) or none (the submission has left the counts). Record dates: "
            "DATE_RECEIVED, DATE_SUBMITTED, DATE_ENTERED, CREATE_DATE. Type/organization are filled for the breakdown "
            "but only the date filter is offered, because inactive log-ins are often never sent to Rhapsody."
            + _TAG_NOTE
        ),
        assumptions=(
            "REASON_INACTIVE_CODE is the coded reason. COMMENTS_INACTIVE is the free-text explanation. "
            "Either one is enough.",
            "Reasons are expected for every inactive record. If older records were inactivated before the reason "
            "field existed, narrow the time period to recent years.",
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
inactive AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    STN_KEY(STN)                                                             AS stn_key,
    BLANK(STN)                                                               AS stn,
    BLANK(NAME)                                                              AS company,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number,
    CAST(DATE_RECEIVED AS DATE)                                              AS date_received,
    CAST(coalesce(UPDATE_DATE, CREATE_DATE) AS DATE)                         AS last_changed,
    CAST(REASON_INACTIVE_CODE AS STRING)                                     AS reason_code,
    BLANK(COMMENTS_INACTIVE)                                                 AS reason_comment,
    <<login_date>>                                                           AS record_date,
    count(*) OVER (PARTITION BY STN_KEY(STN))                                AS stn_records
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND FLAG_ON(INACTIVE_IND)
),
active_stns AS (
  SELECT STN_KEY(STN) AS stn_key, count(*) AS n_active
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
    AND STN_KEY(STN) IS NOT NULL
  GROUP BY STN_KEY(STN)
)
SELECT
  CASE WHEN i.stn_key IS NULL THEN concat('(no STN) eSUB record ', coalesce(i.record_no, '?'))
       WHEN i.stn_records > 1 THEN concat(i.stn_key, ' (eSUB record ', coalesce(i.record_no, '?'), ')')
       ELSE i.stn_key END                                            AS record_key,
  i.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(i.reason_code IS NULL AND i.reason_comment IS NULL, false) AS is_issue,
  CASE WHEN i.reason_code IS NULL AND i.reason_comment IS NULL THEN
         CASE WHEN a.stn_key IS NOT NULL THEN 'Other log-in active'
              ELSE 'No active log-in' END
  END                                                                AS issue_category,
  CASE WHEN i.reason_code IS NULL AND i.reason_comment IS NULL THEN
         concat('The log-in record',
                CASE WHEN i.stn IS NOT NULL THEN concat(' for ', i.stn) ELSE '' END,
                ' is inactive with no reason or comment',
                CASE WHEN a.stn_key IS NOT NULL
                     THEN concat('. The STN still has ', CAST(a.n_active AS STRING), ' active log-in record(s)')
                     WHEN i.stn_key IS NOT NULL
                     THEN '. The STN has no other active log-in record, so submission counts miss it'
                     ELSE '' END,
                '.')
  END                                                                AS issue_reason,
  i.stn                                                              AS `STN`,
  i.company                                                          AS `Company`,
  i.date_received                                                    AS `Received date`,
  i.last_changed                                                     AS `Last changed in eSUB`,
  coalesce(a.n_active, 0)                                            AS `Active log-ins`,
  i.tracking_number                                                  AS `Tracking number`,
  i.record_no                                                        AS `eSUB record number`
FROM inactive i
LEFT JOIN active_stns a ON a.stn_key = i.stn_key
LEFT JOIN rhap_tag rt   ON rt.stn_key = i.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, login_date=_LOGIN_DATE),
    ),
    # ------------------------------------------------------------------ #
    # Consistency
    # ------------------------------------------------------------------ #
    Check(
        id="ESUB_LOGIN_DUPLICATE_STN",
        title="Duplicate STNs on eSUB log-in records",
        systems=("esub",),
        entity="Submissions",
        dimension="Consistency",
        severity="High",
        question="Is each STN on only one active eSUB log-in record?",
        why="An STN identifies exactly one submission. A second log-in counts the submission twice and can "
            "send it to review twice. The copies may also show different received dates.",
        how_to_fix="Open every log-in record for the STN in eSUB and compare each with the receipt. Keep the "
                   "correct one. Ask the Document Control Center to mark the extra copies inactive. If a "
                   "different submission got this STN by mistake, ask them to assign the right STN.",
        record_label="STNs",
        filters=_ALL_FILTERS,
        date_label="Received date",
        warn_below=0.999,
        fail_below=0.99,
        technical_note=(
            "Population: distinct STN_KEY(STN) values on active slvr_ctp_core.esub_itrac_reglist rows "
            "(halo_is_deleted not true and NOT FLAG_ON(INACTIVE_IND)) with a non-blank STN. STN_KEY makes "
            "'PM0000123' equal ' pm0000123'. One row per STN; flagged when it is on more than one record. "
            "'Same details' = all copies share one received date and one CO_NAME(NAME) (so 'Acme Inc.' equals "
            "'ACME, INC'). Each list shows at most 10 values, except companies (5). Record "
            "dates: earliest of DATE_RECEIVED, DATE_SUBMITTED, DATE_ENTERED, CREATE_DATE across the copies."
            + _TAG_NOTE
        ),
        assumptions=(
            "Each STN belongs on exactly one active log-in record. If eSUB logs extra packages or amendments "
            "under the same STN by design, narrow this check.",
            _INACTIVE_ASSUMPTION,
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
logins AS (
  SELECT
    STN_KEY(STN)                                                             AS stn_key,
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    CAST(DATE_RECEIVED AS DATE)                                              AS date_received,
    <<login_date>>                                                           AS any_date,
    BLANK(NAME)                                                              AS company,
    CO_NAME(NAME)                                                            AS company_key,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
    AND STN_KEY(STN) IS NOT NULL
),
by_stn AS (
  SELECT
    stn_key,
    count(*)                                                                 AS n_records,
    count(DISTINCT date_received)                                            AS n_dates,
    count(DISTINCT company_key)                                              AS n_companies,
    min(any_date)                                                            AS first_date,
    array_join(slice(array_sort(collect_set(record_no)), 1, 10), ', ')       AS record_numbers,
    array_join(slice(array_sort(collect_set(CAST(date_received AS STRING))), 1, 10), ', ') AS received_dates,
    array_join(slice(array_sort(collect_set(company)), 1, 5), ' / ')         AS companies,
    array_join(slice(array_sort(collect_set(tracking_number)), 1, 10), ', ') AS tracking_numbers
  FROM logins
  GROUP BY stn_key
)
SELECT
  b.stn_key                                                          AS record_key,
  b.first_date                                                       AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(b.n_records > 1, false)                                   AS is_issue,
  CASE WHEN b.n_records > 1 AND b.n_dates <= 1 AND b.n_companies <= 1 THEN 'Same details'
       WHEN b.n_records > 1                                           THEN 'Details differ'
  END                                                                AS issue_category,
  CASE WHEN b.n_records > 1 THEN
    concat('STN ', b.stn_key, ' is on ', CAST(b.n_records AS STRING), ' active log-in records',
           CASE WHEN b.n_dates > 1 OR b.n_companies > 1 THEN
                  concat(' with ', concat_ws(' and ',
                    CASE WHEN b.n_dates > 1 THEN concat('different received dates (', b.received_dates, ')') END,
                    CASE WHEN b.n_companies > 1 THEN concat('different company names (', b.companies, ')') END))
                WHEN b.received_dates <> '' THEN concat(', all received ', b.received_dates)
                ELSE '' END,
           '.')
  END                                                                AS issue_reason,
  b.stn_key                                                          AS `STN`,
  b.n_records                                                        AS `Log-in records`,
  nullif(b.companies, '')                                            AS `Company`,
  nullif(b.received_dates, '')                                       AS `Received dates`,
  nullif(b.tracking_numbers, '')                                     AS `Tracking numbers`,
  b.record_numbers                                                   AS `eSUB record numbers`
FROM by_stn b
LEFT JOIN rhap_tag rt ON rt.stn_key = b.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, login_date=_LOGIN_DATE),
    ),
    Check(
        id="ESUB_LOGIN_STN_FORMAT",
        title="STN format errors in eSUB",
        systems=("esub",),
        entity="Submissions",
        dimension="Consistency",
        severity="Medium",
        question="Is every STN in eSUB in capital letters with no spaces, following the usual pattern?",
        why="Other systems and searches match submissions on the exact STN. One stray space or wrong character "
            "hides the submission from search and breaks its match with Rhapsody and Reporting.",
        how_to_fix="Open the log-in record in eSUB and retype the STN exactly as the receipt shows it. Where the "
                   "fix is obvious, the suggested STN is shown. For an unusual pattern, confirm the STN with the "
                   "Document Control Center before you change it.",
        record_label="log-in records",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: active slvr_ctp_core.esub_itrac_reglist rows with STN_KEY(STN) not NULL; one row per "
            "REGLIST_KEY. Tests in order: any whitespace incl. non-breaking/Unicode spaces (rlike '[\\s\\p{Z}]', "
            "the characters STN_KEY removes); lowercase letters; characters other than A-Z, 0-9, period or dash "
            "in STN_KEY(STN); unusual pattern. Pattern = STN_KEY(STN) with every letter replaced by 'A' and every "
            "digit by '9' (e.g. PM0000123 -> AA9999999). A pattern is unusual when fewer than 1 in 1,000 active "
            "log-in records share it (so it never fires below 1,000 records). No STN prefix list is hard-coded; "
            "the usual pattern and an example are computed from the data. A window over the grouped patterns is "
            "equi-joined back, so rows do not multiply. Record dates: DATE_RECEIVED, DATE_SUBMITTED, DATE_ENTERED, "
            "CREATE_DATE." + _TAG_NOTE
        ),
        assumptions=(
            "A correct STN has no spaces. It uses capital letters and digits. Periods and dashes are also allowed.",
            "An STN pattern shared by fewer than 1 in 1,000 log-in records is most likely a typo, such as a "
            "missing digit. A rare pattern that is valid will show here until someone adjusts the threshold.",
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
logins AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    STN                                                                      AS stn_raw,
    STN_KEY(STN)                                                             AS stn_key,
    BLANK(NAME)                                                              AS company,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number,
    CAST(DATE_RECEIVED AS DATE)                                              AS date_received,
    <<login_date>>                                                           AS record_date,
    count(*) OVER (PARTITION BY STN_KEY(STN))                                AS stn_records
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
    AND STN_KEY(STN) IS NOT NULL
),
shaped AS (
  SELECT
    l.*,
    regexp_replace(regexp_replace(l.stn_key, '[A-Z]', 'A'), '[0-9]', '9')    AS pattern
  FROM logins l
),
patterns AS (
  SELECT pattern, count(*) AS n_pattern, min(stn_key) AS example_stn
  FROM shaped
  GROUP BY pattern
),
pattern_stats AS (
  SELECT
    pattern,
    n_pattern,
    sum(n_pattern) OVER ()                                                   AS n_total,
    first_value(example_stn) OVER (ORDER BY n_pattern DESC, pattern)         AS usual_example,
    first_value(length(pattern)) OVER (ORDER BY n_pattern DESC, pattern)     AS usual_length
  FROM patterns
),
flagged AS (
  SELECT
    s.record_no, s.stn_raw, s.stn_key, s.company, s.tracking_number, s.date_received, s.record_date,
    s.stn_records, p.n_pattern, p.n_total, p.usual_example, p.usual_length,
    coalesce(s.stn_raw rlike '[\\s\\p{Z}]', false)                           AS has_spaces,
    coalesce(s.stn_raw rlike '[a-z]', false)                                 AS has_lowercase,
    coalesce(s.stn_key rlike '[^A-Z0-9.-]', false)                           AS has_odd_chars,
    coalesce(p.n_pattern * 1000 < p.n_total, false)                          AS rare_pattern
  FROM shaped s
  JOIN pattern_stats p ON p.pattern = s.pattern
)
SELECT
  CASE WHEN f.stn_records > 1 THEN concat(f.stn_key, ' (eSUB record ', coalesce(f.record_no, '?'), ')')
       ELSE f.stn_key END                                            AS record_key,
  f.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  (f.has_spaces OR f.has_lowercase OR f.has_odd_chars OR f.rare_pattern) AS is_issue,
  CASE WHEN f.has_spaces    THEN 'Spaces in STN'
       WHEN f.has_lowercase THEN 'Lowercase letters'
       WHEN f.has_odd_chars THEN 'Unexpected characters'
       WHEN f.rare_pattern  THEN 'Unusual pattern'
  END                                                                AS issue_category,
  CASE WHEN f.has_spaces THEN
         concat('STN "', f.stn_raw, '" has spaces. It should probably be ', f.stn_key, '.')
       WHEN f.has_lowercase THEN
         concat('STN "', f.stn_raw, '" has lowercase letters. It should probably be ', f.stn_key, '.')
       WHEN f.has_odd_chars THEN
         concat('STN "', f.stn_raw, '" has characters other than capital letters, digits, periods or dashes.')
       WHEN f.rare_pattern THEN
         concat('STN "', f.stn_raw, '" has an unusual pattern',
                CASE WHEN length(f.stn_key) <> f.usual_length
                     THEN concat(' (', CAST(length(f.stn_key) AS STRING), ' characters, most have ',
                                 CAST(f.usual_length AS STRING), ')')
                     ELSE '' END,
                '. Only ', CAST(f.n_pattern AS STRING), ' of ', CAST(f.n_total AS STRING),
                ' log-in records share it. Most look like ', f.usual_example, '.')
  END                                                                AS issue_reason,
  concat('"', f.stn_raw, '"')                                        AS `STN as entered`,
  CASE WHEN (f.has_spaces OR f.has_lowercase) AND NOT f.has_odd_chars AND NOT f.rare_pattern
       THEN f.stn_key END                                            AS `Suggested STN`,
  f.usual_example                                                    AS `Usual STN example`,
  f.company                                                          AS `Company`,
  f.date_received                                                    AS `Received date`,
  f.tracking_number                                                  AS `Tracking number`,
  f.record_no                                                        AS `eSUB record number`
FROM flagged f
LEFT JOIN rhap_tag rt ON rt.stn_key = f.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, login_date=_LOGIN_DATE),
    ),
    Check(
        id="ESUB_LOGIN_DOC_TYPE_NOT_IN_LIST",
        title="Unknown or retired document types",
        systems=("esub",),
        entity="Submissions",
        dimension="Consistency",
        severity="Medium",
        question="Does every log-in record use a current document type from eSUB's list?",
        why="The document type says what kind of submission it is. Counts by type depend on it. An unknown or "
            "retired type drops the submission from those counts or puts it in the wrong group.",
        how_to_fix="Open the log-in record in eSUB. Use the cover letter to pick the correct document type from "
                   "the list. If the right type is missing from the list, ask eSUB support to add it.",
        record_label="log-in records",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: active slvr_ctp_core.esub_itrac_reglist rows with NUM_KEY(DOC_TYPES_KEY) not NULL (blank "
            "types are counted by ESUB_LOGIN_REQUIRED_FIELDS_BLANK); one row per REGLIST_KEY. Types come from "
            "esub_itrac_doc_types (halo_is_deleted not true), grouped to one row per NUM_KEY (no duplicate rows). "
            "'Unknown type' = no matching key. 'Retired type' = every copy of the type has ACTIVE_IND N/NO/0/F/FALSE (blank "
            "counts as active) and the log-in record's earliest plausible date (least of DATE_RECEIVED, "
            "DATE_ENTERED, CREATE_DATE) is after the type's latest UPDATE_DATE, taken as the latest possible "
            "retirement date; records logged in before that, or types with no UPDATE_DATE, are not flagged. "
            "Record dates: DATE_RECEIVED, DATE_SUBMITTED, DATE_ENTERED, CREATE_DATE. Confirmed on real data "
            "(Oct 2026): DOC_TYPES_KEY on log-in records is the type list's key (K46: all 28 type numbers in use "
            "are on the list), so 'Unknown type' should stay rare." + _TAG_NOTE
        ),
        assumptions=(
            "A document type marked not active in eSUB's list is retired. New log-ins should not use it.",
            "A retired type's last-changed date falls on or after its retirement date. Only records logged in "
            "after that date are flagged.",
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
logins AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    STN_KEY(STN)                                                             AS stn_key,
    BLANK(STN)                                                               AS stn,
    BLANK(NAME)                                                              AS company,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number,
    NUM_KEY(DOC_TYPES_KEY)                                                   AS doc_type_code,
    least(<<received_ok>>, <<entered_ok>>, <<created_ok>>)                   AS first_logged_date,
    CAST(DATE_RECEIVED AS DATE)                                              AS date_received,
    <<login_date>>                                                           AS record_date
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
    AND NUM_KEY(DOC_TYPES_KEY) IS NOT NULL
),
doc_types AS (
  SELECT
    NUM_KEY(DOC_TYPES_KEY)                                                   AS doc_type_code,
    max(BLANK(DOC_TYPE))                                                     AS doc_type_name,
    max(CASE WHEN <<active_off>> THEN 0 ELSE 1 END)                          AS is_active,
    CAST(max(UPDATE_DATE) AS DATE)                                           AS last_changed
  FROM ${core}.esub_itrac_doc_types
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY NUM_KEY(DOC_TYPES_KEY)
),
flagged AS (
  SELECT
    l.record_no, l.stn_key, l.stn, l.company, l.tracking_number, l.doc_type_code,
    l.first_logged_date, l.date_received, l.record_date, d.doc_type_name, d.is_active, d.last_changed,
    (d.doc_type_code IS NULL)                                                AS not_in_list,
    coalesce(d.is_active = 0 AND l.first_logged_date > d.last_changed, false) AS retired_when_used,
    count(*) OVER (PARTITION BY l.stn_key)                                   AS stn_records
  FROM logins l
  LEFT JOIN doc_types d ON d.doc_type_code = l.doc_type_code
)
SELECT
  CASE WHEN f.stn_key IS NULL THEN concat('(no STN) eSUB record ', coalesce(f.record_no, '?'))
       WHEN f.stn_records > 1 THEN concat(f.stn_key, ' (eSUB record ', coalesce(f.record_no, '?'), ')')
       ELSE f.stn_key END                                            AS record_key,
  f.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  (f.not_in_list OR f.retired_when_used)                             AS is_issue,
  CASE WHEN f.not_in_list       THEN 'Unknown type'
       WHEN f.retired_when_used THEN 'Retired type'
  END                                                                AS issue_category,
  CASE WHEN f.not_in_list THEN
         concat('Document type number ', f.doc_type_code, ' is not on the eSUB list.')
       WHEN f.retired_when_used THEN
         concat('Uses document type "', coalesce(f.doc_type_name, f.doc_type_code), '", retired on or before ',
                CAST(f.last_changed AS STRING), '. This record was logged in later, on ',
                CAST(f.first_logged_date AS STRING), '.')
  END                                                                AS issue_reason,
  f.stn                                                              AS `STN`,
  f.company                                                          AS `Company`,
  f.doc_type_code                                                    AS `Document type number`,
  coalesce(f.doc_type_name, '(not on the list)')                     AS `Document type name`,
  CASE WHEN f.not_in_list THEN NULL WHEN f.is_active = 1 THEN 'Yes' ELSE 'No' END AS `Type still active`,
  f.first_logged_date                                                AS `Logged-in date`,
  f.date_received                                                    AS `Received date`,
  f.tracking_number                                                  AS `Tracking number`,
  f.record_no                                                        AS `eSUB record number`
FROM flagged f
LEFT JOIN rhap_tag rt ON rt.stn_key = f.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, login_date=_LOGIN_DATE, received_ok=_ok("DATE_RECEIVED"),
            entered_ok=_ok("DATE_ENTERED"), created_ok=_ok("CREATE_DATE"),
            active_off=_FLAG_OFF.replace("{x}", "ACTIVE_IND")),
    ),
    # ------------------------------------------------------------------ #
    # Timeliness
    # ------------------------------------------------------------------ #
    Check(
        id="ESUB_LOGIN_DATES_IMPOSSIBLE",
        title="Impossible dates on log-in records",
        systems=("esub",),
        entity="Submissions",
        dimension="Timeliness",
        severity="High",
        question="Are the dates on every log-in record possible and in the right order?",
        why="The received date starts the review clock and sets deadlines. A future date or dates out of order "
            "almost always mean a typo. That typo makes review times and deadline reports wrong.",
        how_to_fix="Open the log-in record in eSUB. Compare each date with the cover letter and the receipt. The "
                   "courier or gateway receipt shows the true received date. Correct the wrong date. The usual "
                   f"cause is a wrong year or a swapped day and month. A received date may fall up to "
                   f"{_RECEIVED_AFTER_LOGIN_DAYS} days after the log-in date. So a received date after today is "
                   f"flagged only when it is also more than {_RECEIVED_AFTER_LOGIN_DAYS} days after the log-in "
                   "date.",
        record_label="log-in records",
        filters=_ALL_FILTERS,
        date_label="Date added to eSUB",
        technical_note=(
            "Population: active slvr_ctp_core.esub_itrac_reglist rows; one row per REGLIST_KEY. Dates compared as "
            "DATE. Tests: DATE_RECEIVED in the future = later than current_date() AND more than "
            f"{_RECEIVED_AFTER_LOGIN_DAYS} days after the logged-in date (DATE_ENTERED, else CREATE_DATE; "
            "current_date() stands in when both are blank). This is the same "
            f"{_RECEIVED_AFTER_LOGIN_DAYS}-day allowance (_RECEIVED_AFTER_LOGIN_DAYS) the 'logged in before "
            "received' test grants, so a received date the module's own rule allows is never called future. "
            "DATE_SUBMITTED or logged-in date later than current_date() + 1 day (time-zone tolerance, not yet "
            "confirmed for these two dates). Confirmed on real data (Oct 2026): eSUB stores DATE_RECEIVED at "
            "midnight (P3.2: 90% of log-ins at exactly 00:00) and, for the 43,906 STNs in both systems, it is the "
            "same calendar date as Rhapsody's received date 100% of the time (P3.1; a UTC shift would put 86% "
            "one day apart), so received dates carry no time-zone shift and get no extra tolerance. The today "
            "test assumes the warehouse session time zone is not behind US Eastern (the Databricks default is "
            "UTC). Other tests: DATE_RECEIVED or DATE_SUBMITTED before "
            "2009-06-22 (Tobacco Control Act); received more than 1 day before submitted; logged in more than "
            f"{_RECEIVED_AFTER_LOGIN_DAYS} days before received (allows the next-business-day receipt rule across "
            "weekends and holidays). The "
            "two order tests only run between dates that are not already flagged as future/too early, so one bad "
            "date is reported once. All flags coalesced to false when a date is blank (blank dates are counted by "
            "ESUB_LOGIN_REQUIRED_FIELDS_BLANK). Record dates: CREATE_DATE, DATE_ENTERED (system-stamped first), so "
            "a record with a mistyped received date still falls inside the steward's date range." + _TAG_NOTE
        ),
        assumptions=(
            "DATE_ENTERED is the log-in date. When it is blank, the record's creation date is used instead.",
            "No CTP submission can predate June 22, 2009, when FDA began regulating tobacco products.",
            f"A received date can fall up to {_RECEIVED_AFTER_LOGIN_DAYS} days after the log-in date. "
            "Submissions that arrive outside business hours count as received the next business day. A received "
            f"date after today is flagged only when it is also more than {_RECEIVED_AFTER_LOGIN_DAYS} days after "
            "the log-in date.",
            _INACTIVE_ASSUMPTION,
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
logins AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    STN_KEY(STN)                                                             AS stn_key,
    BLANK(STN)                                                               AS stn,
    BLANK(NAME)                                                              AS company,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number,
    CAST(DATE_SUBMITTED AS DATE)                                             AS d_submitted,
    CAST(DATE_RECEIVED AS DATE)                                              AS d_received,
    CAST(coalesce(DATE_ENTERED, CREATE_DATE) AS DATE)                        AS d_logged_in,
    <<added_date>>                                                           AS d_added,
    count(*) OVER (PARTITION BY STN_KEY(STN))                                AS stn_records
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
),
range_checked AS (
  SELECT
    l.*,
    -- In the future only when after today AND beyond the allowance after the log-in date (today if blank).
    coalesce(l.d_received > current_date()
             AND datediff(l.d_received, coalesce(l.d_logged_in, current_date())) > <<allow>>, false)
                                                                             AS received_future,
    coalesce(l.d_submitted > date_add(current_date(), 1), false)             AS submitted_future,
    coalesce(l.d_logged_in > date_add(current_date(), 1), false)             AS logged_in_future,
    coalesce(l.d_received  < DATE'2009-06-22', false)                        AS received_too_early,
    coalesce(l.d_submitted < DATE'2009-06-22', false)                        AS submitted_too_early
  FROM logins l
),
flagged AS (
  SELECT
    r.*,
    coalesce(NOT r.received_future AND NOT r.received_too_early
             AND NOT r.submitted_future AND NOT r.submitted_too_early
             AND datediff(r.d_received, r.d_submitted) < -1, false)          AS received_before_submitted,
    coalesce(NOT r.received_future AND NOT r.received_too_early AND NOT r.logged_in_future
             AND datediff(r.d_logged_in, r.d_received) < -<<allow>>, false)  AS logged_in_before_received
  FROM range_checked r
),
described AS (
  SELECT
    f.*,
    concat_ws('; ',
      CASE WHEN f.received_future THEN
        concat('received date ', CAST(f.d_received AS STRING), ' is in the future',
               CASE WHEN f.d_logged_in IS NOT NULL
                    THEN concat(', ', CAST(datediff(f.d_received, f.d_logged_in) AS STRING),
                                ' days after the logged-in date (', CAST(f.d_logged_in AS STRING),
                                '), beyond the <<allow>> days allowed')
                    ELSE ', more than <<allow>> days from today, and the logged-in date is blank' END) END,
      CASE WHEN f.submitted_future THEN concat('submitted date ', CAST(f.d_submitted AS STRING), ' is in the future') END,
      CASE WHEN f.logged_in_future THEN concat('logged-in date ', CAST(f.d_logged_in AS STRING), ' is in the future') END,
      CASE WHEN f.received_too_early THEN concat('received date ', CAST(f.d_received AS STRING),
                                                 ' is before FDA began regulating tobacco (June 2009)') END,
      CASE WHEN f.submitted_too_early THEN concat('submitted date ', CAST(f.d_submitted AS STRING),
                                                  ' is before FDA began regulating tobacco (June 2009)') END,
      CASE WHEN f.received_before_submitted THEN
        concat('received ', CAST(f.d_received AS STRING), ', ',
               CAST(datediff(f.d_submitted, f.d_received) AS STRING),
               ' days before it was submitted (', CAST(f.d_submitted AS STRING), ')') END,
      CASE WHEN f.logged_in_before_received THEN
        concat('logged in ', CAST(f.d_logged_in AS STRING), ', ',
               CAST(datediff(f.d_received, f.d_logged_in) AS STRING),
               ' days before the received date (', CAST(f.d_received AS STRING), ')') END
    )                                                                        AS problems
  FROM flagged f
)
SELECT
  CASE WHEN d.stn_key IS NULL THEN concat('(no STN) eSUB record ', coalesce(d.record_no, '?'))
       WHEN d.stn_records > 1 THEN concat(d.stn_key, ' (eSUB record ', coalesce(d.record_no, '?'), ')')
       ELSE d.stn_key END                                            AS record_key,
  d.d_added                                                          AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  coalesce(d.problems <> '', false)                                  AS is_issue,
  CASE WHEN d.received_future OR d.submitted_future OR d.logged_in_future THEN 'Future date'
       WHEN d.received_too_early OR d.submitted_too_early                 THEN 'Before June 2009'
       WHEN d.received_before_submitted                                   THEN 'Received before submitted'
       WHEN d.logged_in_before_received                                   THEN 'Logged before received'
  END                                                                AS issue_category,
  CASE WHEN d.problems <> '' THEN concat(upper(substr(d.problems, 1, 1)), substr(d.problems, 2), '.') END AS issue_reason,
  d.stn                                                              AS `STN`,
  d.company                                                          AS `Company`,
  d.d_submitted                                                      AS `Submitted date`,
  d.d_received                                                       AS `Received date`,
  d.d_logged_in                                                      AS `Logged-in date`,
  d.d_added                                                          AS `Added to eSUB`,
  d.tracking_number                                                  AS `Tracking number`,
  d.record_no                                                        AS `eSUB record number`
FROM described d
LEFT JOIN rhap_tag rt ON rt.stn_key = d.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, added_date=_first_plausible("CREATE_DATE", "DATE_ENTERED"),
            allow=str(_RECEIVED_AFTER_LOGIN_DAYS)),
    ),
    Check(
        id="ESUB_LOGIN_LONG_DELAYS",
        title="Long receipt or log-in delays",
        systems=("esub",),
        entity="Submissions",
        dimension="Timeliness",
        severity="Medium",
        question="Was every submission received within 30 days of being sent, and logged in within 14 days "
                 "after that?",
        why="A long gap usually means a mistyped date (often the year or month), which shifts the review clock. "
            "If the dates are right, the submission sat unprocessed. That delays review and puts deadlines "
            "at risk.",
        how_to_fix="Compare the dates on the eSUB log-in record with the receipt and the cover letter. If a date "
                   "is wrong, correct it. If the dates are right, report the delay to the Document Control "
                   "Center.",
        record_label="log-in records",
        filters=_ALL_FILTERS,
        date_label="Received date",
        technical_note=(
            "Population: active slvr_ctp_core.esub_itrac_reglist rows whose DATE_RECEIVED is between 2009-06-22 and "
            "current_date(), with no tolerance. Confirmed on real data (Oct 2026): eSUB received dates carry no "
            "time-zone shift (P3.1, P3.2), so the former +1 day tolerance was removed. A received date after "
            "today is either flagged by ESUB_LOGIN_DATES_IMPOSSIBLE (more than "
            + str(_RECEIVED_AFTER_LOGIN_DAYS) + " days after the log-in date) or allowed by its rule, and is "
            "evaluated here once that date has passed. Rows must also have "
            "DATE_SUBMITTED or a logged-in date (DATE_ENTERED, else CREATE_DATE); "
            "one row per REGLIST_KEY. Gaps in days with datediff on DATE values. Flagged when received - submitted "
            "> 30 days (submitted date between 2009-06-22 and current_date() + 1), or logged in - received > 14 "
            "days (logged-in date not after current_date() + 1). Negative gaps and out-of-range dates are left to "
            "ESUB_LOGIN_DATES_IMPOSSIBLE so a typo is not double-counted. Record dates: DATE_RECEIVED." + _TAG_NOTE
        ),
        assumptions=(
            "More than 30 days between the company's submitted date and FDA's received date is unusual enough "
            "to review.",
            "Submissions are normally logged in within 14 days of receipt.",
            "DATE_ENTERED is the log-in date. When it is blank, the record's creation date is used instead. "
            "Records copied into eSUB from an older system may show a late log-in date.",
            _INACTIVE_ASSUMPTION,
            _TAG_ASSUMPTION,
        ),
        sql=_q(r"""
WITH <<rhap_tag>>,
logins AS (
  SELECT
    NUM_KEY(REGLIST_KEY)                                                     AS record_no,
    STN_KEY(STN)                                                             AS stn_key,
    BLANK(STN)                                                               AS stn,
    BLANK(NAME)                                                              AS company,
    BLANK(TRACKING_NUMBER)                                                   AS tracking_number,
    CAST(DATE_SUBMITTED AS DATE)                                             AS d_submitted,
    CAST(DATE_RECEIVED AS DATE)                                              AS d_received,
    CAST(coalesce(DATE_ENTERED, CREATE_DATE) AS DATE)                        AS d_logged_in,
    <<received_date>>                                                        AS record_date,
    count(*) OVER (PARTITION BY STN_KEY(STN))                                AS stn_records
  FROM ${core}.esub_itrac_reglist
  WHERE <<active>>
    AND CAST(DATE_RECEIVED AS DATE) BETWEEN DATE'2009-06-22' AND current_date()
    AND (DATE_SUBMITTED IS NOT NULL OR DATE_ENTERED IS NOT NULL OR CREATE_DATE IS NOT NULL)
),
gaps AS (
  SELECT
    l.*,
    datediff(l.d_received, l.d_submitted)                                    AS days_in_transit,
    datediff(l.d_logged_in, l.d_received)                                    AS days_to_log_in
  FROM logins l
),
flagged AS (
  SELECT
    g.*,
    coalesce(g.d_submitted BETWEEN DATE'2009-06-22' AND date_add(current_date(), 1)
             AND g.days_in_transit > 30, false)                              AS slow_arrival,
    coalesce(g.d_logged_in <= date_add(current_date(), 1)
             AND g.days_to_log_in > 14, false)                               AS slow_log_in
  FROM gaps g
)
SELECT
  CASE WHEN f.stn_key IS NULL THEN concat('(no STN) eSUB record ', coalesce(f.record_no, '?'))
       WHEN f.stn_records > 1 THEN concat(f.stn_key, ' (eSUB record ', coalesce(f.record_no, '?'), ')')
       ELSE f.stn_key END                                            AS record_key,
  f.record_date                                                      AS record_date,
  rt.r_type                                                          AS submission_type,
  rt.r_org                                                           AS office,
  (f.slow_arrival OR f.slow_log_in)                                  AS is_issue,
  CASE WHEN f.slow_arrival AND f.slow_log_in THEN 'Both slow'
       WHEN f.slow_arrival                   THEN 'Slow arrival'
       WHEN f.slow_log_in                    THEN 'Slow log-in'
  END                                                                AS issue_category,
  CASE WHEN f.slow_arrival OR f.slow_log_in THEN
    concat(
      concat_ws('; ',
        CASE WHEN f.slow_arrival THEN concat('Received ', CAST(f.d_received AS STRING), ', ',
                                             CAST(f.days_in_transit AS STRING), ' days after it was submitted (',
                                             CAST(f.d_submitted AS STRING), ')') END,
        CASE WHEN f.slow_log_in THEN concat(CASE WHEN f.slow_arrival THEN 'logged in ' ELSE 'Logged in ' END,
                                            CAST(f.d_logged_in AS STRING), ', ', CAST(f.days_to_log_in AS STRING),
                                            ' days after it was received (', CAST(f.d_received AS STRING), ')') END),
      '.')
  END                                                                AS issue_reason,
  f.stn                                                              AS `STN`,
  f.company                                                          AS `Company`,
  f.d_submitted                                                      AS `Submitted date`,
  f.d_received                                                       AS `Received date`,
  f.d_logged_in                                                      AS `Logged-in date`,
  f.days_in_transit                                                  AS `Arrival delay (days)`,
  f.days_to_log_in                                                   AS `Log-in delay (days)`,
  f.tracking_number                                                  AS `Tracking number`
FROM flagged f
LEFT JOIN rhap_tag rt ON rt.stn_key = f.stn_key
""", rhap_tag=_RHAP_TAG, active=_ACTIVE_LOGIN, received_date=_first_plausible("DATE_RECEIVED")),
    ),
]
