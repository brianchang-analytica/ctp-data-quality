"""'Trace a submission': look one STN up in every system, side by side.

Each TraceQuery takes the steward's STN as the named parameter `:stn`
(upper-cased with all whitespace removed by normalize_stn) and returns rows with friendly
column names. layout="record" shows the first row as a card of field/value
pairs (for things there should be exactly one of, like the submission
itself); layout="table" shows all rows (products, bundles, letters, links).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from dq.model import SYSTEMS
from dq.settings import SETTINGS

_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")


@dataclass(frozen=True)
class TraceQuery:
    id: str
    system: str
    title: str
    description: str
    sql: str
    layout: str = "record"  # "record" | "table"
    # Fields that should be identical across systems; the app highlights differences.
    # Maps a shared concept label (e.g. "Received date") -> this query's column alias.
    compare: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.system not in SYSTEMS:
            raise ValueError(f"TraceQuery {self.id}: unknown system {self.system}")
        if self.layout not in {"record", "table"}:
            raise ValueError(f"TraceQuery {self.id}: layout must be record or table")
        if ":stn" not in self.sql:
            raise ValueError(f"TraceQuery {self.id}: must filter on the :stn parameter")

    def render_sql(self, placeholders: dict[str, str] | None = None) -> str:
        from dq.sqlfrag import expand

        ph = placeholders or SETTINGS.placeholders()
        return _PLACEHOLDER.sub(lambda m: ph[m.group(1)], expand(self.sql)).strip().rstrip(";")


# --------------------------------------------------------------------------- #
# Trace queries
#
# Notes for engineers (stewards never see this block):
#  * Normalization uses the shared macros in dq/sqlfrag.py, exactly as the checks do: STN_KEY() for every
#    STN (upper-cased, all whitespace incl. non-breaking removed -- the same thing normalize_stn() does to
#    what the steward types), NUM_KEY() for numeric IDs, ANY_KEY() for IDs stored as text, BLANK() for
#    display values, TXT() for comparing text, FLAG_ON() for Y/N flags. The only module-local helpers are
#    display formatters the macros do not cover (_yn, _unless, _person, _company, _status). Keys are therefore
#    always compared as normalized strings, so one bad value can never fail a query under ANSI mode.
#  * Soft deletes: silver rows with halo_is_deleted are dropped (esub_submission_amendment_relations has
#    no halo_is_deleted column, so nothing can be dropped there); gold rows must be halo_is_current and not
#    halo_is_deleted (exception, matching RPT_RHAP_SUBMISSION_PRODUCT_COUNTS_DIFFER: the STN's
#    submission_sk values are taken from ANY dim_submission version, because facts may point at an older
#    version's key); Rhapsody temporary copies are dropped; Rhapsody bundles that are deleted are kept and
#    labelled "deleted in Rhapsody" (a live row wins), so "deleted" can be told apart from "never existed";
#    deleted Rhapsody letters are dropped; eSUB DELETED_IND rows are dropped. eSUB INACTIVE_IND rows are
#    deliberately KEPT and shown with an "Inactive" field, so a steward can tell "inactivated in eSUB"
#    apart from "never logged in"; active rows sort first.
#  * layout="record" queries are NOT de-duplicated: the page warns when more than one row matches, which is
#    itself a data problem. They are ordered so the row the checks use is shown first: Rhapsody / Reporting
#    = latest copy; eSUB log-in = the same pick as the eSUB <-> Rhapsody checks (active first, then the one
#    Rhapsody's reglist_key points to, then latest updated). Table queries de-duplicate the many side in
#    CTEs before joining, so they cannot fan out; every lookup joined to them is one row per key.
#  * Rhapsody products for an STN = its three product lists, exactly as RHAP_SUB_NO_PRODUCTS and
#    ESUB_RHAP_PRODUCT_COUNT_DIFFERS count them: rhapsody_submission_products ("regular"),
#    rhapsody_bulk_products ("bulk") and rhapsody_submission_product_wrapper ("review", temporary copies
#    excluded). Reporting product count = distinct NUM_KEY(product_id) (else '#' || product_sk), exactly as
#    RPT_RHAP_SUBMISSION_PRODUCT_COUNTS_DIFFER counts it.
#  * Rhapsody <-> PMS product match. Confirmed on real data (Oct 2026): a Rhapsody product link's own product_id
#    equals pms_product.uuid for 100% of products (K24, K50); pms_product.id matches 0% (K22, K23, K29, K49).
#    The trace matches each link's ANY_KEY(product_id) against the PMS_PRODUCT_REFS fields imported from
#    dq/checks/rhapsody_pms.py (uuid), so it always agrees with those checks; nothing here assumes pms_product.id
#    is a match field.
#  * Rhapsody -> Reporting. Confirmed on real data (Oct 2026): Reporting product IDs are PMS ids (dim_product.id =
#    pms_product.id 100%, K34; facts use the same id, K35, K36), and Rhapsody product IDs match dim_product.id 0%
#    (K64). So a Rhapsody product is "In Reporting" when its product ID equals a PMS_PRODUCT_REFS value
#    (dim_product.uuid) of one of the STN's Reporting products, and a Reporting product is "Linked in Rhapsody"
#    the same way. The Reporting product ID is never compared with the Rhapsody product ID.
#  * Placeholder IDs. Confirmed on real data (Oct 2026, P4 U07, U10): one Rhapsody product_id sits on 14,359
#    product rows and one pms_product.uuid on 14,368 products; every other product ID and uuid is unique. A
#    value on more than PLACEHOLDER_ID_ROWS (imported, 100) rows of Rhapsody's product copy (regular + bulk
#    lists, live rows) or live PMS products is a placeholder (CTE ph, found from the data on every run). It is
#    never matched, de-duplicated or used to pick product details: the product traces show it once, as
#    "Placeholder ID", instead of pairing it with an arbitrary product. Counts are unaffected: the submission
#    card counts it once per STN, as ESUB_RHAP_PRODUCT_COUNT_DIFFERS does.
#  * The at-a-glance grid takes, per system, the FIRST query in this list that carries a detail, and lists
#    details in the order first seen. Rhapsody's submission record comes first because it carries every
#    shared detail; each system's own record query is the first query of that system.
#  * Shared details (identical labels everywhere): Received date, Submitted date, Company, Submission type,
#    Status, Product count, Bundle count, Organization, Project manager, eSUB log-in ID.
#  * "Organization" = Rhapsody / Reporting `organization`. Confirmed on real data (Oct 2026, P2.17): it is the
#    applicant company (3,679 distinct names such as "Philip Morris USA Inc."), so every card labels it
#    Organization.
#  * "Status" = submission_status. Confirmed on real data (Oct 2026, P2.14): Rhapsody holds '1' (16%), '0' (8.5%)
#    or blank (75%); it is shown as 'Open (1)' / 'Closed (0)', blank stays blank, any other code as stored.
#    Reporting's copy gets the same display, so the grid compares like with like.
#  * Letters. Confirmed on real data (Oct 2026): uploadcommunicationstobundle_id reaches 97.4% of bundles (K21)
#    and bundleid 75% (K19, K20), so a letter belongs to the STN's bundles when EITHER ID matches one.
#  * eSUB links. Confirmed on real data (Oct 2026): esub_itrac_reglist.STN_LINK is not an STN (0 of 120,700
#    values match any STN, K39) and esub_tob_submission.STN_BEING_AMENDED never yields an STN when split
#    (K40), so neither is listed or shown as an STN.
#  * eSUB log-in. Confirmed on real data (Oct 2026): log-in STN = electronic submission
#    SUBMISSION_TRACKING_NUMBER 99.8% (K02); log-in TRACKING_NUMBER is a different number (K01); PACKAGE_KEY
#    links 100% of electronic submissions to a log-in (K03); Rhapsody reglist_key = REGLIST_KEY 100% (K09).
#    About 2,900 Rhapsody STNs exist only on inactive log-ins (K06, K08), which is why inactive rows are kept.
#  * PMS code lists. Confirmed on real data (Oct 2026): category and subcategory link by code, never by record
#    number (K52-K59, P2.25); each category code has an active and an inactive entry (U12), and the live, active
#    entry's name is shown.
#  * Row caps: every list has a LIMIT; lists that can realistically reach it carry a count(*) OVER () total
#    (window functions are evaluated before LIMIT, so it is the full number).
#
# Assumptions (not yet confirmed with the data owners):
#  * A blank Rhapsody submission_status means the status was not judged.
#  * eSUB log-in NAME is the company, SUBMITTER_NAME the person who sent it, PRODUCT_COUNT the number of products
#    listed at log-in.
#  * eSUB's document type (esub_itrac_doc_types) is NOT compared with Rhapsody's standard submission type:
#    the two use different wording (ESUB_RHAP_TYPE_UNUSUAL has to learn the usual pairing), so comparing them
#    would show "Differs" on almost every STN. It is shown on the eSUB card only.
#  * Grid "Company" for Rhapsody / Reporting = abpc_company_name, else itrac_company, else abc_company, else
#    mvw_submission_org_company (same order as the Rhapsody and Reporting checks). eSUB's NAME may match the
#    iTRAC company rather than the registered one, so both are shown on the Rhapsody / Reporting cards; the
#    eSUB <-> Rhapsody company check accepts a match on any of them.
#  * The electronic submission's files are esub_submission_document rows with the same SUBMISSION_KEY, plus
#    esub_document rows that share the PACKAGE_KEY of the electronic submission or of the log-in record.
#    FILE_SIZE is in bytes.
#  * esub_itrac_sub_links: STN is the parent and CHILD_STN the child. esub_submission_amendment_relations
#    holds comma/semicolon/space separated STN lists.
#  * The product wrapper's tobundle_id is the review bundle the product sits in.
#  * rhpm/author may be a directory name such as 'cn=jdoe,...'; the cn part is shown.
#  * PMS company = pms_unique_product.org_name (the applicant, not the manufacturer).
#  * Free-text error fields holding N, NO, NONE, SUCCESS, OK, ... mean "no problem"; a rejection reason of
#    N/A, NONE, ... means "not rejected" (same word lists as dq/checks/rhapsody.py).
# --------------------------------------------------------------------------- #

try:  # the product match and the placeholder rule are defined once, in the Rhapsody <-> PMS checks
    from dq.checks.rhapsody_pms import PLACEHOLDER_ID_ROWS, PMS_PRODUCT_REFS, RHAPSODY_PRODUCT_REF
except ImportError:  # pragma: no cover - keep the trace page working if those names move
    RHAPSODY_PRODUCT_REF, PMS_PRODUCT_REFS, PLACEHOLDER_ID_ROWS = "product_id", ("uuid",), 100
if RHAPSODY_PRODUCT_REF != "product_id":  # confirmed on real data (K24): the link's own product_id is the reference
    raise ValueError("dq/lookup.py matches each Rhapsody product link on its own product_id (confirmed K24); "
                     f"update the product traces before setting RHAPSODY_PRODUCT_REF = {RHAPSODY_PRODUCT_REF!r}")

_NO_WORDS = "('N', 'NO', '0', 'F', 'FALSE')"
# Same word lists as dq/checks/rhapsody.py (compared after TXT()).
_NO_ERROR = "('N', 'NO', 'FALSE', 'F', '0', 'NONE', 'NULL', 'N/A', 'NA', 'SUCCESS', 'SUCCESSFUL', 'OK', '-')"
_NOT_REJECTED = "('N/A', 'NA', 'NONE', 'NULL', '-', '0', 'N', 'NO', 'FALSE', 'F')"


def _yn(x: str, blank: str = "'No'") -> str:
    """Y/N-style flag shown as Yes / No. Blank shows `blank` ('No' for "is it set?" flags, where the checks
    also read blank as not set; pass "CAST(NULL AS STRING)" when blank means unknown). Codes that are neither
    yes nor no are shown as stored."""
    return (f"CASE WHEN FLAG_ON({x}) THEN 'Yes' WHEN BLANK({x}) IS NULL THEN {blank} "
            f"WHEN TXT({x}) IN {_NO_WORDS} THEN 'No' ELSE BLANK({x}) END")


def _unless(x: str, words: str) -> str:
    """Display text, blank when it only says 'nothing' (one of `words`, compared after TXT())."""
    return f"CASE WHEN TXT({x}) IN {words} THEN CAST(NULL AS STRING) ELSE BLANK({x}) END"


def _person(x: str) -> str:
    """Readable name from a directory name like 'cn=jdoe,ou=...' (else the value as stored)."""
    return f"coalesce(BLANK(regexp_extract(BLANK({x}), '(?i)cn=([^,]+)', 1)), BLANK({x}))"


def _company(a: str) -> str:
    """Company on a Rhapsody / Reporting submission row aliased `a` (same order as the checks)."""
    return (f"coalesce(BLANK({a}.abpc_company_name), BLANK({a}.itrac_company), BLANK({a}.abc_company), "
            f"BLANK({a}.mvw_submission_org_company))")


def _status(x: str) -> str:
    """Submission status code shown in words: '1' -> 'Open (1)', '0' -> 'Closed (0)' (P2.14); blank stays blank,
    any other code is shown as stored."""
    return f"CASE TXT({x}) WHEN '1' THEN 'Open (1)' WHEN '0' THEN 'Closed (0)' ELSE BLANK({x}) END"


# Shared details for the two submission records (Rhapsody and Reporting carry the same fields).
_SUBMISSION_COMPARE = tuple((c, c) for c in (
    "Received date", "Submitted date", "Company", "Submission type", "Status", "Product count",
    "Bundle count", "Organization", "Project manager", "eSUB log-in ID",
))

# --------------------------------------------------------------------------- CTE building blocks

# Every product link Rhapsody holds for the STN, from its three product lists (one row per list entry).
_RHAP_LINKS = r"""
links AS (
  SELECT STN_KEY(stn) AS stn_n, ANY_KEY(product_id) AS pid, BLANK(product_id) AS product_id,
         ANY_KEY(bundle_id) AS bid, BLANK(bundle_name) AS bundle_name, 'Regular list' AS src
  FROM ${core}.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) = :stn
  UNION ALL
  SELECT STN_KEY(stn), ANY_KEY(product_id), BLANK(product_id),
         NUM_KEY(bundle_id), CAST(NULL AS STRING), 'Bulk list'
  FROM ${core}.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) = :stn
  UNION ALL
  SELECT STN_KEY(untitled_relationship_stn), ANY_KEY(untitled_relationship_product_id),
         BLANK(untitled_relationship_product_id), NUM_KEY(tobundle_id), CAST(NULL AS STRING), 'Review list'
  FROM ${core}.rhapsody_submission_product_wrapper
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
    AND STN_KEY(untitled_relationship_stn) = :stn
)"""


def _bund(ids: str, extra: str = "") -> str:
    """Rhapsody bundles whose NUM_KEY(id) is in `ids`, one row per ID: temporary copies and removed rows
    ignored, a live (not deleted) row wins, is_live = 0 when only deleted rows exist (as in the Rhapsody
    checks). `extra` adds columns (leading comma)."""
    return r"""
bund AS (
  SELECT NUM_KEY(id) AS bid,
         CASE WHEN coalesce(deleted, false) THEN 0 ELSE 1 END AS is_live,
         BLANK(bundlename) AS bundle_name""" + extra + r"""
  FROM ${core}.rhapsody_bundle
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
    AND NUM_KEY(id) IN (""" + ids + r""")
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(id)
                             ORDER BY CASE WHEN coalesce(deleted, false) THEN 1 ELSE 0 END,
                                      halo_update_ts DESC NULLS LAST, s_lastmodifieddate DESC NULLS LAST) = 1
)"""


# CTE rk: every product ID Rhapsody links to the STN (any of its three lists), one row each (needs `links`).
# Confirmed K24: the link's own product ID is the value PMS holds as its uuid.
_RK = r"""
rk AS (
  SELECT DISTINCT pid AS match_key FROM links WHERE pid IS NOT NULL
)"""


def _placeholders(keys: str) -> str:
    """CTE ph: the values in `keys` (a subquery of ANY_KEY product IDs / PMS match values) that are placeholder
    IDs, one row each, with the number of products sharing it (n_rows). A placeholder is on more than
    PLACEHOLDER_ID_ROWS live rows of Rhapsody's product copy (regular + bulk lists, as rhap_placeholder in
    dq/checks/rhapsody_pms.py) or on more than PLACEHOLDER_ID_ROWS live PMS products in one PMS_PRODUCT_REFS
    field (as pms_keys_all.n_sharing there)."""
    pms = "\n      UNION ALL\n".join(
        "      SELECT " + str(i) + " AS key_rank, ANY_KEY(" + f + ") AS match_key, NUM_KEY(id) AS pms_id\n"
        "      FROM ${core}.pms_product\n"
        "      WHERE coalesce(halo_is_deleted, false) = false\n"
        "        AND ANY_KEY(" + f + ") IN (" + keys + ")"
        for i, f in enumerate(PMS_PRODUCT_REFS, 1)
    )
    return r"""
ph AS (
  SELECT match_key, max(n_rows) AS n_rows
  FROM (
    SELECT match_key, count(*) AS n_rows
    FROM (
      SELECT ANY_KEY(product_id) AS match_key
      FROM ${core}.rhapsody_products
      WHERE coalesce(halo_is_deleted, false) = false
      UNION ALL
      SELECT ANY_KEY(product_id)
      FROM ${core}.rhapsody_bulk_products
      WHERE coalesce(halo_is_deleted, false) = false
    ) c
    WHERE match_key IN (""" + keys + r""")
    GROUP BY match_key
    UNION ALL
    SELECT match_key, count(DISTINCT pms_id)
    FROM (
""" + pms + r"""
    ) p
    GROUP BY key_rank, match_key
  ) u
  GROUP BY match_key
  HAVING max(n_rows) > """ + str(PLACEHOLDER_ID_ROWS) + r"""
)"""


def _pms_match(keys: str) -> str:
    """CTE pm: one live PMS product per match value in `keys` (a subquery that already leaves placeholder IDs
    out), matched on PMS_PRODUCT_REFS in order, as in dq/checks/rhapsody_pms.py (pms_match)."""
    branches = "\n    UNION ALL\n".join(
        r"""    SELECT ANY_KEY(""" + f + r""") AS match_key, """ + str(i) + r""" AS key_rank, id AS pms_row_id,
           NUM_KEY(id) AS pms_id, BLANK(stdz_stn) AS pms_stn, STN_KEY(stdz_stn) AS pms_stn_n,
           update_date, halo_update_ts
    FROM ${core}.pms_product
    WHERE coalesce(halo_is_deleted, false) = false
      AND ANY_KEY(""" + f + r""") IN (""" + keys + ")"
        for i, f in enumerate(PMS_PRODUCT_REFS, 1)
    )
    return r"""
pm AS (
  SELECT match_key, pms_id, pms_stn, pms_stn_n
  FROM (
""" + branches + r"""
  ) u
  QUALIFY row_number() OVER (PARTITION BY match_key
                             ORDER BY key_rank, update_date DESC NULLS LAST, halo_update_ts DESC NULLS LAST,
                                      pms_row_id DESC) = 1
)"""


_REF_NUMS = range(1, len(PMS_PRODUCT_REFS) + 1)

# CTE ru: the PMS reference values (PMS_PRODUCT_REFS fields of dim_product, i.e. its uuid) of the STN's Reporting
# products, one row per value. Reporting product IDs are PMS ids (K34) and never equal a Rhapsody product ID
# (K64), so this is how a Rhapsody product reaches Reporting. rf = the STN's current, non-deleted product links on
# any version of the submission; rd = the matching non-deleted dim_product rows (the current row with the link's
# product id, or the version the link points at), as in the edw_products trace.
_RPT_REFS = r"""
rf AS (
  SELECT NUM_KEY(product_id) AS product_id, product_sk
  FROM ${edw}.fact_bundle_submission_product
  WHERE halo_is_current = true
    AND coalesce(halo_is_deleted, false) = false
    AND submission_sk IN (SELECT submission_sk FROM ${edw}.dim_submission WHERE STN_KEY(stn) = :stn)
),
rd AS (
  SELECT """ + ", ".join(f"ANY_KEY({f}) AS ref_{i}" for i, f in enumerate(PMS_PRODUCT_REFS, 1)) + r"""
  FROM ${edw}.dim_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND ((halo_is_current = true AND NUM_KEY(id) IN (SELECT product_id FROM rf))
         OR product_sk IN (SELECT product_sk FROM rf))
),
ru AS (
  SELECT DISTINCT match_key
  FROM (
    """ + "\n    UNION ALL\n    ".join(f"SELECT ref_{i} AS match_key FROM rd" for i in _REF_NUMS) + r"""
  ) u
  WHERE match_key IS NOT NULL
)"""


def _linked_in_rhapsody(refs: list[str]) -> tuple[str, str]:
    """(joins, expr): LEFT JOINs of rk (the STN's Rhapsody product IDs) and ph (placeholder IDs) on each
    normalized PMS reference in `refs`, and the 'Linked in Rhapsody' value: 'Placeholder ID' when any reference
    is a placeholder (never paired with a product), else Yes when any is one of the STN's Rhapsody product
    links, else No. rk and ph are one row per value, so the joins cannot fan out."""
    joins = "".join(f"\nLEFT JOIN rk k{i} ON k{i}.match_key = {r}\nLEFT JOIN ph x{i} ON x{i}.match_key = {r}"
                    for i, r in enumerate(refs))
    placeholder = " OR ".join(f"x{i}.match_key IS NOT NULL" for i in range(len(refs)))
    hit = " OR ".join(f"k{i}.match_key IS NOT NULL" for i in range(len(refs)))
    return joins, f"CASE WHEN {placeholder} THEN 'Placeholder ID' WHEN {hit} THEN 'Yes' ELSE 'No' END"


_PMS_LINK_JOINS, _PMS_LINKED = _linked_in_rhapsody([f"ANY_KEY(pr.{f})" for f in PMS_PRODUCT_REFS])
_PMS_REF_KEYS = " UNION ALL ".join(f"SELECT ANY_KEY({f}) FROM pr" for f in PMS_PRODUCT_REFS)
_EDW_LINK_JOINS, _EDW_LINKED = _linked_in_rhapsody([f"coalesce(pc.ref_{i}, ps.ref_{i})" for i in _REF_NUMS])
_EDW_REF_KEYS = " UNION ALL ".join(f"SELECT ref_{i} FROM dp" for i in _REF_NUMS)
_EDW_PRODUCT_REFS = "".join(f", ANY_KEY({f}) AS ref_{i}" for i, f in enumerate(PMS_PRODUCT_REFS, 1))
_EDW_PRODUCT_COLS = ("product_sk, product_id, product_name, pd_number, category, subcategory, tp_number, "
                     "is_bulk_product" + "".join(f", ref_{i}" for i in range(1, len(PMS_PRODUCT_REFS) + 1)))
_PMS_PRODUCT_COLS = ", ".join(sorted({"id", "unique_product_id", "static_id", *PMS_PRODUCT_REFS}))


TRACE_QUERIES: list[TraceQuery] = [
    # ======================================================================= Rhapsody
    TraceQuery(
        id="rhapsody_submission",
        system="rhapsody",
        title="Submission",
        description="The submission as Rhapsody records it. The product and bundle counts include every "
                    "Rhapsody product list.",
        layout="record",
        compare=_SUBMISSION_COMPARE,
        # rhapsody_submissions (halo_is_deleted dropped; the table has no source deleted flag). Counts from the
        # three product lists, aggregated per STN before the join (one row), 0 when nothing is linked. A placeholder
        # product ID counts once (as in ESUB_RHAP_PRODUCT_COUNT_DIFFERS). Status '1' / '0' shown as Open / Closed
        # (P2.14).
        sql=r"""
WITH""" + _RHAP_LINKS + r""",
counts AS (
  SELECT stn_n,
         count(DISTINCT pid)                                         AS product_count,
         count(DISTINCT CASE WHEN src = 'Regular list' THEN pid END) AS n_regular,
         count(DISTINCT CASE WHEN src = 'Bulk list' THEN pid END)    AS n_bulk,
         count(DISTINCT CASE WHEN src = 'Review list' THEN pid END)  AS n_review,
         count(DISTINCT bid)                                         AS bundle_count
  FROM links
  GROUP BY stn_n
)
SELECT
  BLANK(s.stn)                                        AS `STN`,
  BLANK(s.stdz_submission_type)                       AS `Submission type`,
  BLANK(s.submission_type)                            AS `Submission type as entered`,
  """ + _status("s.submission_status") + r"""         AS `Status`,
  CAST(s.date_received AS DATE)                       AS `Received date`,
  CAST(s.date_submitted AS DATE)                      AS `Submitted date`,
  CAST(s.date_created AS DATE)                        AS `Created in Rhapsody`,
  """ + _company("s") + r"""                          AS `Company`,
  BLANK(s.abpc_company_name)                          AS `Registered company`,
  BLANK(s.itrac_company)                              AS `iTRAC company`,
  BLANK(s.organization)                               AS `Organization`,
  """ + _person("s.rhpm") + r"""                      AS `Project manager`,
  coalesce(c.product_count, 0)                        AS `Product count`,
  nullif(concat_ws(', ',
    CASE WHEN c.n_regular > 0 THEN concat(CAST(c.n_regular AS STRING), ' on the regular list') END,
    CASE WHEN c.n_bulk > 0 THEN concat(CAST(c.n_bulk AS STRING), ' on the bulk list') END,
    CASE WHEN c.n_review > 0 THEN concat(CAST(c.n_review AS STRING), ' on the review list') END), '')
                                                      AS `Products by list`,
  coalesce(c.bundle_count, 0)                         AS `Bundle count`,
  """ + _yn("s.no_products") + r"""                   AS `Marked no products`,
  BLANK(s.version_type)                               AS `Version type`,
  """ + _unless("s.submission_rejected_desc", _NOT_REJECTED) + r""" AS `Rejection reason`,
  NUM_KEY(s.reglist_key)                              AS `eSUB log-in ID`
FROM ${core}.rhapsody_submissions s
LEFT JOIN counts c ON c.stn_n = STN_KEY(s.stn)
WHERE coalesce(s.halo_is_deleted, false) = false
  AND STN_KEY(s.stn) = :stn
ORDER BY s.halo_update_ts DESC NULLS LAST, s.date_created DESC NULLS LAST, s.reglist_key DESC NULLS LAST
""",
    ),
    TraceQuery(
        id="rhapsody_products",
        system="rhapsody",
        title="Linked products",
        description="Every product Rhapsody links to this STN. Each row shows the product list and review "
                    "bundle. 'In PMS' and 'In Reporting' show whether those systems also have it under this STN. A "
                    "product ID that many products share is marked 'Placeholder ID', because it cannot point to "
                    "one product. Shows up to 1,000 products. 'Product count' gives the full total.",
        layout="table",
        # One row per ANY_KEY(product ID) across the three product lists. Placeholder IDs (ph) come first, are
        # shown once, and get no details, PMS match or Reporting match (mk = the other IDs). Details:
        # rhapsody_products (latest row per product ID), else the bulk list's own copy. Bundle names from live
        # rhapsody_bundle rows (deleted / missing bundles labelled). In PMS: the product ID equals a
        # PMS_PRODUCT_REFS value (uuid, K24) of a live pms_product row (one per value). In Reporting: the product
        # ID equals the dim_product uuid of one of the STN's Reporting products (ru, K64). Every lookup is one
        # row per value, so the joins cannot fan out.
        sql=r"""
WITH""" + _RHAP_LINKS + "," + _bund("SELECT bid FROM links") + r""",
pp AS (
  SELECT
    l.pid,
    min(l.product_id)                                                   AS product_id,
    array_join(array_sort(collect_set(l.src)), ', ')                    AS lists,
    nullif(array_join(array_sort(collect_set(
      CASE WHEN l.bid IS NULL THEN CAST(NULL AS STRING)
           WHEN b.is_live = 1 THEN coalesce(b.bundle_name, l.bundle_name, concat('Bundle ID ', l.bid))
           WHEN b.bid IS NOT NULL
             THEN concat(coalesce(b.bundle_name, l.bundle_name, concat('Bundle ID ', l.bid)), ' (deleted in Rhapsody)')
           ELSE concat(coalesce(l.bundle_name, concat('Bundle ID ', l.bid)), ' (not found in Rhapsody)')
      END)), '; '), '')                                                 AS bundles
  FROM links l
  LEFT JOIN bund b ON b.bid = l.bid
  WHERE l.pid IS NOT NULL
  GROUP BY l.pid
),""" + _placeholders("SELECT pid FROM pp") + r""",
mk AS (
  SELECT pp.pid AS match_key
  FROM pp
  LEFT JOIN ph ON ph.match_key = pp.pid
  WHERE ph.match_key IS NULL
),
p AS (
  SELECT ANY_KEY(product_id) AS pid, product_name, product_category, product_subcategory,
         characterizing_flavor, tp_number, static_product_id, active
  FROM ${core}.rhapsody_products
  WHERE coalesce(halo_is_deleted, false) = false
    AND ANY_KEY(product_id) IN (SELECT match_key FROM mk)
  QUALIFY row_number() OVER (PARTITION BY ANY_KEY(product_id)
                             ORDER BY pms_updated_date_time DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),
bp AS (
  SELECT ANY_KEY(product_id) AS pid, product_name, product_category, product_subcategory,
         characterizing_flavor, tp_number, static_product_id, active
  FROM ${core}.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) = :stn
    AND ANY_KEY(product_id) IN (SELECT match_key FROM mk)
  QUALIFY row_number() OVER (PARTITION BY ANY_KEY(product_id)
                             ORDER BY pms_updated_date_time DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),""" + _pms_match("SELECT match_key FROM mk") + "," + _RPT_REFS + r"""
SELECT
  pp.product_id                                                          AS `Product ID`,
  CASE WHEN x.match_key IS NOT NULL
         THEN concat('(placeholder ID shared by ', CAST(x.n_rows AS STRING), ' products)')
       ELSE coalesce(BLANK(p.product_name), BLANK(bp.product_name),
                     CASE WHEN p.pid IS NULL AND bp.pid IS NULL THEN '(no product details in Rhapsody)' END)
  END                                                                    AS `Product name`,
  coalesce(BLANK(p.product_category), BLANK(bp.product_category))        AS `Category`,
  coalesce(BLANK(p.product_subcategory), BLANK(bp.product_subcategory))  AS `Subcategory`,
  coalesce(BLANK(p.characterizing_flavor), BLANK(bp.characterizing_flavor)) AS `Flavor`,
  coalesce(BLANK(p.tp_number), BLANK(bp.tp_number))                      AS `TP number`,
  coalesce(BLANK(p.static_product_id), BLANK(bp.static_product_id))      AS `Static product ID`,
  CASE WHEN p.pid IS NULL AND bp.pid IS NULL THEN CAST(NULL AS STRING)
       ELSE """ + _yn("coalesce(p.active, bp.active)", "CAST(NULL AS STRING)") + r""" END AS `Active`,
  pp.lists                                                               AS `Product list`,
  pp.bundles                                                             AS `Bundle`,
  CASE WHEN x.match_key IS NOT NULL THEN 'Placeholder ID'
       WHEN pm.match_key IS NULL THEN 'No'
       WHEN pm.pms_stn_n = :stn THEN 'Yes'
       WHEN pm.pms_stn_n IS NULL THEN 'Yes, but with no STN in PMS'
       ELSE 'Yes, under a different STN' END                             AS `In PMS`,
  pm.pms_stn                                                             AS `STN in PMS`,
  CASE WHEN x.match_key IS NOT NULL THEN 'Placeholder ID'
       WHEN ru.match_key IS NOT NULL THEN 'Yes'
       ELSE 'No' END                                                     AS `In Reporting`,
  count(*) OVER ()                                                       AS `Product count`
FROM pp
LEFT JOIN ph x ON x.match_key = pp.pid
LEFT JOIN p    ON p.pid = pp.pid
LEFT JOIN bp   ON bp.pid = pp.pid
LEFT JOIN pm   ON pm.match_key = pp.pid
LEFT JOIN ru   ON ru.match_key = pp.pid
ORDER BY CASE WHEN x.match_key IS NOT NULL THEN 0 ELSE 1 END,
         coalesce(BLANK(p.product_name), BLANK(bp.product_name)) NULLS LAST, pp.product_id
LIMIT 1000
""",
    ),
    TraceQuery(
        id="rhapsody_bundles",
        system="rhapsody",
        title="Review bundles",
        description="Review bundles that hold this submission's products. Shows up to 500 bundles. Products "
                    "from this submission still point to any bundle marked deleted or not found.",
        layout="table",
        # Bundle IDs from the three product lists (one row per bundle ID), left-joined to rhapsody_bundle
        # (one row per id: halo_is_deleted and temporary copies dropped, a live row wins over a deleted one).
        sql=r"""
WITH""" + _RHAP_LINKS + r""",
sb AS (
  SELECT bid, max(bundle_name) AS link_name, count(DISTINCT pid) AS products_from_stn,
         array_join(array_sort(collect_set(src)), ', ') AS lists
  FROM links
  WHERE bid IS NOT NULL
  GROUP BY bid
),""" + _bund("SELECT bid FROM sb", r""",
         BLANK(bundlestatus)                  AS bundle_status,
         BLANK(stdz_submission_type)          AS stdz_type,
         BLANK(organization)                  AS organization,
         """ + _person("rhpm") + r"""         AS pm,
         CAST(datereceivedstart AS DATE)      AS received,
         CAST(goaldate AS DATE)               AS goal,
         totalproductsinbundle                AS total_products,
         CAST(s_createddate AS DATE)          AS created""") + r"""
SELECT
  coalesce(b.bundle_name, sb.link_name, concat('Bundle ID ', sb.bid)) AS `Bundle name`,
  sb.bid                                                 AS `Bundle ID`,
  CASE WHEN b.bid IS NULL THEN 'Not found in Rhapsody'
       WHEN b.is_live = 0 THEN 'Deleted in Rhapsody'
       ELSE b.bundle_status END                          AS `Bundle status`,
  b.stdz_type                                            AS `Submission type`,
  b.organization                                         AS `Organization`,
  b.pm                                                   AS `Project manager`,
  b.received                                             AS `Received date`,
  b.goal                                                 AS `Goal date`,
  sb.products_from_stn                                   AS `Products from this submission`,
  b.total_products                                       AS `Products in bundle`,
  sb.lists                                               AS `Product list`,
  b.created                                              AS `Created in Rhapsody`
FROM sb
LEFT JOIN bund b ON b.bid = sb.bid
ORDER BY b.received DESC NULLS LAST, b.created DESC NULLS LAST, sb.bid
LIMIT 500
""",
    ),
    TraceQuery(
        id="rhapsody_letters",
        system="rhapsody",
        title="Letters and communications",
        description="Letters and other communications filed on this submission's review bundles, newest "
                    "first. Shows up to 200 items. 'Letter count' gives the full total.",
        layout="table",
        # Live rhapsody_communications (halo_is_deleted, deleted, s_is_temporary_copy dropped; latest row per
        # id) linked to one of the STN's bundle IDs through EITHER NUM_KEY(uploadcommunicationstobundle_id) OR
        # ANY_KEY(bundleid). Confirmed on real data (Oct 2026): the first reaches 97.4% of bundles (K21), the
        # second 75% (K19, K20), so neither alone finds every letter. bn is one row per bundle ID (every ID
        # present, name when Rhapsody has one), so the two lookups cannot fan out and the bundle shown is never
        # blank; when the two IDs name different bundles, a live one is shown first.
        sql=r"""
WITH""" + _RHAP_LINKS + r""",
bids AS (
  SELECT DISTINCT bid FROM links WHERE bid IS NOT NULL
),""" + _bund("SELECT bid FROM bids") + r""",
bn AS (
  SELECT k.bid,
         coalesce(b.is_live, 0) AS is_live,
         CASE WHEN b.bid IS NULL THEN concat('Bundle ID ', k.bid, ' (not found in Rhapsody)')
              WHEN b.is_live = 0 THEN concat(coalesce(b.bundle_name, concat('Bundle ID ', k.bid)), ' (deleted in Rhapsody)')
              ELSE coalesce(b.bundle_name, concat('Bundle ID ', k.bid)) END AS bundle_label
  FROM bids k
  LEFT JOIN bund b ON b.bid = k.bid
),
c AS (
  SELECT id,
         NUM_KEY(uploadcommunicationstobundle_id) AS bid_link,
         ANY_KEY(bundleid)                        AS bid_text,
         stdz_communicationstypelocal, communicationstypelocal, communicationtype, issuedate, reviewstage,
         decisionrecommendation, industryduedate, author, finalfilename, doc_filename, s_filename,
         uploadeddate, stdz_failuremsg, failuremsg, esuberror
  FROM ${core}.rhapsody_communications
  WHERE coalesce(halo_is_deleted, false) = false
    AND coalesce(deleted, false) = false
    AND coalesce(s_is_temporary_copy, false) = false
    AND (NUM_KEY(uploadcommunicationstobundle_id) IN (SELECT bid FROM bids)
         OR ANY_KEY(bundleid) IN (SELECT bid FROM bids))
  QUALIFY row_number() OVER (PARTITION BY id
                             ORDER BY halo_update_ts DESC NULLS LAST, s_lastmodified DESC NULLS LAST) = 1
)
SELECT
  coalesce(BLANK(c.stdz_communicationstypelocal), BLANK(c.communicationstypelocal),
           BLANK(c.communicationtype))                                    AS `Letter or communication`,
  CAST(c.issuedate AS DATE)                                               AS `Issued`,
  CASE WHEN b1.is_live = 1 OR b2.bid IS NULL THEN b1.bundle_label
       WHEN b2.is_live = 1 OR b1.bid IS NULL THEN b2.bundle_label
       ELSE b1.bundle_label END                                           AS `Bundle`,
  BLANK(c.reviewstage)                                                    AS `Review stage`,
  BLANK(c.decisionrecommendation)                                         AS `Decision recommendation`,
  CAST(c.industryduedate AS DATE)                                         AS `Company response due`,
  """ + _person("c.author") + r"""                                        AS `Author`,
  coalesce(BLANK(c.finalfilename), BLANK(c.doc_filename), BLANK(c.s_filename)) AS `File name`,
  CAST(c.uploadeddate AS DATE)                                            AS `Uploaded`,
  coalesce(""" + _unless("c.stdz_failuremsg", _NO_ERROR) + ", " + _unless("c.failuremsg", _NO_ERROR) + r""",
           """ + _unless("c.esuberror", _NO_ERROR) + r""")               AS `Problem reported`,
  count(*) OVER ()                                                        AS `Letter count`
FROM c
LEFT JOIN bn b1 ON b1.bid = c.bid_link
LEFT JOIN bn b2 ON b2.bid = c.bid_text
ORDER BY c.issuedate DESC NULLS LAST, c.uploadeddate DESC NULLS LAST, c.id DESC
LIMIT 200
""",
    ),
    # ======================================================================= eSUB
    TraceQuery(
        id="esub_login",
        system="esub",
        title="Log-in record",
        description="The record eSUB made when the submission arrived. The Inactive field shows whether eSUB "
                    "inactivated it. When eSUB has several, the card shows the one the checks use: an active "
                    "record first, then the one Rhapsody names. Ties go to the most recent update.",
        layout="record",
        compare=(
            ("Received date", "Received date"),
            ("Submitted date", "Submitted date"),
            ("Company", "Company"),
            ("Product count", "Product count"),
            ("eSUB log-in ID", "eSUB log-in ID"),
        ),
        # esub_itrac_reglist (halo_is_deleted dropped; no DELETED_IND on this table; INACTIVE_IND kept and
        # shown). Document type from esub_itrac_doc_types (one row per NUM_KEY(DOC_TYPES_KEY), latest update).
        # rk = the reglist_key of the latest Rhapsody copy of the STN (one row), used for ordering only.
        # Product count 0 / blank = not recorded (as in ESUB_RHAP_PRODUCT_COUNT_DIFFERS), shown blank so the
        # grid does not report a difference for it.
        sql=r"""
WITH dt AS (
  SELECT
    NUM_KEY(DOC_TYPES_KEY)                                   AS doc_type_id,
    BLANK(DOC_TYPE)                                          AS doc_type,
    coalesce(BLANK(DISPLAY_CODE), BLANK(CODE))               AS doc_code
  FROM ${core}.esub_itrac_doc_types
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(DOC_TYPES_KEY) IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(DOC_TYPES_KEY)
                             ORDER BY UPDATE_DATE DESC NULLS LAST, CREATE_DATE DESC NULLS LAST,
                                      halo_update_ts DESC NULLS LAST) = 1
),
rk AS (
  SELECT NUM_KEY(reglist_key) AS login_id
  FROM ${core}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) = :stn
  QUALIFY row_number() OVER (PARTITION BY STN_KEY(stn)
                             ORDER BY halo_update_ts DESC NULLS LAST, date_created DESC NULLS LAST,
                                      reglist_key DESC NULLS LAST) = 1
),
r AS (
  SELECT REGLIST_KEY, STN, TRACKING_NUMBER, NAME, SUBMITTER_NAME, DATE_RECEIVED, DATE_SUBMITTED, DATE_ENTERED,
         CREATE_DATE, UPDATE_DATE, halo_update_ts, PRODUCT_COUNT, DOC_TYPES_KEY, COMMENTS_INACTIVE,
         FLAG_ON(INACTIVE_IND) AS is_inactive
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(STN) = :stn
)
SELECT
  BLANK(r.STN)                                              AS `STN`,
  BLANK(r.TRACKING_NUMBER)                                  AS `Tracking number`,
  BLANK(r.NAME)                                             AS `Company`,
  BLANK(r.SUBMITTER_NAME)                                   AS `Submitter`,
  CAST(r.DATE_RECEIVED AS DATE)                             AS `Received date`,
  CAST(r.DATE_SUBMITTED AS DATE)                            AS `Submitted date`,
  CAST(coalesce(r.DATE_ENTERED, r.CREATE_DATE) AS DATE)     AS `Log-in date`,
  CASE WHEN try_cast(r.PRODUCT_COUNT AS BIGINT) > 0
       THEN try_cast(r.PRODUCT_COUNT AS BIGINT) END         AS `Product count`,
  dt.doc_type                                               AS `Document type`,
  dt.doc_code                                               AS `Document type code`,
  CASE WHEN r.is_inactive THEN 'Yes' ELSE 'No' END          AS `Inactive`,
  CASE WHEN r.is_inactive THEN BLANK(r.COMMENTS_INACTIVE) END AS `Reason made inactive`,
  NUM_KEY(r.REGLIST_KEY)                                    AS `eSUB log-in ID`,
  CASE WHEN rk.login_id IS NOT NULL THEN 'Yes' ELSE 'No' END AS `Used by Rhapsody`
FROM r
LEFT JOIN dt ON dt.doc_type_id = NUM_KEY(r.DOC_TYPES_KEY)
LEFT JOIN rk ON rk.login_id = NUM_KEY(r.REGLIST_KEY)
ORDER BY CASE WHEN r.is_inactive THEN 1 ELSE 0 END,
         CASE WHEN rk.login_id IS NOT NULL THEN 0 ELSE 1 END,
         coalesce(r.UPDATE_DATE, r.halo_update_ts, r.CREATE_DATE) DESC NULLS LAST,
         r.REGLIST_KEY DESC NULLS LAST
""",
    ),
    TraceQuery(
        id="esub_electronic_submission",
        system="esub",
        title="Electronic submission form",
        description="Electronic submission records eSUB holds for this STN. Shows up to 100 records. The "
                    "Inactive field flags inactive ones, and the list skips deleted ones.",
        layout="table",
        # esub_tob_submission (halo_is_deleted and FLAG_ON(DELETED_IND) dropped; INACTIVE_IND kept and shown).
        # STN_BEING_AMENDED is not shown: confirmed on real data (Oct 2026), it never yields an STN (K40).
        sql=r"""
SELECT
  BLANK(SUBMISSION_TRACKING_NUMBER)      AS `STN`,
  BLANK(SUBMISSION_TYPE_CODE)            AS `Submission type code`,
  CAST(CREATE_DATE AS DATE)              AS `Created in eSUB`,
  CAST(UPDATE_DATE AS DATE)              AS `Last updated`,
  BLANK(TMASTER_ORIGINAL_STN)            AS `Original master file STN`,
  BLANK(PURPOSE)                         AS `Purpose`,
  """ + _yn("HOLD_IND") + r"""           AS `On hold`,
  """ + _yn("DEFICIENT_IND") + r"""      AS `Marked deficient`,
  CASE WHEN FLAG_ON(INACTIVE_IND) THEN 'Yes' ELSE 'No' END AS `Inactive`
FROM ${core}.esub_tob_submission
WHERE coalesce(halo_is_deleted, false) = false
  AND NOT FLAG_ON(DELETED_IND)
  AND STN_KEY(SUBMISSION_TRACKING_NUMBER) = :stn
ORDER BY CASE WHEN FLAG_ON(INACTIVE_IND) THEN 1 ELSE 0 END, CREATE_DATE DESC NULLS LAST,
         SUBMISSION_KEY DESC NULLS LAST
LIMIT 100
""",
    ),
    TraceQuery(
        id="esub_documents",
        system="esub",
        title="Documents received",
        description="Files eSUB received with this submission, newest first. Shows up to 200 files. "
                    "'Document count' gives the full total. 'How it is linked' flags files that only share the "
                    "delivery package or have missing file details.",
        layout="table",
        # Files = esub_submission_document rows for the STN's esub_tob_submission SUBMISSION_KEYs, plus
        # esub_document rows sharing a PACKAGE_KEY of the electronic submission or the log-in record. One
        # row per NUM_KEY(DOCUMENT_KEY) (link rows aggregated, esub_document latest row per key); document
        # types from esub_submission_doc_type -> esub_submission_doc_type_ref aggregated per document;
        # category from esub_document_category_ref. halo_is_deleted dropped everywhere, DELETED_IND on
        # esub_tob_submission. count(*) OVER () is evaluated before LIMIT, so it is the full total.
        sql=r"""
WITH sub AS (
  SELECT NUM_KEY(SUBMISSION_KEY) AS sub_key, NUM_KEY(PACKAGE_KEY) AS pkg
  FROM ${core}.esub_tob_submission
  WHERE coalesce(halo_is_deleted, false) = false
    AND NOT FLAG_ON(DELETED_IND)
    AND STN_KEY(SUBMISSION_TRACKING_NUMBER) = :stn
),
pkg AS (
  SELECT pkg FROM sub WHERE pkg IS NOT NULL
  UNION
  SELECT NUM_KEY(PACKAGE_KEY)
  FROM ${core}.esub_itrac_reglist
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(STN) = :stn
    AND NUM_KEY(PACKAGE_KEY) IS NOT NULL
),
link AS (
  SELECT NUM_KEY(DOCUMENT_KEY) AS doc_key, NUM_KEY(SUBMISSION_DOCUMENT_KEY) AS sd_key,
         SUBMISSION_PATH, IMAGE_HIDE_IND, CREATE_DATE
  FROM ${core}.esub_submission_document
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(SUBMISSION_KEY) IN (SELECT sub_key FROM sub)
),
lk AS (
  SELECT doc_key,
         max(BLANK(SUBMISSION_PATH))                                AS folder,
         max(CASE WHEN FLAG_ON(IMAGE_HIDE_IND) THEN 1 ELSE 0 END)   AS hidden,
         min(CREATE_DATE)                                           AS linked_at
  FROM link
  WHERE doc_key IS NOT NULL
  GROUP BY doc_key
),
doc AS (
  SELECT NUM_KEY(DOCUMENT_KEY) AS doc_key, FILE_NAME, FILE_TITLE, FILE_SIZE, FILE_CATEGORY,
         DOCUMENT_CATEGORY_KEY, CREATE_DATE
  FROM ${core}.esub_document
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(DOCUMENT_KEY) IS NOT NULL
    AND (NUM_KEY(DOCUMENT_KEY) IN (SELECT doc_key FROM lk)
         OR NUM_KEY(PACKAGE_KEY) IN (SELECT pkg FROM pkg))
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(DOCUMENT_KEY)
                             ORDER BY UPDATE_DATE DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),
ref AS (
  SELECT NUM_KEY(SUBMISSION_DOC_TYPE_REF_KEY) AS ref_key, BLANK(SUBMISSION_DOC_TYPE) AS ref_text
  FROM ${core}.esub_submission_doc_type_ref
  WHERE coalesce(halo_is_deleted, false) = false
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(SUBMISSION_DOC_TYPE_REF_KEY)
                             ORDER BY UPDATE_DATE DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),
dtype AS (
  SELECT l.doc_key,
         nullif(array_join(array_sort(collect_set(
           coalesce(ref.ref_text, BLANK(t.SUBMISSION_DOC_TYPE_OTHER)))), '; '), '') AS doc_types
  FROM ${core}.esub_submission_doc_type t
  JOIN link l ON l.sd_key = NUM_KEY(t.SUBMISSION_DOCUMENT_KEY)
  LEFT JOIN ref ON ref.ref_key = NUM_KEY(t.SUBMISSION_DOC_TYPE_REF_KEY)
  WHERE coalesce(t.halo_is_deleted, false) = false
    AND l.doc_key IS NOT NULL
  GROUP BY l.doc_key
),
cat AS (
  SELECT NUM_KEY(DOCUMENT_CATEGORY_KEY) AS cat_key, BLANK(DOCUMENT_CATEGORY_TEXT) AS cat_text
  FROM ${core}.esub_document_category_ref
  WHERE coalesce(halo_is_deleted, false) = false
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(DOCUMENT_CATEGORY_KEY)
                             ORDER BY UPDATE_DATE DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),
alld AS (
  SELECT doc_key FROM lk
  UNION
  SELECT doc_key FROM doc
)
SELECT
  coalesce(BLANK(d.FILE_TITLE), BLANK(d.FILE_NAME))            AS `Document title`,
  BLANK(d.FILE_NAME)                                           AS `File name`,
  dt.doc_types                                                 AS `Document type`,
  coalesce(cat.cat_text, BLANK(d.FILE_CATEGORY))               AS `Category`,
  CAST(coalesce(d.CREATE_DATE, lk.linked_at) AS DATE)          AS `Date added`,
  round(CAST(d.FILE_SIZE AS DOUBLE) / 1048576, 2)              AS `Size (MB)`,
  CASE WHEN lk.doc_key IS NOT NULL AND d.doc_key IS NULL THEN 'Attached, but the file details are missing'
       WHEN lk.doc_key IS NOT NULL THEN 'Attached to the electronic submission'
       ELSE 'Same delivery package only' END                   AS `How it is linked`,
  CASE WHEN lk.hidden = 1 THEN 'Yes' WHEN lk.doc_key IS NOT NULL THEN 'No' END AS `Hidden`,
  lk.folder                                                    AS `Folder`,
  count(*) OVER ()                                             AS `Document count`
FROM alld a
LEFT JOIN lk         ON lk.doc_key = a.doc_key
LEFT JOIN doc d      ON d.doc_key = a.doc_key
LEFT JOIN cat        ON cat.cat_key = NUM_KEY(d.DOCUMENT_CATEGORY_KEY)
LEFT JOIN dtype dt   ON dt.doc_key = a.doc_key
ORDER BY coalesce(d.CREATE_DATE, lk.linked_at) DESC NULLS LAST, a.doc_key DESC
LIMIT 200
""",
    ),
    TraceQuery(
        id="esub_links",
        system="esub",
        title="Linked submissions",
        description="Other STNs that eSUB links with this one. They include amendments and parent or child "
                    "submissions. Shows one row per linked STN, up to 500 rows.",
        layout="table",
        # Sources: esub_itrac_sub_links (both directions) and esub_submission_amendment_relations lists (no
        # halo_is_deleted column there). Lists are split on , ; | and any whitespace; every STN goes through
        # STN_KEY. Grouped to one row per linked STN; this STN itself and blanks are left out. Not used, confirmed
        # on real data (Oct 2026): esub_itrac_reglist.STN_LINK is not an STN (0 of 120,700 values match any STN,
        # K39) and esub_tob_submission.STN_BEING_AMENDED never yields an STN when split (K40).
        sql=r"""
WITH sl AS (
  SELECT
    CASE WHEN STN_KEY(STN) = :stn THEN STN_KEY(CHILD_STN) ELSE STN_KEY(STN) END        AS linked_stn,
    CASE WHEN STN_KEY(STN) = :stn THEN 'Child of this STN' ELSE 'Parent of this STN' END AS how,
    BLANK(DESCRIPTOR_CODE)                                                             AS link_code,
    BLANK(COMMENTS)                                                                    AS note,
    CREATE_DATE                                                                        AS linked_on
  FROM ${core}.esub_itrac_sub_links
  WHERE coalesce(halo_is_deleted, false) = false
    AND (STN_KEY(STN) = :stn OR STN_KEY(CHILD_STN) = :stn)
),
ar AS (
  SELECT AMENDED_SUBMISSIONS, CHILD_AMENDMENTS, RELATED_SUBMISSIONS
  FROM ${core}.esub_submission_amendment_relations
  WHERE STN_KEY(STN) = :stn
),
lists AS (
  SELECT 'Amended by this STN' AS how, AMENDED_SUBMISSIONS AS stn_list, CAST(NULL AS TIMESTAMP) AS linked_on
  FROM ar
  UNION ALL
  SELECT 'Amendment to this STN', CHILD_AMENDMENTS, CAST(NULL AS TIMESTAMP) FROM ar
  UNION ALL
  SELECT 'Related submission', RELATED_SUBMISSIONS, CAST(NULL AS TIMESTAMP) FROM ar
),
lx AS (
  SELECT STN_KEY(x) AS linked_stn, how, CAST(NULL AS STRING) AS link_code, CAST(NULL AS STRING) AS note,
         linked_on
  FROM lists
  LATERAL VIEW explode(split(coalesce(stn_list, ''), '[,;|\\s\\p{Z}]+')) e AS x
),
alll AS (
  SELECT linked_stn, how, link_code, note, linked_on FROM sl
  UNION ALL
  SELECT linked_stn, how, link_code, note, linked_on FROM lx
)
SELECT
  linked_stn                                                       AS `Linked STN`,
  array_join(array_sort(collect_set(how)), '; ')                   AS `How it is linked`,
  nullif(array_join(array_sort(collect_set(link_code)), ', '), '') AS `Link code`,
  CAST(min(linked_on) AS DATE)                                     AS `First linked`,
  nullif(array_join(array_sort(collect_set(note)), ' | '), '')     AS `Notes`
FROM alll
WHERE linked_stn IS NOT NULL
  AND linked_stn <> :stn
GROUP BY linked_stn
ORDER BY linked_stn
LIMIT 500
""",
    ),
    # ======================================================================= PMS
    TraceQuery(
        id="pms_products",
        system="pms",
        title="Products with this STN",
        description="PMS products that carry this STN, and whether Rhapsody links each one to this "
                    "submission. A product whose ID many products share is marked 'Placeholder ID', because "
                    "that ID cannot show which product Rhapsody means. Shows up to 1,000 products. 'Product "
                    "count' gives the full total.",
        layout="table",
        compare=(
            ("Company", "Company"),
            ("Product count", "Product count"),
        ),
        # pms_product by STN_KEY(stdz_stn) (halo_is_deleted dropped, latest row per id); pms_unique_product
        # (one row per id) for company and category codes; category/subcategory names matched on ANY_KEY(code)
        # only (confirmed K52-K59, P2.25: never by record number), the live entry first (inactive_date not
        # passed, then active, then latest update; U12: each code has an active and an inactive entry). Linked
        # in Rhapsody: 'Placeholder ID' when a PMS_PRODUCT_REFS value (uuid) of the product is a placeholder
        # (ph), else Yes when it is one of the STN's Rhapsody product IDs (rk). rk and ph are one row per value.
        sql=r"""
WITH""" + _RHAP_LINKS + "," + _RK + r""",
pr AS (
  SELECT """ + _PMS_PRODUCT_COLS + r""", stdz_pdnumber, product_name, brand, manufacturer, tp_number,
         create_date, update_date
  FROM ${core}.pms_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stdz_stn) = :stn
  QUALIFY row_number() OVER (PARTITION BY id ORDER BY update_date DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),""" + _placeholders(_PMS_REF_KEYS) + r""",
up AS (
  SELECT NUM_KEY(id) AS up_id, org_name, ctgry_code, subctgry_code
  FROM ${core}.pms_unique_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND NUM_KEY(id) IN (SELECT NUM_KEY(unique_product_id) FROM pr)
  QUALIFY row_number() OVER (PARTITION BY id ORDER BY halo_update_ts DESC NULLS LAST, halo_insert_ts DESC NULLS LAST) = 1
),
cat AS (
  SELECT ANY_KEY(code) AS match_value, BLANK(text) AS label
  FROM ${core}.pms_category
  WHERE coalesce(halo_is_deleted, false) = false
    AND ANY_KEY(code) IS NOT NULL
    AND BLANK(text) IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY ANY_KEY(code)
                             ORDER BY CASE WHEN coalesce(inactive_date <= current_timestamp(), false) THEN 1 ELSE 0 END,
                                      coalesce(active, true) DESC, update_date DESC NULLS LAST, id) = 1
),
subcat AS (
  SELECT ANY_KEY(code) AS match_value, BLANK(text) AS label
  FROM ${core}.pms_subcategory
  WHERE coalesce(halo_is_deleted, false) = false
    AND ANY_KEY(code) IS NOT NULL
    AND BLANK(text) IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY ANY_KEY(code)
                             ORDER BY CASE WHEN coalesce(inactive_date <= current_timestamp(), false) THEN 1 ELSE 0 END,
                                      coalesce(active, true) DESC, update_date DESC NULLS LAST, id) = 1
)
SELECT
  BLANK(pr.stdz_pdnumber)                           AS `PD number`,
  BLANK(pr.product_name)                            AS `Product name`,
  BLANK(pr.brand)                                   AS `Brand`,
  coalesce(cat.label, BLANK(up.ctgry_code))         AS `Category`,
  coalesce(sc.label, BLANK(up.subctgry_code))       AS `Subcategory`,
  BLANK(up.org_name)                                AS `Company`,
  BLANK(pr.manufacturer)                            AS `Manufacturer`,
  BLANK(pr.tp_number)                               AS `TP number`,
  BLANK(pr.static_id)                               AS `Static product ID`,
  NUM_KEY(pr.id)                                    AS `PMS product ID`,
  """ + _PMS_LINKED + r"""                          AS `Linked in Rhapsody`,
  CAST(pr.create_date AS DATE)                      AS `Created in PMS`,
  CAST(pr.update_date AS DATE)                      AS `Last updated`,
  count(*) OVER ()                                  AS `Product count`
FROM pr
LEFT JOIN up         ON up.up_id = NUM_KEY(pr.unique_product_id)
LEFT JOIN cat        ON cat.match_value = ANY_KEY(up.ctgry_code)
LEFT JOIN subcat sc  ON sc.match_value = ANY_KEY(up.subctgry_code)""" + _PMS_LINK_JOINS + r"""
ORDER BY BLANK(pr.stdz_pdnumber) NULLS LAST, BLANK(pr.product_name) NULLS LAST, pr.id
LIMIT 1000
""",
    ),
    # ======================================================================= Reporting
    TraceQuery(
        id="edw_submission",
        system="edw",
        title="Submission",
        description="The submission as Reporting records it. Dashboards and official counts use this copy, "
                    "so it should match Rhapsody exactly.",
        layout="record",
        compare=_SUBMISSION_COMPARE,
        # dim_submission current rows (halo_is_current, not halo_is_deleted). Counts exactly as
        # RPT_RHAP_SUBMISSION_PRODUCT_COUNTS_DIFFER: current, non-deleted fact_bundle_submission_product rows
        # whose submission_sk is ANY dim_submission version of this STN; products = distinct NUM_KEY(product_id)
        # (else '#' || product_sk); bundles = distinct NUM_KEY(dim_bundle.bundle_id) via bundle_sk (else
        # '#' || bundle_sk). Distinct counts, so the dim_bundle lookup cannot inflate them; aggregated per STN
        # before the join (one row), 0 when none. Status gets the Rhapsody display (P2.14), on the assumption
        # that Reporting keeps Rhapsody's codes (its submissions all come from Rhapsody, P2.29).
        sql=r"""
WITH sk AS (
  SELECT DISTINCT submission_sk, STN_KEY(stn) AS stn_n
  FROM ${edw}.dim_submission
  WHERE STN_KEY(stn) = :stn
    AND submission_sk IS NOT NULL
),
counts AS (
  SELECT sk.stn_n,
         count(DISTINCT coalesce(NUM_KEY(f.product_id), concat('#', CAST(f.product_sk AS STRING)))) AS product_count,
         count(DISTINCT coalesce(NUM_KEY(b.bundle_id), concat('#', CAST(f.bundle_sk AS STRING))))  AS bundle_count
  FROM ${edw}.fact_bundle_submission_product f
  JOIN sk ON sk.submission_sk = f.submission_sk
  LEFT JOIN ${edw}.dim_bundle b ON b.bundle_sk = f.bundle_sk
  WHERE f.halo_is_current = true
    AND coalesce(f.halo_is_deleted, false) = false
  GROUP BY sk.stn_n
)
SELECT
  BLANK(s.stn)                                        AS `STN`,
  BLANK(s.stdz_submission_type)                       AS `Submission type`,
  BLANK(s.submission_type)                            AS `Submission type as entered`,
  """ + _status("s.submission_status") + r"""         AS `Status`,
  CAST(s.date_received AS DATE)                       AS `Received date`,
  CAST(s.date_submitted AS DATE)                      AS `Submitted date`,
  CAST(s.date_created AS DATE)                        AS `Created in Rhapsody`,
  """ + _company("s") + r"""                          AS `Company`,
  BLANK(s.abpc_company_name)                          AS `Registered company`,
  BLANK(s.itrac_company)                              AS `iTRAC company`,
  BLANK(s.organization)                               AS `Organization`,
  """ + _person("s.rhpm") + r"""                      AS `Project manager`,
  coalesce(c.product_count, 0)                        AS `Product count`,
  coalesce(c.bundle_count, 0)                         AS `Bundle count`,
  """ + _yn("s.is_bulk_submission", "CAST(NULL AS STRING)") + r""" AS `Bulk submission`,
  """ + _yn("s.is_original_submission", "CAST(NULL AS STRING)") + r""" AS `Original submission`,
  BLANK(s.version_type)                               AS `Version type`,
  NUM_KEY(s.reglist_key)                              AS `eSUB log-in ID`
FROM ${edw}.dim_submission s
LEFT JOIN counts c ON c.stn_n = STN_KEY(s.stn)
WHERE s.halo_is_current = true
  AND coalesce(s.halo_is_deleted, false) = false
  AND STN_KEY(s.stn) = :stn
ORDER BY s.halo_effective_ts DESC NULLS LAST, s.halo_modified_ts DESC NULLS LAST
""",
    ),
    TraceQuery(
        id="edw_products",
        system="edw",
        title="Linked products",
        description="Every product Reporting links to this STN, with its review bundle. 'Linked in Rhapsody' "
                    "shows whether Rhapsody also links it to this submission, or 'Placeholder ID' when many "
                    "products share its ID. Shows up to 1,000 products. 'Product count' gives the full total and "
                    "matches the submission card.",
        layout="table",
        # Products = current, non-deleted fact_bundle_submission_product rows on any dim_submission version of
        # the STN, one row per NUM_KEY(product_id) (else '#' || product_sk) -- the same set the submission
        # card counts. Details: the current, non-deleted dim_product row with that id, else the (non-deleted)
        # version the link points at (product_sk). Bundles via dim_bundle by bundle_sk (one row per sk, current
        # first). Linked in Rhapsody: 'Placeholder ID' when a PMS_PRODUCT_REFS value of the product (its
        # dim_product.uuid) is a placeholder (ph), else Yes when it is one of the STN's Rhapsody product IDs (rk).
        # Confirmed on real data (Oct 2026): Reporting product IDs are PMS ids (K34) and never equal a Rhapsody
        # product ID (K64), so the product ID itself is not compared. rk and ph are one row per value.
        sql=r"""
WITH""" + _RHAP_LINKS + "," + _RK + r""",
sk AS (
  SELECT DISTINCT submission_sk
  FROM ${edw}.dim_submission
  WHERE STN_KEY(stn) = :stn
    AND submission_sk IS NOT NULL
),
f AS (
  SELECT coalesce(NUM_KEY(product_id), concat('#', CAST(product_sk AS STRING))) AS pkey,
         NUM_KEY(product_id) AS product_id, product_sk, bundle_sk
  FROM ${edw}.fact_bundle_submission_product
  WHERE halo_is_current = true
    AND coalesce(halo_is_deleted, false) = false
    AND submission_sk IN (SELECT submission_sk FROM sk)
),
bn AS (
  SELECT bundle_sk, BLANK(bundle_name) AS bundle_name, NUM_KEY(bundle_id) AS bundle_id,
         coalesce(deleted, false) OR coalesce(halo_is_deleted, false) AS is_deleted
  FROM ${edw}.dim_bundle
  WHERE bundle_sk IN (SELECT bundle_sk FROM f)
  QUALIFY row_number() OVER (PARTITION BY bundle_sk
                             ORDER BY CASE WHEN halo_is_current = true THEN 0 ELSE 1 END,
                                      halo_effective_ts DESC NULLS LAST) = 1
),
fp AS (
  SELECT
    f.pkey,
    max(f.product_id)                                                   AS product_id,
    max(f.product_sk)                                                   AS product_sk,
    nullif(array_join(array_sort(collect_set(
      CASE WHEN f.bundle_sk IS NULL THEN CAST(NULL AS STRING)
           WHEN bn.bundle_sk IS NULL THEN '(bundle details missing in Reporting)'
           ELSE concat(coalesce(bn.bundle_name, concat('Bundle ID ', bn.bundle_id), '(unnamed bundle)'),
                       CASE WHEN bn.is_deleted THEN ' (deleted)' ELSE '' END)
      END)), '; '), '')                                                 AS bundles
  FROM f
  LEFT JOIN bn ON bn.bundle_sk = f.bundle_sk
  WHERE f.pkey IS NOT NULL
  GROUP BY f.pkey
),
dp AS (
  SELECT product_sk, NUM_KEY(id) AS product_id, halo_is_current, halo_effective_ts,
         BLANK(product_name) AS product_name, BLANK(stdz_pdnumber) AS pd_number,
         coalesce(BLANK(ctgry_text), BLANK(ctgry_code)) AS category,
         coalesce(BLANK(subctgry_text), BLANK(subctgry_code)) AS subcategory,
         BLANK(tp_number) AS tp_number, is_bulk_product""" + _EDW_PRODUCT_REFS + r"""
  FROM ${edw}.dim_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND (product_sk IN (SELECT product_sk FROM fp) OR NUM_KEY(id) IN (SELECT product_id FROM fp))
),
pc AS (
  SELECT """ + _EDW_PRODUCT_COLS + r"""
  FROM dp
  WHERE halo_is_current = true AND product_id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY product_id
                             ORDER BY halo_effective_ts DESC NULLS LAST, product_sk DESC) = 1
),
ps AS (
  SELECT """ + _EDW_PRODUCT_COLS + r"""
  FROM dp
  WHERE product_sk IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY product_sk ORDER BY halo_effective_ts DESC NULLS LAST) = 1
),""" + _placeholders(_EDW_REF_KEYS) + r"""
SELECT
  coalesce(fp.product_id, ps.product_id)                                 AS `Product ID`,
  coalesce(pc.product_name, ps.product_name,
           CASE WHEN pc.product_sk IS NULL AND ps.product_sk IS NULL
                THEN '(no product details in Reporting)' END)            AS `Product name`,
  coalesce(pc.pd_number, ps.pd_number)                                   AS `PD number`,
  coalesce(pc.category, ps.category)                                     AS `Category`,
  coalesce(pc.subcategory, ps.subcategory)                               AS `Subcategory`,
  coalesce(pc.tp_number, ps.tp_number)                                   AS `TP number`,
  CASE WHEN pc.product_sk IS NULL AND ps.product_sk IS NULL THEN CAST(NULL AS STRING)
       ELSE """ + _yn("coalesce(pc.is_bulk_product, ps.is_bulk_product)", "CAST(NULL AS STRING)") + r""" END
                                                                         AS `Bulk product`,
  fp.bundles                                                             AS `Bundle`,
  """ + _EDW_LINKED + r"""                                               AS `Linked in Rhapsody`,
  count(*) OVER ()                                                       AS `Product count`
FROM fp
LEFT JOIN pc ON pc.product_id = fp.product_id
LEFT JOIN ps ON ps.product_sk = fp.product_sk""" + _EDW_LINK_JOINS + r"""
ORDER BY coalesce(pc.product_name, ps.product_name) NULLS LAST, coalesce(fp.product_id, ps.product_id), fp.pkey
LIMIT 1000
""",
    ),
    TraceQuery(
        id="edw_relationships",
        system="edw",
        title="Linked submissions",
        description="Amendments and other submissions that Reporting links with this STN. Each row names the "
                    "other STN. Shows up to 500 links.",
        layout="table",
        # dim_submission_relationships current, non-deleted rows where this STN is original_stn or related_stn
        # (STN_KEY on both); rows linking the STN to itself are left out, as on the eSUB side.
        sql=r"""
WITH r AS (
  SELECT
    coalesce(STN_KEY(original_stn) = :stn, false) AS is_original,
    original_stn, related_stn, relationship_type, relationship_path, through_stn,
    original_doc_type_name, related_doc_type_name, original_inactive_ind, related_inactive_ind
  FROM ${edw}.dim_submission_relationships
  WHERE halo_is_current = true
    AND coalesce(halo_is_deleted, false) = false
    AND (STN_KEY(original_stn) = :stn OR STN_KEY(related_stn) = :stn)
),
v AS (
  SELECT
    CASE WHEN is_original THEN related_stn ELSE original_stn END                     AS linked_stn,
    is_original, relationship_type, relationship_path, through_stn,
    CASE WHEN is_original THEN related_doc_type_name ELSE original_doc_type_name END AS linked_doc_type,
    CASE WHEN is_original THEN related_inactive_ind ELSE original_inactive_ind END   AS linked_inactive
  FROM r
)
SELECT
  BLANK(linked_stn)                                                                   AS `Linked STN`,
  BLANK(relationship_type)                                                            AS `Relationship`,
  CASE WHEN is_original THEN 'The original submission' ELSE 'The related submission' END AS `This STN is`,
  BLANK(linked_doc_type)                                                              AS `Linked STN document type`,
  BLANK(through_stn)                                                                  AS `Linked through STN`,
  BLANK(relationship_path)                                                            AS `Link path`,
  """ + _yn("linked_inactive") + r"""                                                 AS `Linked STN inactive`
FROM v
WHERE NOT coalesce(STN_KEY(linked_stn) = :stn, false)
ORDER BY BLANK(relationship_type) NULLS LAST, STN_KEY(linked_stn)
LIMIT 500
""",
    ),
]


def normalize_stn(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).upper()


def run_trace(stn: str, user_token: str | None = None) -> dict[str, pd.DataFrame | Exception]:
    from concurrent.futures import ThreadPoolExecutor

    from dq import db

    stn = normalize_stn(stn)
    if SETTINGS.demo_mode:
        return {q.id: _demo(q, stn) for q in TRACE_QUERIES}

    def one(q: TraceQuery):
        try:
            return db.query(q.render_sql(), {"stn": stn}, user_token)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=max(1, SETTINGS.max_parallel_queries)) as pool:
        results = dict(zip([q.id for q in TRACE_QUERIES], pool.map(one, TRACE_QUERIES)))
    return results


def _demo(q: TraceQuery, stn: str) -> pd.DataFrame:
    import hashlib
    import random
    from datetime import date, timedelta

    import sqlglot

    r = random.Random(int(hashlib.sha256(f"{q.id}{stn}".encode()).hexdigest()[:10], 16))
    try:
        cols = list(sqlglot.parse_one(q.render_sql(), read="databricks").named_selects)
    except Exception:
        cols = ["Field", "Value"]
    received = date(2023, 1, 1) + timedelta(days=int(stn[-4:]) % 700 if stn[-4:].isdigit() else 100)
    n = 1 if q.layout == "record" else r.randint(1, 5)
    rows = []
    for i in range(n):
        row = {}
        for c in cols:
            lc = c.lower()
            if "stn" in lc:
                row[c] = stn
            elif "received" in lc:
                # Make eSUB and Rhapsody disagree sometimes so the comparison highlight is visible.
                row[c] = (received + timedelta(days=(2 if q.system == "rhapsody" and r.random() < 0.5 else 0))).isoformat()
            elif "date" in lc:
                row[c] = (received + timedelta(days=r.randint(0, 200))).isoformat()
            elif "company" in lc:
                row[c] = "Acme Tobacco Co." if r.random() < 0.7 else "ACME TOBACCO COMPANY"
            elif "product" in lc and "name" in lc:
                row[c] = r.choice(["Classic Tobacco Pod 5%", "Original Menthol 20ct", "Wintergreen Pouch 6mg"])
            elif "count" in lc or lc.startswith("#") or "number" in lc:
                row[c] = r.randint(1, 12)
            elif "status" in lc:
                row[c] = r.choice(["Received", "In Review", "Closed"])
            elif "type" in lc:
                row[c] = r.choice(["PMTA", "SE"])
            else:
                row[c] = f"Demo value {i + 1}"
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)
