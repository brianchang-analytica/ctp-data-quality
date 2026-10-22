"""Reference example for check authors. Not registered in GROUP_MODULES."""

from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check

CHECKS: list[Check] = [
    Check(
        id="EXAMPLE_RHAP_SUB_RECEIVED_BEFORE_SUBMITTED",
        title="Submissions received before they were submitted",
        systems=("rhapsody",),
        entity="Submissions",
        dimension="Timeliness",
        severity="High",
        question="Is every submission's received date on or after the date the company submitted it?",
        why="Received date starts the review clock. A received date earlier than the submitted date "
            "usually means a typo, and it makes review-time metrics look shorter than they really were.",
        how_to_fix="Open the submission in Rhapsody and confirm both dates against the eSUB receipt. "
                   "Correct whichever date was keyed wrong.",
        record_label="submissions",
        filters=frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE}),
        date_label="Received date",
        technical_note="slvr_ctp_core.rhapsody_submissions; date_received < date_submitted. Soft-deleted rows excluded.",
        sql=r"""
WITH s AS (
  SELECT
    trim(stn)                     AS stn,
    date_received,
    date_submitted,
    stdz_submission_type,
    organization,
    coalesce(abpc_company_name, itrac_company, abc_company) AS company
  FROM ${core}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
)
SELECT
  CAST(stn AS STRING)                                    AS record_key,
  CAST(coalesce(date_received, date_submitted) AS DATE)  AS record_date,
  CAST(stdz_submission_type AS STRING)                   AS submission_type,
  CAST(organization AS STRING)                           AS office,
  coalesce(date_received < date_submitted, false)        AS is_issue,
  CASE WHEN date_received < date_submitted THEN 'Received before submitted' END AS issue_category,
  CASE WHEN date_received < date_submitted THEN
    concat('Received ', CAST(date_received AS STRING), ', which is ',
           CAST(datediff(date_submitted, date_received) AS STRING),
           ' day(s) before it was submitted (', CAST(date_submitted AS STRING), ').')
  END                                                    AS issue_reason,
  stn                                                    AS `STN`,
  company                                                AS `Company`,
  date_submitted                                         AS `Submitted date`,
  date_received                                          AS `Received date`,
  stdz_submission_type                                   AS `Submission type`
FROM s
""",
    ),
]
