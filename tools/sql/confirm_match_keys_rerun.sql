-- =====================================================================================================
-- CTP Data Quality app: RE-RUN of the sections missing from the first run of confirm_match_keys.sql
-- =====================================================================================================
-- Sections: P1.1-P1.6 (which IDs link the systems), P2.01-P2.06 (eSUB inactive / deleted / hold flags,
-- status and document type), P2.30. Same statements as the full script, unchanged.
-- Run all, then download each result tab as CSV into the 'sql outputs' folder. Any file name works:
-- the app team identifies each result by its 'section' column. Part 1 reads large tables and can take
-- several minutes per statement; if one fails, note the error message and carry on.
-- =====================================================================================================

-- =====================================================================================================
-- CTP Data Quality app: confirm how the systems link together, and what the status codes mean
-- =====================================================================================================
--
-- WHY THIS EXISTS
--   The Data Quality app compares the same submissions, products, bundles and letters across
--   eSUB, Rhapsody, PMS and Reporting. To do that it has to know which ID in one system matches
--   which ID in another, and what status codes such as "Closed" or "Y" mean. The field
--   descriptions in the lakehouse are blank, and the app team cannot read the real data, so those
--   links and meanings are educated guesses. This script tests each guess against the real data.
--
--   It only READS data. It changes nothing (no tables, views or functions are created).
--
-- HOW TO RUN
--   1. Open a new query in the Databricks SQL editor and paste this whole file.
--   2. Pick a SQL warehouse and click "Run all". Every statement below gives its own result tab.
--      (If you run one statement at a time, run the USE CATALOG line first, or pick
--      ctp_pd_catalog in the catalog drop-down at the top of the editor.)
--   3. Part 1 and Part 4 read many large tables and can take several minutes each.
--   4. If a statement fails (for example no permission on one table), write down the error message
--      and carry on with the next one - every statement stands on its own.
--   5. If you know which SQL warehouse the Data Quality app uses, use that one: the time zone
--      check in Part 3 depends on the warehouse time zone.
--
-- WHAT TO SEND BACK
--   Download each result as CSV (or paste it into one spreadsheet, one tab per result) and name it with
--   the "section" value shown in its first column:
--     P1.1 ... P1.6     which IDs link the systems          (6 results)
--     P2.01 ... P2.33   what the status, flag and code values mean (33 results)
--     P3.1, P3.2        received-date time zone check       (2 results)
--     P4                is each ID really unique?           (1 result)
--   Most useful of all: in Part 2, confirm or correct the "app_currently_reads_as" column, row by row.
--   Results contain counts, codes, list names, short STN examples and STN "shapes". They contain no
--   company names, people or contact details. Two results show short reasons or system messages that
--   staff or systems typed (P2.15 rejection reasons, P2.22 letter sending errors). E-mail addresses in
--   them are masked, but please glance through those two and delete any row that names a person
--   before sending.
--
-- HOW VALUES ARE COMPARED (exactly the same rules the app uses - dq/sqlfrag.py)
--   * STNs ("STN rule", app macro STN_KEY): upper-cased with ALL spaces removed, including spaces in
--     the middle and non-breaking spaces:
--        nullif(upper(regexp_replace(CAST(x AS STRING), '[\\s\\p{Z}]+', '')), '')
--   * Number IDs (eSUB numbers are stored as decimals, e.g. 123.000000 - app macro NUM_KEY): whole-number
--     text, so 123.000000 matches 123:            CAST(try_cast(x AS BIGINT) AS STRING)
--   * Text IDs that may hold a number (Rhapsody product and bundle IDs, PMS codes - app macro ANY_KEY):
--     the whole number when the value reads as a number ('00123', '123.0' and 123 all match), otherwise
--     the text upper-cased with outer spaces removed:
--        coalesce(CAST(try_cast(try_cast(trim(CAST(x AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
--                 nullif(upper(trim(CAST(x AS STRING))), ''))
--   * Free text (app macro TXT): upper-cased, outer spaces removed, inner runs of spaces made single.
--   * Yes/no text flags such as eSUB DELETED_IND / INACTIVE_IND (app macro FLAG_ON): "set" when the value
--     is Y, YES, 1, T or TRUE in any case - anything else (including blank) is "not set".
--   * Blank text never counts as a match. Nothing here can fail on a badly typed value: every
--     conversion uses try_cast, which gives "no value" instead of an error.
--   * Records removed from the lakehouse copy (halo_is_deleted) are left out everywhere. Reporting rows
--     must also be the current version (halo_is_current). Rhapsody rows marked deleted, and Rhapsody
--     temporary working copies (s_is_temporary_copy), are left out where those flags exist. eSUB rows
--     whose DELETED_IND / INACTIVE_IND is set are treated as deleted / inactive (each row says whether
--     inactive eSUB records are included).
-- =====================================================================================================

USE CATALOG ctp_pd_catalog;


-- =====================================================================================================
-- PART 1: Which IDs really link the systems?  (six results, P1.1 to P1.6)
-- =====================================================================================================
-- One row per candidate link. Rows are numbered K01, K02, ... - the numbers stay the same between versions
-- of this script (rows K01-K37 are from the first version, K38 onward were added), so they are not in order
-- inside each result.
--
-- How to read a row:
--   left_side / right_side   the two IDs being compared (system, business name, [table.field]).
--   left_distinct            how many different non-blank values the left side has.
--   right_distinct           the same for the right side.
--   matched_distinct         how many of those values appear on BOTH sides.
--   pct_left_matched         % of left-side values found on the right side.
--   pct_right_matched        % of right-side values found on the left side.
--   notes                    what the app assumes, and what a high or low number would tell us.
--
--   Near 100%   the two fields really are the same ID (the link works).
--   Near 0%     they are NOT the same ID - the app should stop using this link.
--   In between  a real link with gaps - those gaps are exactly what the app will report to stewards.
--   Where several rows test different candidates for the same link (for example K22-K28 and K49-K51 for
--   products), normally only one of them will be high. That one is the real link.
--
-- Each value is counted once per side (sets of distinct values are compared), so duplicates inside a
-- system cannot inflate the numbers. Part 4 checks the duplicates separately.
-- =====================================================================================================

-- -----------------------------------------------------------------------------------------------------
-- P1.1  Inside eSUB: log-in records, electronic submissions, files, links, applications, document types
-- -----------------------------------------------------------------------------------------------------
WITH
login AS (
  SELECT
    CAST(try_cast(REGLIST_KEY AS BIGINT) AS STRING)                                          AS login_id,
    nullif(upper(regexp_replace(CAST(STN AS STRING), '[\\s\\p{Z}]+', '')), '')               AS stn,
    nullif(upper(regexp_replace(CAST(STN_LINK AS STRING), '[\\s\\p{Z}]+', '')), '')          AS stn_link,
    nullif(upper(regexp_replace(CAST(TRACKING_NUMBER AS STRING), '[\\s\\p{Z}]+', '')), '')   AS tracking_no,
    CAST(try_cast(PACKAGE_KEY AS BIGINT) AS STRING)                                          AS package_id,
    CAST(try_cast(DOC_TYPES_KEY AS BIGINT) AS STRING)                                        AS doc_type_id,
    coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false) AS is_inactive
  FROM slvr_ctp_core.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
),
esub AS (
  SELECT
    CAST(try_cast(SUBMISSION_KEY AS BIGINT) AS STRING)                                       AS esub_id,
    nullif(upper(regexp_replace(CAST(SUBMISSION_TRACKING_NUMBER AS STRING), '[\\s\\p{Z}]+', '')), '') AS stn,
    CAST(try_cast(PACKAGE_KEY AS BIGINT) AS STRING)                                          AS package_id,
    coalesce(upper(trim(CAST(DELETED_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)  AS is_deleted,
    coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false) AS is_inactive,
    coalesce(upper(trim(CAST(HOLD_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)     AS is_on_hold
  FROM slvr_ctp_core.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
),
amended AS (
  -- STN_BEING_AMENDED split exactly as the app does: upper-cased, a letter and a digit separated only by
  -- spaces are joined, then split on anything that is not a letter or digit - parts of 5+ characters that
  -- contain a letter and a digit are taken as STNs ('PM0000123.A1, PM0000456' -> PM0000123, PM0000456).
  SELECT DISTINCT nullif(upper(regexp_replace(CAST(part AS STRING), '[\\s\\p{Z}]+', '')), '') AS stn
  FROM (
    SELECT explode(split(regexp_replace(upper(STN_BEING_AMENDED), '([A-Z])[\\s\\p{Z}]+([0-9])', '$1$2'),
                         '[^A-Z0-9]+')) AS part
    FROM slvr_ctp_core.esub_tob_submission
    WHERE coalesce(halo_is_deleted, false) = false
      AND NOT coalesce(upper(trim(CAST(DELETED_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)
      AND NOT coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)
      AND nullif(trim(STN_BEING_AMENDED), '') IS NOT NULL
  ) AS p
  WHERE length(part) >= 5 AND part RLIKE '[A-Z]' AND part RLIKE '[0-9]'
),
sub_link AS (
  SELECT
    nullif(upper(regexp_replace(CAST(STN AS STRING), '[\\s\\p{Z}]+', '')), '')               AS parent_stn,
    nullif(upper(regexp_replace(CAST(CHILD_STN AS STRING), '[\\s\\p{Z}]+', '')), '')         AS child_stn
  FROM slvr_ctp_core.esub_itrac_sub_links
  WHERE coalesce(halo_is_deleted, false) = false
),
sub_doc AS (
  SELECT
    CAST(try_cast(SUBMISSION_KEY AS BIGINT) AS STRING)                                       AS esub_id,
    CAST(try_cast(DOCUMENT_KEY AS BIGINT) AS STRING)                                         AS doc_id
  FROM slvr_ctp_core.esub_submission_document
  WHERE coalesce(halo_is_deleted, false) = false
),
doc AS (
  SELECT
    CAST(try_cast(DOCUMENT_KEY AS BIGINT) AS STRING)                                         AS doc_id,
    CAST(try_cast(PACKAGE_KEY AS BIGINT) AS STRING)                                          AS package_id
  FROM slvr_ctp_core.esub_document
  WHERE coalesce(halo_is_deleted, false) = false
),
app_link AS (
  SELECT
    CAST(try_cast(APPLICATION_KEY AS BIGINT) AS STRING)                                      AS app_id,
    CAST(try_cast(REGLIST_KEY AS BIGINT) AS STRING)                                          AS login_id
  FROM slvr_ctp_core.esub_application_submission
  WHERE coalesce(halo_is_deleted, false) = false
),
app AS (
  SELECT CAST(try_cast(APPLICATION_KEY AS BIGINT) AS STRING)                                 AS app_id
  FROM slvr_ctp_core.esub_application
  WHERE coalesce(halo_is_deleted, false) = false
),
doc_type AS (
  SELECT CAST(try_cast(DOC_TYPES_KEY AS BIGINT) AS STRING)                                   AS doc_type_id
  FROM slvr_ctp_core.esub_itrac_doc_types
  WHERE coalesce(halo_is_deleted, false) = false
),
-- ---------- Distinct value sets (one row per different value - blanks never match) ----------
s_login_tracking    AS (SELECT DISTINCT tracking_no AS k FROM login WHERE NOT is_inactive),
s_login_stn         AS (SELECT DISTINCT stn AS k         FROM login WHERE NOT is_inactive),
s_login_stn_any     AS (SELECT DISTINCT stn AS k         FROM login),
s_login_stn_or_link AS (SELECT stn AS k FROM login UNION SELECT stn_link AS k FROM login),
s_login_link        AS (SELECT DISTINCT stn_link AS k    FROM login),
s_login_package     AS (SELECT DISTINCT package_id AS k  FROM login WHERE NOT is_inactive),
s_login_id          AS (SELECT DISTINCT login_id AS k    FROM login),
s_login_doc_type    AS (SELECT DISTINCT doc_type_id AS k FROM login),
s_esub_stn          AS (SELECT DISTINCT stn AS k         FROM esub WHERE NOT is_deleted AND NOT is_inactive),
s_esub_stn_due      AS (SELECT DISTINCT stn AS k         FROM esub WHERE NOT is_deleted AND NOT is_inactive AND NOT is_on_hold),
s_esub_package      AS (SELECT DISTINCT package_id AS k  FROM esub WHERE NOT is_deleted AND NOT is_inactive),
s_esub_package_any  AS (SELECT DISTINCT package_id AS k  FROM esub),
s_esub_id           AS (SELECT DISTINCT esub_id AS k     FROM esub),
s_known_stn         AS (SELECT stn AS k FROM login UNION SELECT stn AS k FROM esub WHERE NOT is_deleted),
s_link_parent       AS (SELECT DISTINCT parent_stn AS k  FROM sub_link),
s_link_child        AS (SELECT DISTINCT child_stn AS k   FROM sub_link),
s_amended           AS (SELECT DISTINCT stn AS k         FROM amended),
s_subdoc_esub       AS (SELECT DISTINCT esub_id AS k     FROM sub_doc),
s_subdoc_doc        AS (SELECT DISTINCT doc_id AS k      FROM sub_doc),
s_doc_id            AS (SELECT DISTINCT doc_id AS k      FROM doc),
s_doc_package       AS (SELECT DISTINCT package_id AS k  FROM doc),
s_applink_app       AS (SELECT DISTINCT app_id AS k      FROM app_link),
s_applink_login     AS (SELECT DISTINCT login_id AS k    FROM app_link),
s_app_id            AS (SELECT DISTINCT app_id AS k      FROM app),
s_doc_type_id       AS (SELECT DISTINCT doc_type_id AS k FROM doc_type),
pair_counts AS (
  SELECT 'K01 eSUB <-> eSUB | Log-in record vs electronic submission (tracking number)' AS pair_label,
         'eSUB: log-in record tracking number (active log-ins)  [esub_itrac_reglist.TRACKING_NUMBER]' AS left_side,
         'eSUB: electronic submission tracking number  [esub_tob_submission.SUBMISSION_TRACKING_NUMBER]' AS right_side,
         count(l.k) AS left_distinct, count(r.k) AS right_distinct,
         count_if(l.k IS NOT NULL AND r.k IS NOT NULL) AS matched_distinct,
         'Alternative candidate - the app does NOT use it. Compare with K02: the higher of the two is the real link. If K01 is clearly higher, tell us: the app would need to switch.' AS notes
  FROM s_login_tracking l FULL OUTER JOIN s_esub_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K02 eSUB <-> eSUB | Log-in record vs electronic submission (STN)',
         'eSUB: log-in record STN (active log-ins)  [esub_itrac_reglist.STN]',
         'eSUB: electronic submission tracking number  [esub_tob_submission.SUBMISSION_TRACKING_NUMBER]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes the electronic submission tracking number IS the STN on the log-in record ("no files" and "never logged in" checks). High % right = assumption holds. Log-ins received on paper have no electronic record, so % left can be lower.'
  FROM s_login_stn l FULL OUTER JOIN s_esub_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K03 eSUB <-> eSUB | Log-in record vs electronic submission (package number)',
         'eSUB: log-in record package number (active log-ins)  [esub_itrac_reglist.PACKAGE_KEY]',
         'eSUB: electronic submission package number  [esub_tob_submission.PACKAGE_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app uses the package number as a second way to tie a log-in to its electronic submission and its files. High % right = assumption holds.'
  FROM s_login_package l FULL OUTER JOIN s_esub_package r ON l.k = r.k
  UNION ALL
  SELECT 'K04 eSUB <-> eSUB | Submission link (STN the link is recorded on) vs STNs eSUB has',
         'eSUB: submission link, recorded-on STN  [esub_itrac_sub_links.STN]',
         'eSUB: every STN eSUB has (log-in records, active or inactive, plus electronic submissions not deleted)  [esub_itrac_reglist.STN + esub_tob_submission.SUBMISSION_TRACKING_NUMBER]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app flags links to an STN eSUB does not have. Expect % left near 100 - the rest are links to mistyped or pre-eSUB STNs. % right is naturally low (most submissions have no links).'
  FROM s_link_parent l FULL OUTER JOIN s_known_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K05 eSUB <-> eSUB | Submission link (linked STN) vs STNs eSUB has',
         'eSUB: submission link, linked STN  [esub_itrac_sub_links.CHILD_STN]',
         'eSUB: every STN eSUB has (same as K04)  [esub_itrac_reglist.STN + esub_tob_submission.SUBMISSION_TRACKING_NUMBER]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Same as K04 for the other end of the link. If K04 is high and K05 is low (or the reverse), tell us: it may mean the app has the two ends of the link backwards.'
  FROM s_link_child l FULL OUTER JOIN s_known_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K38 eSUB <-> eSUB | Electronic submission vs log-in record STN or STN link (the "logged in" test the app uses)',
         'eSUB: electronic submission tracking number (not deleted, not inactive, not on hold)  [esub_tob_submission.SUBMISSION_TRACKING_NUMBER]',
         'eSUB: log-in record STN or STN link (active and inactive log-ins)  [esub_itrac_reglist.STN + STN_LINK]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "never logged in" check counts an electronic submission as logged in when a log-in record has its STN as its own STN or as its STN link (or shares its package, K03). Expect % left near 100 for kinds of submission that are always logged in.'
  FROM s_esub_stn_due l FULL OUTER JOIN s_login_stn_or_link r ON l.k = r.k
  UNION ALL
  SELECT 'K39 eSUB <-> eSUB | Log-in record STN link vs log-in record STNs',
         'eSUB: log-in record STN link  [esub_itrac_reglist.STN_LINK]',
         'eSUB: log-in record STN (active and inactive log-ins)  [esub_itrac_reglist.STN]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes a log-in record with no STN of its own but an STN link is an add-on document for that other STN. Expect % left near 100. If it is low, the STN link field means something else - please tell us what.'
  FROM s_login_link l FULL OUTER JOIN s_login_stn_any r ON l.k = r.k
  UNION ALL
  SELECT 'K40 eSUB <-> eSUB | STN being amended vs STNs eSUB has',
         'eSUB: STNs named as "being amended" (split as the app does - not deleted, not inactive)  [esub_tob_submission.STN_BEING_AMENDED]',
         'eSUB: every STN eSUB has (same as K04)  [esub_itrac_reglist.STN + esub_tob_submission.SUBMISSION_TRACKING_NUMBER]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app flags amendments that name an STN eSUB does not have. Expect % left near 100. If it is low, the field holds something other than STNs (or STNs with a suffix the split does not remove) - P2.32 shows STN shapes.'
  FROM s_amended l FULL OUTER JOIN s_known_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K41 eSUB <-> eSUB | Submission-file list vs electronic submissions',
         'eSUB: submission number on the submission-file list  [esub_submission_document.SUBMISSION_KEY]',
         'eSUB: electronic submission number (any status)  [esub_tob_submission.SUBMISSION_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "no files" check finds the files of a submission through this link. Expect % left near 100. % right = share of electronic submissions with at least one file listed.'
  FROM s_subdoc_esub l FULL OUTER JOIN s_esub_id r ON l.k = r.k
  UNION ALL
  SELECT 'K42 eSUB <-> eSUB | Submission-file list vs document list',
         'eSUB: document number on the submission-file list  [esub_submission_document.DOCUMENT_KEY]',
         'eSUB: document number  [esub_document.DOCUMENT_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Expect % left near 100: every listed file should exist in the document list.'
  FROM s_subdoc_doc l FULL OUTER JOIN s_doc_id r ON l.k = r.k
  UNION ALL
  SELECT 'K43 eSUB <-> eSUB | Document list package vs electronic submission package',
         'eSUB: package number on a document  [esub_document.PACKAGE_KEY]',
         'eSUB: electronic submission package number (any status)  [esub_tob_submission.PACKAGE_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "no files" check also finds files by package number. Expect % left near 100 if documents belong to electronic submission packages.'
  FROM s_doc_package l FULL OUTER JOIN s_esub_package_any r ON l.k = r.k
  UNION ALL
  SELECT 'K44 eSUB <-> eSUB | Application-submission link vs applications',
         'eSUB: application number on an application-submission link  [esub_application_submission.APPLICATION_KEY]',
         'eSUB: application number (deleted applications included)  [esub_application.APPLICATION_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "application link broken" check flags links to an application eSUB does not have. Expect % left near 100.'
  FROM s_applink_app l FULL OUTER JOIN s_app_id r ON l.k = r.k
  UNION ALL
  SELECT 'K45 eSUB <-> eSUB | Application-submission link vs log-in records',
         'eSUB: log-in record number on an application-submission link  [esub_application_submission.REGLIST_KEY]',
         'eSUB: log-in record number (active and inactive)  [esub_itrac_reglist.REGLIST_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes the application link points at the log-in record number. Expect % left near 100. If low, the field may hold the electronic submission number instead - please tell us.'
  FROM s_applink_login l FULL OUTER JOIN s_login_id r ON l.k = r.k
  UNION ALL
  SELECT 'K46 eSUB <-> eSUB | Log-in document type vs document type list',
         'eSUB: document type number on log-in records  [esub_itrac_reglist.DOC_TYPES_KEY]',
         'eSUB: document type list  [esub_itrac_doc_types.DOC_TYPES_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Expect % left near 100 - P2.03 lists the values that are not on the list.'
  FROM s_login_doc_type l FULL OUTER JOIN s_doc_type_id r ON l.k = r.k
)
SELECT
  'P1.1'                                                         AS section,
  pair_label,
  left_side,
  right_side,
  left_distinct,
  right_distinct,
  matched_distinct,
  round(100.0 * matched_distinct / nullif(left_distinct, 0), 1)  AS pct_left_matched,
  round(100.0 * matched_distinct / nullif(right_distinct, 0), 1) AS pct_right_matched,
  notes
FROM pair_counts
ORDER BY pair_label;


-- -----------------------------------------------------------------------------------------------------
-- P1.2  eSUB <-> Rhapsody: submissions (STN, eSUB log-in record number, company FEI)
-- -----------------------------------------------------------------------------------------------------
WITH
login AS (
  SELECT
    CAST(try_cast(REGLIST_KEY AS BIGINT) AS STRING)                                          AS login_id,
    nullif(upper(regexp_replace(CAST(STN AS STRING), '[\\s\\p{Z}]+', '')), '')               AS stn,
    nullif(upper(regexp_replace(CAST(STN AS STRING), '[^A-Za-z0-9]', '')), '')               AS stn_loose,
    -- FEI as the app compares it: digits only, read as a whole number (leading zeros, spaces and dashes ignored)
    coalesce(CAST(try_cast(try_cast(trim(regexp_replace(CAST(FEI AS STRING), '[^0-9]', '')) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(regexp_replace(CAST(FEI AS STRING), '[^0-9]', ''))), '')) AS fei_key,
    coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false) AS is_inactive
  FROM slvr_ctp_core.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_sub AS (
  SELECT
    nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')               AS stn,
    nullif(upper(regexp_replace(CAST(stn AS STRING), '[^A-Za-z0-9]', '')), '')               AS stn_loose,
    CAST(try_cast(reglist_key AS BIGINT) AS STRING)                                          AS login_id,
    coalesce(CAST(try_cast(try_cast(trim(regexp_replace(CAST(abpc_fei AS STRING), '[^0-9]', '')) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(regexp_replace(CAST(abpc_fei AS STRING), '[^0-9]', ''))), '')) AS fei_key
  FROM slvr_ctp_core.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
),
-- (STN, FEI) pairs, only for STNs that have an FEI of 5+ digits in BOTH systems (the FEI check population).
e_fei AS (SELECT DISTINCT stn, fei_key FROM login    WHERE stn IS NOT NULL AND length(fei_key) >= 5),
r_fei AS (SELECT DISTINCT stn, fei_key FROM rhap_sub WHERE stn IS NOT NULL AND length(fei_key) >= 5),
s_login_stn_fei     AS (SELECT DISTINCT concat(stn, '|', fei_key) AS k FROM e_fei WHERE stn IN (SELECT stn FROM r_fei)),
s_rhap_stn_fei      AS (SELECT DISTINCT concat(stn, '|', fei_key) AS k FROM r_fei WHERE stn IN (SELECT stn FROM e_fei)),
s_login_stn         AS (SELECT DISTINCT stn AS k                     FROM login WHERE NOT is_inactive),
s_login_stn_any     AS (SELECT DISTINCT stn AS k                     FROM login),
s_login_stn_loose   AS (SELECT DISTINCT stn_loose AS k               FROM login WHERE NOT is_inactive),
s_login_id          AS (SELECT DISTINCT login_id AS k                FROM login),
s_login_id_stn      AS (SELECT DISTINCT concat(login_id, '|', stn) AS k FROM login),
s_rhap_stn          AS (SELECT DISTINCT stn AS k                     FROM rhap_sub),
s_rhap_stn_loose    AS (SELECT DISTINCT stn_loose AS k               FROM rhap_sub),
s_rhap_login_id     AS (SELECT DISTINCT login_id AS k                FROM rhap_sub),
s_rhap_login_id_stn AS (SELECT DISTINCT concat(login_id, '|', stn) AS k FROM rhap_sub),
pair_counts AS (
  SELECT 'K06 Rhapsody <-> eSUB | Submission STN' AS pair_label,
         'Rhapsody: submission STN  [rhapsody_submissions.stn]' AS left_side,
         'eSUB: log-in record STN (active log-ins)  [esub_itrac_reglist.STN]' AS right_side,
         count(l.k) AS left_distinct, count(r.k) AS right_distinct,
         count_if(l.k IS NOT NULL AND r.k IS NOT NULL) AS matched_distinct,
         'The main link between eSUB and Rhapsody (app STN rule). Expect % left near 100 (almost every Rhapsody submission was logged in to eSUB). % right is lower because some eSUB types never go to Rhapsody. Compare with K07 and K08.' AS notes
  FROM s_rhap_stn l FULL OUTER JOIN s_login_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K07 Rhapsody <-> eSUB | Submission STN, ignoring dashes, dots and other punctuation',
         'Rhapsody: submission STN, letters and digits only  [rhapsody_submissions.stn]',
         'eSUB: log-in record STN, letters and digits only (active log-ins)  [esub_itrac_reglist.STN]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Looser than the app rule (which only removes spaces). If this is clearly higher than K06, the systems punctuate STNs differently and the app should compare letters and digits only - please tell us whether such STNs are really the same.'
  FROM s_rhap_stn_loose l FULL OUTER JOIN s_login_stn_loose r ON l.k = r.k
  UNION ALL
  SELECT 'K08 Rhapsody <-> eSUB | Submission STN, including inactive eSUB log-ins',
         'Rhapsody: submission STN  [rhapsody_submissions.stn]',
         'eSUB: log-in record STN (active AND inactive log-ins)  [esub_itrac_reglist.STN]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Same as K06 but inactive eSUB log-ins are kept. The difference from K06 = Rhapsody submissions whose only eSUB log-in is inactive.'
  FROM s_rhap_stn l FULL OUTER JOIN s_login_stn_any r ON l.k = r.k
  UNION ALL
  SELECT 'K09 Rhapsody <-> eSUB | eSUB log-in record number',
         'Rhapsody: eSUB log-in record number on the submission  [rhapsody_submissions.reglist_key]',
         'eSUB: log-in record number (active and inactive log-ins)  [esub_itrac_reglist.REGLIST_KEY]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes Rhapsody stores the eSUB log-in record number. Expect % left near 100 if the assumption holds. If left_distinct is much smaller than in K06, the field is only filled for some submissions.'
  FROM s_rhap_login_id l FULL OUTER JOIN s_login_id r ON l.k = r.k
  UNION ALL
  SELECT 'K10 Rhapsody <-> eSUB | Log-in record number AND STN together',
         'Rhapsody: log-in record number + STN  [rhapsody_submissions.reglist_key + stn]',
         'eSUB: log-in record number + STN (active and inactive log-ins)  [esub_itrac_reglist.REGLIST_KEY + STN]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Checks that the log-in record number in Rhapsody points at a log-in with the SAME STN. If K09 is high but K10 is clearly lower, some Rhapsody submissions point at the wrong eSUB log-in.'
  FROM s_rhap_login_id_stn l FULL OUTER JOIN s_login_id_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K66 Rhapsody <-> eSUB | Company FEI number on the same STN',
         'Rhapsody: STN + company FEI (STNs with an FEI in both systems)  [rhapsody_submissions.stn + abpc_fei]',
         'eSUB: STN + FEI on any log-in record of that STN  [esub_itrac_reglist.STN + FEI]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The FEI check assumes the eSUB FEI is the FEI of the applicant firm, like the Rhapsody company FEI. Expect % left near 100. If it is low (for example under 70), eSUB probably holds a manufacturing site FEI and that check should be dropped - please tell us. (The check also accepts a match inside a field that lists several numbers, so its pass rate is a little higher.)'
  FROM s_rhap_stn_fei l FULL OUTER JOIN s_login_stn_fei r ON l.k = r.k
)
SELECT
  'P1.2'                                                         AS section,
  pair_label,
  left_side,
  right_side,
  left_distinct,
  right_distinct,
  matched_distinct,
  round(100.0 * matched_distinct / nullif(left_distinct, 0), 1)  AS pct_left_matched,
  round(100.0 * matched_distinct / nullif(right_distinct, 0), 1) AS pct_right_matched,
  notes
FROM pair_counts
ORDER BY pair_label;


-- -----------------------------------------------------------------------------------------------------
-- P1.3  Inside Rhapsody: submissions, the three product lists, bundles, letters, review tasks
-- -----------------------------------------------------------------------------------------------------
WITH
rhap_sub AS (
  SELECT nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')         AS stn
  FROM slvr_ctp_core.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_prod AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id
  FROM slvr_ctp_core.rhapsody_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_bulk AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id,
    CAST(try_cast(bundle_id AS BIGINT) AS STRING)                                            AS bundle_id,
    nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')               AS stn
  FROM slvr_ctp_core.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_link AS (
  SELECT
    nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')               AS stn,
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id,
    nullif(upper(trim(CAST(bundle_id AS STRING))), '')                                       AS bundle_id_typed,
    coalesce(CAST(try_cast(try_cast(trim(CAST(bundle_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(bundle_id AS STRING))), ''))                             AS bundle_id
  FROM slvr_ctp_core.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_wrap AS (
  SELECT
    nullif(upper(regexp_replace(CAST(untitled_relationship_stn AS STRING), '[\\s\\p{Z}]+', '')), '') AS stn,
    coalesce(CAST(try_cast(try_cast(trim(CAST(untitled_relationship_product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(untitled_relationship_product_id AS STRING))), ''))      AS product_id
  FROM slvr_ctp_core.rhapsody_submission_product_wrapper
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
),
rhap_comm AS (
  SELECT
    nullif(upper(trim(CAST(bundleid AS STRING))), '')                                        AS bundle_id_typed,
    coalesce(CAST(try_cast(try_cast(trim(CAST(bundleid AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(bundleid AS STRING))), ''))                              AS bundle_id,
    CAST(try_cast(uploadcommunicationstobundle_id AS BIGINT) AS STRING)                      AS upload_bundle_id
  FROM slvr_ctp_core.rhapsody_communications
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
),
rhap_bundle AS (
  SELECT CAST(try_cast(id AS BIGINT) AS STRING)                                              AS bundle_id
  FROM slvr_ctp_core.rhapsody_bundle
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
),
rhap_task AS (
  -- The overdue-task check takes the first "two letters + seven digits" in the task title as its STN.
  SELECT
    nullif(upper(regexp_replace(CAST(regexp_extract(upper(coalesce(subject, '')), '([A-Z]{2}[0-9]{7})', 1) AS STRING),
                                '[\\s\\p{Z}]+', '')), '')                                    AS stn
  FROM slvr_ctp_core.rhapsody_notf_task_instance
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(delete_flag, 0) = 0
),
s_sub_stn            AS (SELECT DISTINCT stn AS k             FROM rhap_sub),
s_prod_id            AS (SELECT DISTINCT product_id AS k      FROM rhap_prod),
s_prod_or_bulk_id    AS (SELECT product_id AS k FROM rhap_prod UNION SELECT product_id AS k FROM rhap_bulk),
s_bulk_id            AS (SELECT DISTINCT product_id AS k      FROM rhap_bulk),
s_bulk_bundle        AS (SELECT DISTINCT bundle_id AS k       FROM rhap_bulk),
s_bulk_stn           AS (SELECT DISTINCT stn AS k             FROM rhap_bulk),
s_link_stn           AS (SELECT DISTINCT stn AS k             FROM rhap_link),
s_link_product       AS (SELECT DISTINCT product_id AS k      FROM rhap_link),
s_link_bundle_typed  AS (SELECT DISTINCT bundle_id_typed AS k FROM rhap_link),
s_link_bundle        AS (SELECT DISTINCT bundle_id AS k       FROM rhap_link),
s_wrap_stn           AS (SELECT DISTINCT stn AS k             FROM rhap_wrap),
s_wrap_product       AS (SELECT DISTINCT product_id AS k      FROM rhap_wrap),
s_comm_bundle_typed  AS (SELECT DISTINCT bundle_id_typed AS k FROM rhap_comm),
s_comm_bundle        AS (SELECT DISTINCT bundle_id AS k       FROM rhap_comm),
s_comm_upload        AS (SELECT DISTINCT upload_bundle_id AS k FROM rhap_comm),
s_bundle_id          AS (SELECT DISTINCT bundle_id AS k       FROM rhap_bundle),
s_task_stn           AS (SELECT DISTINCT stn AS k             FROM rhap_task),
pair_counts AS (
  SELECT 'K11 Rhapsody <-> Rhapsody | Submission-product link vs submissions (STN)' AS pair_label,
         'Rhapsody: submission-product link STN  [rhapsody_submission_products.stn]' AS left_side,
         'Rhapsody: submission STN  [rhapsody_submissions.stn]' AS right_side,
         count(l.k) AS left_distinct, count(r.k) AS right_distinct,
         count_if(l.k IS NOT NULL AND r.k IS NOT NULL) AS matched_distinct,
         'Every product link should belong to a known submission: expect % left near 100. % right is the share of submissions that have at least one product link.' AS notes
  FROM s_link_stn l FULL OUTER JOIN s_sub_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K12 Rhapsody <-> Rhapsody | Submission-product link vs regular + bulk product lists',
         'Rhapsody: submission-product link product ID  [rhapsody_submission_products.product_id]',
         'Rhapsody: product ID on the regular OR bulk product list  [rhapsody_products.product_id + rhapsody_bulk_products.product_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "links broken" check treats a linked product as known when it is on either product list. Expect % left near 100.'
  FROM s_link_product l FULL OUTER JOIN s_prod_or_bulk_id r ON l.k = r.k
  UNION ALL
  SELECT 'K13 Rhapsody <-> Rhapsody | Submission-product link vs bundles, text exactly as typed',
         'Rhapsody: submission-product link bundle ID as typed (stored as text)  [rhapsody_submission_products.bundle_id]',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Stricter than the app rule (K14). If K14 is clearly higher, the text IDs carry leading zeros or decimals and the number rule of the app is needed.'
  FROM s_link_bundle_typed l FULL OUTER JOIN s_bundle_id r ON l.k = r.k
  UNION ALL
  SELECT 'K14 Rhapsody <-> Rhapsody | Submission-product link vs bundles (app rule)',
         'Rhapsody: submission-product link bundle ID, read as a number when it is one  [rhapsody_submission_products.bundle_id]',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes the text bundle ID on a product link is the bundle number ("00123" or "123.0" count as 123). Expect % left near 100 (a few links to deleted bundles are normal).'
  FROM s_link_bundle l FULL OUTER JOIN s_bundle_id r ON l.k = r.k
  UNION ALL
  SELECT 'K15 Rhapsody <-> Rhapsody | Bulk product list vs regular product list',
         'Rhapsody: bulk-submission product ID  [rhapsody_bulk_products.product_id]',
         'Rhapsody: regular product list product ID  [rhapsody_products.product_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app treats bulk products like regular products and counts a product on both lists once. Tells us whether bulk products also appear in the regular list (high % left) or are kept separately (low % left - that is fine, just tell us).'
  FROM s_bulk_id l FULL OUTER JOIN s_prod_id r ON l.k = r.k
  UNION ALL
  SELECT 'K16 Rhapsody <-> Rhapsody | Bulk product list vs bundles',
         'Rhapsody: bulk-submission product bundle ID  [rhapsody_bulk_products.bundle_id]',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The bundle product-count check counts bulk products in the bundle they name. Expect % left near 100 if the bulk-product bundle ID is the bundle number.'
  FROM s_bulk_bundle l FULL OUTER JOIN s_bundle_id r ON l.k = r.k
  UNION ALL
  SELECT 'K17 Rhapsody <-> Rhapsody | Product wrapper vs regular + bulk product lists',
         'Rhapsody: product wrapper product ID (not temporary copies)  [rhapsody_submission_product_wrapper.untitled_relationship_product_id]',
         'Rhapsody: product ID on the regular OR bulk product list  [rhapsody_products.product_id + rhapsody_bulk_products.product_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app treats the product wrapper as a third list of submission products using the same product IDs. Expect % left near 100 if it does.'
  FROM s_wrap_product l FULL OUTER JOIN s_prod_or_bulk_id r ON l.k = r.k
  UNION ALL
  SELECT 'K18 Rhapsody <-> Rhapsody | Product wrapper vs submissions (STN)',
         'Rhapsody: product wrapper STN (not temporary copies)  [rhapsody_submission_product_wrapper.untitled_relationship_stn]',
         'Rhapsody: submission STN  [rhapsody_submissions.stn]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Expect % left near 100 if the wrapper STN is the submission STN.'
  FROM s_wrap_stn l FULL OUTER JOIN s_sub_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K19 Rhapsody <-> Rhapsody | Letters vs bundles (bundle ID text exactly as typed)',
         'Rhapsody: letter/communication bundle ID as typed (stored as text)  [rhapsody_communications.bundleid]',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Stricter than the app rule (K20). If K20 is clearly higher, the text IDs carry leading zeros or decimals.'
  FROM s_comm_bundle_typed l FULL OUTER JOIN s_bundle_id r ON l.k = r.k
  UNION ALL
  SELECT 'K20 Rhapsody <-> Rhapsody | Letters vs bundles (bundle ID text, app rule)',
         'Rhapsody: letter/communication bundle ID, read as a number when it is one  [rhapsody_communications.bundleid]',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app accepts a letter when EITHER this field or the upload-to-bundle number (K21) reaches a live bundle. Tell us if one of the two is clearly the real link.'
  FROM s_comm_bundle l FULL OUTER JOIN s_bundle_id r ON l.k = r.k
  UNION ALL
  SELECT 'K21 Rhapsody <-> Rhapsody | Letters vs bundles (upload-to-bundle number)',
         'Rhapsody: letter/communication upload-to-bundle number  [rhapsody_communications.uploadcommunicationstobundle_id]',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Second way a letter points at its bundle (the app tries this one first). Expect % left near 100 if this is the bundle number.'
  FROM s_comm_upload l FULL OUTER JOIN s_bundle_id r ON l.k = r.k
  UNION ALL
  SELECT 'K47 Rhapsody <-> Rhapsody | Bulk product list vs submissions (STN)',
         'Rhapsody: bulk-submission product STN  [rhapsody_bulk_products.stn]',
         'Rhapsody: submission STN  [rhapsody_submissions.stn]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app counts a bulk product as linked to the submission with this STN ("no products", product-count and PMS checks). Expect % left near 100.'
  FROM s_bulk_stn l FULL OUTER JOIN s_sub_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K48 Rhapsody <-> Rhapsody | STN found in review task titles vs submissions',
         'Rhapsody: first "2 letters + 7 digits" in a review task title (tasks not deleted)  [rhapsody_notf_task_instance.subject]',
         'Rhapsody: submission STN  [rhapsody_submissions.stn]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The overdue-task check gets the submission type and office of a task through the STN in its title. Expect % left near 100. Part 4 (row U20) shows how many tasks have no STN in the title - if STNs have another shape (see P2.32), tell us.'
  FROM s_task_stn l FULL OUTER JOIN s_sub_stn r ON l.k = r.k
)
SELECT
  'P1.3'                                                         AS section,
  pair_label,
  left_side,
  right_side,
  left_distinct,
  right_distinct,
  matched_distinct,
  round(100.0 * matched_distinct / nullif(left_distinct, 0), 1)  AS pct_left_matched,
  round(100.0 * matched_distinct / nullif(right_distinct, 0), 1) AS pct_right_matched,
  notes
FROM pair_counts
ORDER BY pair_label;


-- -----------------------------------------------------------------------------------------------------
-- P1.4  Rhapsody <-> PMS: which PMS ID does Rhapsody store for a product?
-- -----------------------------------------------------------------------------------------------------
-- The app currently matches the Rhapsody product ID (regular and bulk lists) to the PMS product record
-- number (K23, K49), and, when no PMS product has that number, to the PMS UUID (K24). K50 is that combined
-- rule. K22 and K25-K28 and K51 test the other candidates.
WITH
rhap_prod AS (
  SELECT
    nullif(upper(trim(CAST(product_id AS STRING))), '')                                      AS product_id_typed,
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id,
    coalesce(CAST(try_cast(try_cast(trim(CAST(static_product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(static_product_id AS STRING))), ''))                     AS static_id,
    CAST(try_cast(static_product_id_number AS BIGINT) AS STRING)                             AS static_id_number,
    nullif(upper(regexp_replace(trim(CAST(tp_number AS STRING)), '\\s+', ' ')), '')          AS tp_number
  FROM slvr_ctp_core.rhapsody_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_bulk AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id,
    nullif(upper(regexp_replace(trim(CAST(tp_number AS STRING)), '\\s+', ' ')), '')          AS tp_number
  FROM slvr_ctp_core.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_link AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id
  FROM slvr_ctp_core.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_sub AS (
  SELECT nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')         AS stn
  FROM slvr_ctp_core.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
),
pms_prod AS (
  SELECT
    CAST(try_cast(id AS BIGINT) AS STRING)                                                   AS pms_id,
    coalesce(CAST(try_cast(try_cast(trim(CAST(uuid AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(uuid AS STRING))), ''))                                  AS uuid,
    coalesce(CAST(try_cast(try_cast(trim(CAST(static_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(static_id AS STRING))), ''))                             AS static_id,
    CAST(try_cast(unique_product_id AS BIGINT) AS STRING)                                    AS unique_id,
    nullif(upper(regexp_replace(CAST(stdz_stn AS STRING), '[\\s\\p{Z}]+', '')), '')          AS stn,
    nullif(upper(regexp_replace(trim(CAST(tp_number AS STRING)), '\\s+', ' ')), '')          AS tp_number
  FROM slvr_ctp_core.pms_product
  WHERE coalesce(halo_is_deleted, false) = false
),
s_rhap_id_typed   AS (SELECT DISTINCT product_id_typed AS k FROM rhap_prod),
s_rhap_id         AS (SELECT DISTINCT product_id AS k       FROM rhap_prod),
s_rhap_static     AS (SELECT DISTINCT static_id AS k        FROM rhap_prod),
s_rhap_static_num AS (SELECT DISTINCT static_id_number AS k FROM rhap_prod),
s_bulk_id         AS (SELECT DISTINCT product_id AS k       FROM rhap_bulk),
s_rhap_any_id     AS (SELECT product_id AS k FROM rhap_prod UNION SELECT product_id AS k FROM rhap_bulk),
s_rhap_tp         AS (SELECT tp_number AS k FROM rhap_prod UNION SELECT tp_number AS k FROM rhap_bulk),
s_link_product    AS (SELECT DISTINCT product_id AS k       FROM rhap_link),
s_rhap_stn        AS (SELECT DISTINCT stn AS k              FROM rhap_sub),
s_pms_id          AS (SELECT DISTINCT pms_id AS k           FROM pms_prod),
s_pms_uuid        AS (SELECT DISTINCT uuid AS k             FROM pms_prod),
s_pms_id_or_uuid  AS (SELECT pms_id AS k FROM pms_prod UNION SELECT uuid AS k FROM pms_prod),
s_pms_static      AS (SELECT DISTINCT static_id AS k        FROM pms_prod),
s_pms_unique_ref  AS (SELECT DISTINCT unique_id AS k        FROM pms_prod),
s_pms_stn         AS (SELECT DISTINCT stn AS k              FROM pms_prod),
s_pms_tp          AS (SELECT DISTINCT tp_number AS k        FROM pms_prod),
pair_counts AS (
  SELECT 'K22 Rhapsody <-> PMS | Product ID exactly as typed vs PMS record number' AS pair_label,
         'Rhapsody: product list product ID as typed  [rhapsody_products.product_id]' AS left_side,
         'PMS: product record number  [pms_product.id]' AS right_side,
         count(l.k) AS left_distinct, count(r.k) AS right_distinct,
         count_if(l.k IS NOT NULL AND r.k IS NOT NULL) AS matched_distinct,
         'Stricter than the app rule (K23). If K23 is clearly higher, the Rhapsody IDs carry leading zeros or decimals.' AS notes
  FROM s_rhap_id_typed l FULL OUTER JOIN s_pms_id r ON l.k = r.k
  UNION ALL
  SELECT 'K23 Rhapsody <-> PMS | Product ID vs PMS record number (app rule, first choice)',
         'Rhapsody: product list product ID, read as a number when it is one  [rhapsody_products.product_id]',
         'PMS: product record number  [pms_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app currently assumes THIS is the Rhapsody-to-PMS product link (with K24 as a fallback) - every Rhapsody <-> PMS check depends on it. K22 to K28 and K49 to K51 test the candidates - normally only one is high. Please tell us which.'
  FROM s_rhap_id l FULL OUTER JOIN s_pms_id r ON l.k = r.k
  UNION ALL
  SELECT 'K24 Rhapsody <-> PMS | Product ID vs PMS UUID (app rule, fallback)',
         'Rhapsody: product list product ID  [rhapsody_products.product_id]',
         'PMS: product UUID  [pms_product.uuid]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Candidate: Rhapsody stores the PMS UUID (long letters-and-digits ID) as its product ID. The app tries this when K23 finds nothing.'
  FROM s_rhap_id l FULL OUTER JOIN s_pms_uuid r ON l.k = r.k
  UNION ALL
  SELECT 'K25 Rhapsody <-> PMS | Product ID vs PMS static ID',
         'Rhapsody: product list product ID  [rhapsody_products.product_id]',
         'PMS: static product ID  [pms_product.static_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Candidate (not used by the app): Rhapsody stores the PMS static ID as its product ID.'
  FROM s_rhap_id l FULL OUTER JOIN s_pms_static r ON l.k = r.k
  UNION ALL
  SELECT 'K26 Rhapsody <-> PMS | Product ID vs PMS unique product number',
         'Rhapsody: product list product ID  [rhapsody_products.product_id]',
         'PMS: unique product number  [pms_product.unique_product_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Candidate (not used by the app): Rhapsody stores the PMS unique product number as its product ID.'
  FROM s_rhap_id l FULL OUTER JOIN s_pms_unique_ref r ON l.k = r.k
  UNION ALL
  SELECT 'K27 Rhapsody <-> PMS | Static product ID vs PMS static ID',
         'Rhapsody: static product ID  [rhapsody_products.static_product_id]',
         'PMS: static product ID  [pms_product.static_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Candidate: the two "static" IDs are the same thing. If this is high and K23 is low, the app should link products through the static ID instead.'
  FROM s_rhap_static l FULL OUTER JOIN s_pms_static r ON l.k = r.k
  UNION ALL
  SELECT 'K28 Rhapsody <-> PMS | Static product ID number vs PMS static ID',
         'Rhapsody: static product ID number  [rhapsody_products.static_product_id_number]',
         'PMS: static product ID, read as a number when it is one  [pms_product.static_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Candidate: Rhapsody keeps the PMS static ID as a number in a separate field.'
  FROM s_rhap_static_num l FULL OUTER JOIN s_pms_static r ON l.k = r.k
  UNION ALL
  SELECT 'K29 Rhapsody <-> PMS | Submission-product link product ID vs PMS record number',
         'Rhapsody: submission-product link product ID  [rhapsody_submission_products.product_id]',
         'PMS: product record number  [pms_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app matches products linked to a submission straight to PMS by this ID. Expect a % left similar to K23.'
  FROM s_link_product l FULL OUTER JOIN s_pms_id r ON l.k = r.k
  UNION ALL
  SELECT 'K30 PMS <-> Rhapsody | Product STN vs submission STN',
         'PMS: STN the product was submitted under  [pms_product.stdz_stn]',
         'Rhapsody: submission STN  [rhapsody_submissions.stn]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes the PMS product STN is the same STN as in Rhapsody (submission type and office of PMS products, and the "not linked to its STN" check). Expect % left near 100. If low, look at P2.32: PMS may write STNs in a different format.'
  FROM s_pms_stn l FULL OUTER JOIN s_rhap_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K49 Rhapsody <-> PMS | Bulk product ID vs PMS record number',
         'Rhapsody: bulk-submission product ID, read as a number when it is one  [rhapsody_bulk_products.product_id]',
         'PMS: product record number  [pms_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app matches bulk products to PMS exactly like regular products. If this is much lower than K23, bulk products are not PMS products (or use another ID) - please tell us.'
  FROM s_bulk_id l FULL OUTER JOIN s_pms_id r ON l.k = r.k
  UNION ALL
  SELECT 'K50 Rhapsody <-> PMS | Regular + bulk product ID vs PMS record number OR UUID (the full rule the app uses)',
         'Rhapsody: product ID on the regular or bulk product list  [rhapsody_products.product_id + rhapsody_bulk_products.product_id]',
         'PMS: product record number or UUID  [pms_product.id + pms_product.uuid]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Exactly what the "linked product missing from PMS" check measures. % left = share of Rhapsody products the app can find in PMS.'
  FROM s_rhap_any_id l FULL OUTER JOIN s_pms_id_or_uuid r ON l.k = r.k
  UNION ALL
  SELECT 'K51 Rhapsody <-> PMS | TP number',
         'Rhapsody: TP number on the regular or bulk product list  [rhapsody_products.tp_number + rhapsody_bulk_products.tp_number]',
         'PMS: TP number  [pms_product.tp_number]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Candidate link that the app does not use (it only compares TP numbers once products are matched). If this is high while K23 and K24 are low, TP number may be the practical link.'
  FROM s_rhap_tp l FULL OUTER JOIN s_pms_tp r ON l.k = r.k
)
SELECT
  'P1.4'                                                         AS section,
  pair_label,
  left_side,
  right_side,
  left_distinct,
  right_distinct,
  matched_distinct,
  round(100.0 * matched_distinct / nullif(left_distinct, 0), 1)  AS pct_left_matched,
  round(100.0 * matched_distinct / nullif(right_distinct, 0), 1) AS pct_right_matched,
  notes
FROM pair_counts
ORDER BY pair_label;


-- -----------------------------------------------------------------------------------------------------
-- P1.5  Inside PMS: product details record and the category / subcategory / attribute lists
-- -----------------------------------------------------------------------------------------------------
-- The app translates a code on the details record of a product by looking it up in the PMS list: on the
-- list code first, and on the list record number when no code matches. Rows K52-K57 test both.
WITH
up AS (
  SELECT
    CAST(try_cast(id AS BIGINT) AS STRING)                                                   AS unique_id,
    coalesce(CAST(try_cast(try_cast(trim(CAST(ctgry_code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(ctgry_code AS STRING))), ''))                            AS ctgry_code,
    coalesce(CAST(try_cast(try_cast(trim(CAST(subctgry_code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(subctgry_code AS STRING))), ''))                         AS subctgry_code,
    coalesce(CAST(try_cast(try_cast(trim(CAST(flavor_code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(flavor_code AS STRING))), ''))                           AS flavor_code,
    coalesce(CAST(try_cast(try_cast(trim(CAST(pak_type_code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(pak_type_code AS STRING))), ''))                         AS pak_type_code
  FROM slvr_ctp_core.pms_unique_product
  WHERE coalesce(halo_is_deleted, false) = false
),
pp AS (
  SELECT
    CAST(try_cast(unique_product_id AS BIGINT) AS STRING)                                    AS unique_id,
    coalesce(CAST(try_cast(try_cast(trim(CAST(nicotine_source AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(nicotine_source AS STRING))), ''))                       AS nicotine_source
  FROM slvr_ctp_core.pms_product
  WHERE coalesce(halo_is_deleted, false) = false
),
cat AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(code AS STRING))), ''))                                  AS code,
    CAST(try_cast(id AS BIGINT) AS STRING)                                                   AS list_id
  FROM slvr_ctp_core.pms_category
  WHERE coalesce(halo_is_deleted, false) = false
),
subcat AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(code AS STRING))), ''))                                  AS code,
    CAST(try_cast(id AS BIGINT) AS STRING)                                                   AS list_id
  FROM slvr_ctp_core.pms_subcategory
  WHERE coalesce(halo_is_deleted, false) = false
),
attr AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(code AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(code AS STRING))), ''))                                  AS code,
    CAST(try_cast(id AS BIGINT) AS STRING)                                                   AS list_id
  FROM slvr_ctp_core.pms_product_attribute
  WHERE coalesce(halo_is_deleted, false) = false
),
s_pp_unique        AS (SELECT DISTINCT unique_id AS k       FROM pp),
s_up_id            AS (SELECT DISTINCT unique_id AS k       FROM up),
s_up_ctgry         AS (SELECT DISTINCT ctgry_code AS k      FROM up),
s_up_subctgry      AS (SELECT DISTINCT subctgry_code AS k   FROM up),
s_up_flavor        AS (SELECT DISTINCT flavor_code AS k     FROM up),
s_up_pak_type      AS (SELECT DISTINCT pak_type_code AS k   FROM up),
s_pp_nicotine      AS (SELECT DISTINCT nicotine_source AS k FROM pp),
s_cat_code         AS (SELECT DISTINCT code AS k            FROM cat),
s_cat_id           AS (SELECT DISTINCT list_id AS k         FROM cat),
s_subcat_code      AS (SELECT DISTINCT code AS k            FROM subcat),
s_subcat_id        AS (SELECT DISTINCT list_id AS k         FROM subcat),
s_attr_code        AS (SELECT DISTINCT code AS k            FROM attr),
s_attr_id          AS (SELECT DISTINCT list_id AS k         FROM attr),
s_attr_code_or_id  AS (SELECT code AS k FROM attr UNION SELECT list_id AS k FROM attr),
pair_counts AS (
  SELECT 'K31 PMS <-> PMS | Product vs unique product details record' AS pair_label,
         'PMS: unique product (details record) number on the product  [pms_product.unique_product_id]' AS left_side,
         'PMS: unique product record number  [pms_unique_product.id]' AS right_side,
         count(l.k) AS left_distinct, count(r.k) AS right_distinct,
         count_if(l.k IS NOT NULL AND r.k IS NOT NULL) AS matched_distinct,
         'The app reads the category, flavor, nicotine and package of a product through this link. Expect % left near 100.' AS notes
  FROM s_pp_unique l FULL OUTER JOIN s_up_id r ON l.k = r.k
  UNION ALL
  SELECT 'K52 PMS <-> PMS | Category code vs category list code (app rule, first choice)',
         'PMS: category code on the details record  [pms_unique_product.ctgry_code]',
         'PMS: category list code  [pms_category.code]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Expect % left near 100 if products store the list code. Compare with K53.'
  FROM s_up_ctgry l FULL OUTER JOIN s_cat_code r ON l.k = r.k
  UNION ALL
  SELECT 'K53 PMS <-> PMS | Category code vs category list record number (app rule, fallback)',
         'PMS: category code on the details record  [pms_unique_product.ctgry_code]',
         'PMS: category list record number  [pms_category.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'If this is high and K52 is low, products store the list record number, not the code. If both are high, codes and record numbers overlap and the app may translate some codes wrongly - please tell us which one products use.'
  FROM s_up_ctgry l FULL OUTER JOIN s_cat_id r ON l.k = r.k
  UNION ALL
  SELECT 'K54 PMS <-> PMS | Subcategory code vs subcategory list code (app rule, first choice)',
         'PMS: subcategory code on the details record  [pms_unique_product.subctgry_code]',
         'PMS: subcategory list code  [pms_subcategory.code]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Same as K52 for subcategories.'
  FROM s_up_subctgry l FULL OUTER JOIN s_subcat_code r ON l.k = r.k
  UNION ALL
  SELECT 'K55 PMS <-> PMS | Subcategory code vs subcategory list record number (app rule, fallback)',
         'PMS: subcategory code on the details record  [pms_unique_product.subctgry_code]',
         'PMS: subcategory list record number  [pms_subcategory.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Same as K53 for subcategories.'
  FROM s_up_subctgry l FULL OUTER JOIN s_subcat_id r ON l.k = r.k
  UNION ALL
  SELECT 'K56 PMS <-> PMS | Flavor code vs product attribute list code (app rule, first choice)',
         'PMS: flavor code on the details record  [pms_unique_product.flavor_code]',
         'PMS: product attribute list code  [pms_product_attribute.code]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes flavor codes are listed in the general product attribute list. Expect % left near 100. If low, flavors live in another list - please tell us which.'
  FROM s_up_flavor l FULL OUTER JOIN s_attr_code r ON l.k = r.k
  UNION ALL
  SELECT 'K57 PMS <-> PMS | Flavor code vs product attribute list record number (app rule, fallback)',
         'PMS: flavor code on the details record  [pms_unique_product.flavor_code]',
         'PMS: product attribute list record number  [pms_product_attribute.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Same as K53 for flavors.'
  FROM s_up_flavor l FULL OUTER JOIN s_attr_id r ON l.k = r.k
  UNION ALL
  SELECT 'K58 PMS <-> PMS | Package type code vs product attribute list (code or record number)',
         'PMS: package type code on the details record  [pms_unique_product.pak_type_code]',
         'PMS: product attribute list code or record number  [pms_product_attribute.code + id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The package comparison with Rhapsody translates package type codes through this list. Expect % left near 100.'
  FROM s_up_pak_type l FULL OUTER JOIN s_attr_code_or_id r ON l.k = r.k
  UNION ALL
  SELECT 'K59 PMS <-> PMS | Nicotine source vs product attribute list (code or record number)',
         'PMS: nicotine source on the product  [pms_product.nicotine_source]',
         'PMS: product attribute list code or record number  [pms_product_attribute.code + id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app translates nicotine source through this list only if it is a code. High % left = it is a code - near 0 = it is stored as words (also fine - just tell us).'
  FROM s_pp_nicotine l FULL OUTER JOIN s_attr_code_or_id r ON l.k = r.k
)
SELECT
  'P1.5'                                                         AS section,
  pair_label,
  left_side,
  right_side,
  left_distinct,
  right_distinct,
  matched_distinct,
  round(100.0 * matched_distinct / nullif(left_distinct, 0), 1)  AS pct_left_matched,
  round(100.0 * matched_distinct / nullif(right_distinct, 0), 1) AS pct_right_matched,
  notes
FROM pair_counts
ORDER BY pair_label;


-- -----------------------------------------------------------------------------------------------------
-- P1.6  Each system <-> Reporting (and the clean-up job log of the data team)
-- -----------------------------------------------------------------------------------------------------
WITH
login AS (
  SELECT nullif(upper(regexp_replace(CAST(STN AS STRING), '[\\s\\p{Z}]+', '')), '')         AS stn
  FROM slvr_ctp_core.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND NOT coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)
),
esub_link AS (
  SELECT
    nullif(upper(regexp_replace(CAST(STN AS STRING), '[\\s\\p{Z}]+', '')), '')               AS a,
    nullif(upper(regexp_replace(CAST(CHILD_STN AS STRING), '[\\s\\p{Z}]+', '')), '')         AS b
  FROM slvr_ctp_core.esub_itrac_sub_links
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_sub AS (
  SELECT nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')         AS stn
  FROM slvr_ctp_core.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_bundle AS (
  SELECT CAST(try_cast(id AS BIGINT) AS STRING)                                              AS bundle_id
  FROM slvr_ctp_core.rhapsody_bundle
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
),
rhap_comm AS (
  SELECT CAST(try_cast(id AS BIGINT) AS STRING)                                              AS letter_id
  FROM slvr_ctp_core.rhapsody_communications
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
),
rhap_bulk AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(product_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(product_id AS STRING))), ''))                            AS product_id
  FROM slvr_ctp_core.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
),
pms_prod AS (
  SELECT CAST(try_cast(id AS BIGINT) AS STRING)                                              AS pms_id
  FROM slvr_ctp_core.pms_product
  WHERE coalesce(halo_is_deleted, false) = false
),
rpt_sub AS (
  SELECT nullif(upper(regexp_replace(CAST(stn AS STRING), '[\\s\\p{Z}]+', '')), '')         AS stn
  FROM gold_ctp_edw.dim_submission
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
),
rpt_sub_any AS (
  -- The product-count check looks up the submission of a fact row in ANY version of the Reporting submission.
  SELECT CAST(try_cast(submission_sk AS BIGINT) AS STRING)                                   AS submission_sk
  FROM gold_ctp_edw.dim_submission
),
rpt_prod AS (
  SELECT
    CAST(try_cast(id AS BIGINT) AS STRING)                                                   AS product_id,
    coalesce(is_bulk_product, false)                                                         AS is_bulk
  FROM gold_ctp_edw.dim_product
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
),
rpt_fact AS (
  SELECT
    CAST(try_cast(product_id AS BIGINT) AS STRING)                                           AS product_id,
    CAST(try_cast(submission_sk AS BIGINT) AS STRING)                                        AS submission_sk
  FROM gold_ctp_edw.fact_bundle_submission_product
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
),
rpt_bundle AS (
  SELECT CAST(try_cast(bundle_id AS BIGINT) AS STRING)                                       AS bundle_id
  FROM gold_ctp_edw.dim_bundle
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
),
rpt_comm AS (
  SELECT CAST(try_cast(communication_id AS BIGINT) AS STRING)                                AS letter_id
  FROM gold_ctp_edw.dim_communication
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
),
rpt_rel AS (
  SELECT
    nullif(upper(regexp_replace(CAST(original_stn AS STRING), '[\\s\\p{Z}]+', '')), '')      AS a,
    nullif(upper(regexp_replace(CAST(related_stn AS STRING), '[\\s\\p{Z}]+', '')), '')       AS b
  FROM gold_ctp_edw.dim_submission_relationships
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
  UNION ALL
  SELECT
    nullif(upper(regexp_replace(CAST(through_stn AS STRING), '[\\s\\p{Z}]+', '')), ''),
    nullif(upper(regexp_replace(CAST(related_stn AS STRING), '[\\s\\p{Z}]+', '')), '')
  FROM gold_ctp_edw.dim_submission_relationships
  WHERE halo_is_current = true AND coalesce(halo_is_deleted, false) = false
),
stdz_log AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(rule_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(rule_id AS STRING))), ''))                               AS rule_id
  FROM slvr_ctp_dq.standardization_log
),
stdz_rules AS (
  SELECT
    coalesce(CAST(try_cast(try_cast(trim(CAST(stdz_rule_id AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING),
             nullif(upper(trim(CAST(stdz_rule_id AS STRING))), ''))                          AS rule_id
  FROM slvr_ctp_dq.standardization_rules
),
s_login_stn        AS (SELECT DISTINCT stn AS k            FROM login),
s_rhap_stn         AS (SELECT DISTINCT stn AS k            FROM rhap_sub),
s_rhap_bundle      AS (SELECT DISTINCT bundle_id AS k      FROM rhap_bundle),
s_rhap_letter      AS (SELECT DISTINCT letter_id AS k      FROM rhap_comm),
s_rhap_bulk        AS (SELECT DISTINCT product_id AS k     FROM rhap_bulk),
s_pms_id           AS (SELECT DISTINCT pms_id AS k         FROM pms_prod),
s_rpt_stn          AS (SELECT DISTINCT stn AS k            FROM rpt_sub),
s_rpt_sub_sk       AS (SELECT DISTINCT submission_sk AS k  FROM rpt_sub_any),
s_rpt_prod         AS (SELECT DISTINCT product_id AS k     FROM rpt_prod),
s_rpt_bulk_prod    AS (SELECT DISTINCT product_id AS k     FROM rpt_prod WHERE is_bulk),
s_rpt_fact_prod    AS (SELECT DISTINCT product_id AS k     FROM rpt_fact),
s_rpt_fact_sub_sk  AS (SELECT DISTINCT submission_sk AS k  FROM rpt_fact),
s_rpt_bundle       AS (SELECT DISTINCT bundle_id AS k      FROM rpt_bundle),
s_rpt_letter       AS (SELECT DISTINCT letter_id AS k      FROM rpt_comm),
s_esub_link_pair   AS (SELECT DISTINCT concat(least(a, b), '|', greatest(a, b)) AS k
                       FROM esub_link WHERE a IS NOT NULL AND b IS NOT NULL AND a <> b),
s_rpt_rel_pair     AS (SELECT DISTINCT concat(least(a, b), '|', greatest(a, b)) AS k
                       FROM rpt_rel WHERE a IS NOT NULL AND b IS NOT NULL AND a <> b),
s_stdz_log_rule    AS (SELECT DISTINCT rule_id AS k        FROM stdz_log),
s_stdz_rule        AS (SELECT DISTINCT rule_id AS k        FROM stdz_rules),
pair_counts AS (
  SELECT 'K32 Rhapsody <-> Reporting | Submission STN' AS pair_label,
         'Rhapsody: submission STN  [rhapsody_submissions.stn]' AS left_side,
         'Reporting: submission STN (current)  [dim_submission.stn]' AS right_side,
         count(l.k) AS left_distinct, count(r.k) AS right_distinct,
         count_if(l.k IS NOT NULL AND r.k IS NOT NULL) AS matched_distinct,
         'Reporting is built from Rhapsody, so expect both % near 100. A gap means submissions are missing from (or extra in) official reports.' AS notes
  FROM s_rhap_stn l FULL OUTER JOIN s_rpt_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K33 eSUB <-> Reporting | Submission STN',
         'eSUB: log-in record STN (active log-ins)  [esub_itrac_reglist.STN]',
         'Reporting: submission STN (current)  [dim_submission.stn]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Tells us whether Reporting also holds eSUB-only submissions (high % left) or only those that reached Rhapsody (similar to K06). See also P2.29.'
  FROM s_login_stn l FULL OUTER JOIN s_rpt_stn r ON l.k = r.k
  UNION ALL
  SELECT 'K34 PMS <-> Reporting | Product record number',
         'PMS: product record number  [pms_product.id]',
         'Reporting: product record number (current)  [dim_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app assumes the Reporting product is the PMS product with the same record number. Expect % left near 100 - % right is lower if Reporting also holds bulk products (K63).'
  FROM s_pms_id l FULL OUTER JOIN s_rpt_prod r ON l.k = r.k
  UNION ALL
  SELECT 'K35 Reporting <-> Reporting | Bundle-submission-product facts vs products',
         'Reporting: product ID on bundle-submission-product facts (current)  [fact_bundle_submission_product.product_id]',
         'Reporting: product record number (current)  [dim_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Expect % left near 100: every product in the facts should exist in the Reporting product list.'
  FROM s_rpt_fact_prod l FULL OUTER JOIN s_rpt_prod r ON l.k = r.k
  UNION ALL
  SELECT 'K36 Reporting <-> PMS | Bundle-submission-product facts vs PMS products',
         'Reporting: product ID on bundle-submission-product facts (current)  [fact_bundle_submission_product.product_id]',
         'PMS: product record number  [pms_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Confirms that the product ID Reporting uses is the PMS record number. If high while K23 is low, Rhapsody stores something else and Reporting translates it - tell us how.'
  FROM s_rpt_fact_prod l FULL OUTER JOIN s_pms_id r ON l.k = r.k
  UNION ALL
  SELECT 'K37 Rhapsody <-> Reporting | Bundle ID',
         'Rhapsody: bundle ID (not deleted)  [rhapsody_bundle.id]',
         'Reporting: bundle ID (current, not deleted)  [dim_bundle.bundle_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Reporting bundles are built from Rhapsody bundles, so expect both % near 100.'
  FROM s_rhap_bundle l FULL OUTER JOIN s_rpt_bundle r ON l.k = r.k
  UNION ALL
  SELECT 'K60 Rhapsody <-> Reporting | Letter ID',
         'Rhapsody: letter/communication record number (not deleted)  [rhapsody_communications.id]',
         'Reporting: communication ID (current, not deleted)  [dim_communication.communication_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "letters missing from Reporting" check matches letters on this ID. Expect both % near 100.'
  FROM s_rhap_letter l FULL OUTER JOIN s_rpt_letter r ON l.k = r.k
  UNION ALL
  SELECT 'K61 Reporting <-> Reporting | Bundle-submission-product facts vs submissions',
         'Reporting: submission key on bundle-submission-product facts (current)  [fact_bundle_submission_product.submission_sk]',
         'Reporting: submission key (any version)  [dim_submission.submission_sk]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The product-count check finds the STN of each fact row through this key. Expect % left near 100.'
  FROM s_rpt_fact_sub_sk l FULL OUTER JOIN s_rpt_sub_sk r ON l.k = r.k
  UNION ALL
  SELECT 'K62 eSUB <-> Reporting | Submission links (either direction)',
         'eSUB: submission link STN pair, in either order  [esub_itrac_sub_links.STN + CHILD_STN]',
         'Reporting: relationship STN pair, original or through STN with related STN, in either order (current)  [dim_submission_relationships]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "links missing from Reporting" check matches links this way. Expect % left near 100 - P2.10 shows which link types are missing. % right is lower because Reporting also adds multi-level links.'
  FROM s_esub_link_pair l FULL OUTER JOIN s_rpt_rel_pair r ON l.k = r.k
  UNION ALL
  SELECT 'K63 Reporting <-> PMS | Reporting bulk products vs PMS products',
         'Reporting: product record number of products marked bulk (current)  [dim_product.id where is_bulk_product]',
         'PMS: product record number  [pms_product.id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The app does not flag Reporting bulk products that are missing from PMS, assuming they come from the Rhapsody bulk list instead. Expect % left near 0. If it is high, bulk products ARE PMS products and should be checked - please tell us.'
  FROM s_rpt_bulk_prod l FULL OUTER JOIN s_pms_id r ON l.k = r.k
  UNION ALL
  SELECT 'K64 Reporting <-> Rhapsody | Reporting bulk products vs Rhapsody bulk product list',
         'Reporting: product record number of products marked bulk (current)  [dim_product.id where is_bulk_product]',
         'Rhapsody: bulk-submission product ID  [rhapsody_bulk_products.product_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'Expect % left near 100 if Reporting bulk products come from the Rhapsody bulk list (the counterpart of K63).'
  FROM s_rpt_bulk_prod l FULL OUTER JOIN s_rhap_bulk r ON l.k = r.k
  UNION ALL
  SELECT 'K65 Clean-up log <-> clean-up rules | Rule number',
         'Data team: rule number on clean-up job runs  [slvr_ctp_dq.standardization_log.rule_id]',
         'Data team: clean-up rule number  [slvr_ctp_dq.standardization_rules.stdz_rule_id]',
         count(l.k), count(r.k), count_if(l.k IS NOT NULL AND r.k IS NOT NULL),
         'The "clean-up job failed" checks name each rule and decide its system from the rule list. Expect % left near 100.'
  FROM s_stdz_log_rule l FULL OUTER JOIN s_stdz_rule r ON l.k = r.k
)
SELECT
  'P1.6'                                                         AS section,
  pair_label,
  left_side,
  right_side,
  left_distinct,
  right_distinct,
  matched_distinct,
  round(100.0 * matched_distinct / nullif(left_distinct, 0), 1)  AS pct_left_matched,
  round(100.0 * matched_distinct / nullif(right_distinct, 0), 1) AS pct_right_matched,
  notes
FROM pair_counts
ORDER BY pair_label;


-- =====================================================================================================
-- PART 2: What do the status, flag and code values mean?  (P2.01 to P2.33)
-- =====================================================================================================
-- Each result lists every different value of one field (or the top values, where it says so), how many
-- records have it, and (in app_currently_reads_as) how the app interprets that value today.
--
-- PLEASE: for each value, confirm or correct app_currently_reads_as. That is the most useful thing
-- you can send back. Where it says "(no rule yet: shown as-is)", a one-line meaning per value helps.
-- (The readings were copied from the rules of the app on 2026-10-21 - if the app changes, update them here.)
--
-- Value display: "(blank - no value)" = nothing stored, "(blank - empty text)" = only spaces stored,
-- "[has extra spaces]" = the value has spaces at the start or end (the app ignores those).
-- Upper/lower-case versions of the same word show as separate rows - the app treats them as the same.
-- =====================================================================================================

-- ------------------------------------------- eSUB ----------------------------------------------------

-- P2.01  eSUB: is a log-in record inactive?  The app leaves inactive log-ins out of most checks.
SELECT
  'P2.01' AS section, 'eSUB' AS system,
  'Log-in record inactive flag  [esub_itrac_reglist.INACTIVE_IND]' AS field,
  CASE WHEN v IS NULL THEN '(blank - no value)' WHEN trim(v) = '' THEN '(blank - empty text)'
       WHEN v <> trim(v) THEN concat(v, '  [has extra spaces]') ELSE v END AS value,
  records,
  round(100.0 * records / sum(records) OVER (), 2) AS pct_of_records,
  CASE WHEN upper(trim(v)) IN ('Y', 'YES', '1', 'T', 'TRUE') THEN 'Inactive (withdrawn, voided or replaced): left out'
       ELSE 'Active: included' END AS app_currently_reads_as
FROM (
  SELECT CAST(INACTIVE_IND AS STRING) AS v, count(*) AS records
  FROM slvr_ctp_core.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY CAST(INACTIVE_IND AS STRING)
) t
ORDER BY records DESC;

-- P2.02  eSUB: log-in record submission status (number, with its name from the eSUB status list).
--        Context only: no check uses this field yet, but the meanings help us read P2.01 and P2.08.
SELECT
  'P2.02' AS section, 'eSUB' AS system,
  'Log-in record submission status  [esub_itrac_reglist.SUBMISSION_STATUS_KEY - name from brnz_esub_itrac.itrac_status]' AS field,
  CASE WHEN t.v IS NULL THEN '(blank - no value)'
       ELSE concat(t.v, ' - ', coalesce(n.status_name, '(not on the eSUB status list)')) END AS value,
  t.records,
  round(100.0 * t.records / sum(t.records) OVER (), 2) AS pct_of_records,
  t.inactive_records,
  '(no rule yet: shown as-is)' AS app_currently_reads_as
FROM (
  SELECT
    CAST(try_cast(SUBMISSION_STATUS_KEY AS BIGINT) AS STRING) AS v,
    count(*) AS records,
    count_if(coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)) AS inactive_records
  FROM slvr_ctp_core.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY CAST(try_cast(SUBMISSION_STATUS_KEY AS BIGINT) AS STRING)
) t
LEFT JOIN (
  -- The status list only exists as the raw (bronze) copy - take the latest load of each status.
  SELECT CAST(try_cast(STATUS_KEY AS BIGINT) AS STRING) AS k, nullif(trim(STATUS), '') AS status_name
  FROM brnz_esub_itrac.itrac_status
  QUALIFY row_number() OVER (PARTITION BY CAST(try_cast(STATUS_KEY AS BIGINT) AS STRING)
                             ORDER BY halo_insert_ts DESC NULLS LAST, UPDATE_DATE DESC NULLS LAST) = 1
) n ON n.k = t.v
ORDER BY t.records DESC;

-- P2.03  eSUB: log-in record document type (active log-ins), with its name from the eSUB document type list.
--        active_ind_values = every ACTIVE_IND value stored for that type (a type can have several rows).
SELECT
  'P2.03' AS section, 'eSUB' AS system,
  'Log-in record document type  [esub_itrac_reglist.DOC_TYPES_KEY - name and ACTIVE_IND from esub_itrac_doc_types]' AS field,
  CASE WHEN t.v IS NULL THEN '(blank - no value)'
       ELSE concat(t.v, ' - ', coalesce(d.type_name, '(not on the document type list)')) END AS value,
  t.records,
  round(100.0 * t.records / sum(t.records) OVER (), 2) AS pct_of_records,
  d.active_ind_values,
  CASE WHEN t.v IS NULL THEN 'Blank document type: flagged'
       WHEN d.k IS NULL THEN 'Not on the document type list: flagged'
       WHEN d.is_active = 0 THEN 'Retired type (every copy has ACTIVE_IND N/NO/0/F/FALSE): flagged on log-ins after it was retired'
       ELSE 'Current document type' END AS app_currently_reads_as
FROM (
  SELECT CAST(try_cast(DOC_TYPES_KEY AS BIGINT) AS STRING) AS v, count(*) AS records
  FROM slvr_ctp_core.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND NOT coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)
  GROUP BY CAST(try_cast(DOC_TYPES_KEY AS BIGINT) AS STRING)
) t
LEFT JOIN (
  SELECT
    CAST(try_cast(DOC_TYPES_KEY AS BIGINT) AS STRING) AS k,
    concat_ws(' / ', max(nullif(trim(DOC_TYPE), '')), max(nullif(trim(CODE), ''))) AS type_name,
    max(CASE WHEN coalesce(upper(trim(CAST(ACTIVE_IND AS STRING))) IN ('N', 'NO', '0', 'F', 'FALSE'), false)
             THEN 0 ELSE 1 END) AS is_active,
    array_join(array_sort(collect_set(coalesce(CAST(ACTIVE_IND AS STRING), '(blank)'))), ', ') AS active_ind_values
  FROM slvr_ctp_core.esub_itrac_doc_types
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY CAST(try_cast(DOC_TYPES_KEY AS BIGINT) AS STRING)
) d ON d.k = t.v
ORDER BY t.records DESC;

-- P2.04  eSUB: is an electronic submission deleted?
SELECT
  'P2.04' AS section, 'eSUB' AS system,
  'Electronic submission deleted flag  [esub_tob_submission.DELETED_IND]' AS field,
  CASE WHEN v IS NULL THEN '(blank - no value)' WHEN trim(v) = '' THEN '(blank - empty text)'
       WHEN v <> trim(v) THEN concat(v, '  [has extra spaces]') ELSE v END AS value,
  records,
  round(100.0 * records / sum(records) OVER (), 2) AS pct_of_records,
  CASE WHEN upper(trim(v)) IN ('Y', 'YES', '1', 'T', 'TRUE') THEN 'Deleted: left out'
       ELSE 'Not deleted: included' END AS app_currently_reads_as
FROM (
  SELECT CAST(DELETED_IND AS STRING) AS v, count(*) AS records
  FROM slvr_ctp_core.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY CAST(DELETED_IND AS STRING)
) t
ORDER BY records DESC;

-- P2.05  eSUB: is an electronic submission inactive?
SELECT
  'P2.05' AS section, 'eSUB' AS system,
  'Electronic submission inactive flag  [esub_tob_submission.INACTIVE_IND]' AS field,
  CASE WHEN v IS NULL THEN '(blank - no value)' WHEN trim(v) = '' THEN '(blank - empty text)'
       WHEN v <> trim(v) THEN concat(v, '  [has extra spaces]') ELSE v END AS value,
  records,
  round(100.0 * records / sum(records) OVER (), 2) AS pct_of_records,
  CASE WHEN upper(trim(v)) IN ('Y', 'YES', '1', 'T', 'TRUE') THEN 'Inactive: left out'
       ELSE 'Active: included' END AS app_currently_reads_as
FROM (
  SELECT CAST(INACTIVE_IND AS STRING) AS v, count(*) AS records
  FROM slvr_ctp_core.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
  GROUP BY CAST(INACTIVE_IND AS STRING)
) t
ORDER BY records DESC;

-- P2.06  eSUB: is an electronic submission on hold?  (electronic submissions not deleted and not inactive)
SELECT
  'P2.06' AS section, 'eSUB' AS system,
  'Electronic submission on-hold flag  [esub_tob_submission.HOLD_IND]' AS field,
  CASE WHEN v IS NULL THEN '(blank - no value)' WHEN trim(v) = '' THEN '(blank - empty text)'
       WHEN v <> trim(v) THEN concat(v, '  [has extra spaces]') ELSE v END AS value,
  records,
  round(100.0 * records / sum(records) OVER (), 2) AS pct_of_records,
  CASE WHEN upper(trim(v)) IN ('Y', 'YES', '1', 'T', 'TRUE')
         THEN 'On hold: not expected to be logged in until the hold is lifted'
       ELSE 'Not on hold: expected to be logged in within 3 days' END AS app_currently_reads_as
FROM (
  SELECT CAST(HOLD_IND AS STRING) AS v, count(*) AS records
  FROM slvr_ctp_core.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
    AND NOT coalesce(upper(trim(CAST(DELETED_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)
    AND NOT coalesce(upper(trim(CAST(INACTIVE_IND AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)
  GROUP BY CAST(HOLD_IND AS STRING)
) t
ORDER BY records DESC;

-- P2.30  Data team clean-up job log: run status values (all runs). The app compares the status with
--        everything except letters removed, upper-cased.
SELECT
  'P2.30' AS section, 'Data team' AS system,
  'Clean-up job run status  [slvr_ctp_dq.standardization_log.execution_status]' AS field,
  CASE WHEN v IS NULL THEN '(blank - no value)' WHEN trim(v) = '' THEN '(blank - empty text)'
       WHEN v <> trim(v) THEN concat(v, '  [has extra spaces]') ELSE v END AS value,
  runs,
  CAST(last_run AS DATE) AS last_seen,
  CASE WHEN code IN ('SUCCESS', 'SUCCEEDED', 'SUCCESSFUL', 'COMPLETED', 'COMPLETE', 'OK', 'PASS', 'PASSED', 'DONE', 'FINISHED')
         THEN 'Succeeded'
       WHEN code IN ('RUNNING', 'INPROGRESS', 'STARTED', 'PENDING', 'QUEUED', 'PROCESSING')
         THEN 'Still running (flagged as "never finished" after 1 day)'
       ELSE 'Failed: flagged' END AS app_currently_reads_as
FROM (
  SELECT
    CAST(execution_status AS STRING)                                       AS v,
    regexp_replace(upper(coalesce(execution_status, '')), '[^A-Z]', '')    AS code,
    count(*)                                                               AS runs,
    max(execution_timestamp)                                               AS last_run
  FROM slvr_ctp_dq.standardization_log
  GROUP BY CAST(execution_status AS STRING), regexp_replace(upper(coalesce(execution_status, '')), '[^A-Z]', '')
) t
ORDER BY runs DESC;
