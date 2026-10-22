"""Checks inside PMS (Product Management System) only.

Every check here evaluates one row per non-deleted PMS product, so stewards see
products the way they find them in PMS: by PD number and STN. All checks share
the `_BASE` product list below, which

  * drops soft-deleted rows (halo_is_deleted) from pms_product and pms_unique_product,
  * keeps one row per product id (latest update) and per unique-product id, so the
    left join from product to its details record can never multiply rows,
  * treats blank strings and common placeholders ('N/A', 'NONE', 'TBD', '-', ...)
    as blank (module-local `_filled`, built on the shared BLANK macro),
  * normalizes every ID with the shared macros in dq/sqlfrag.py (NUM_KEY for PMS
    record numbers, STN_KEY for STNs, ANY_KEY for codes), so products match the
    same way here as in every other module,
  * builds `record_key` from the PD number (falling back to TP number, static ID,
    then the PMS record number) and makes it unique by appending the PMS record
    number when two products share the same identifier,
  * sets `record_date` to the product's created date when it is plausible
    (2009-06-22 .. tomorrow), else the date the record arrived in the lakehouse, so a
    mistyped created date is never hidden by the date filter,
  * looks up the product's STN in Rhapsody (one row per STN) for the submission type
    and Organization (Rhapsody's organization field: the applicant company, not a CTP
    office), so the submission-type and organization filters work. Products whose STN
    is not in Rhapsody show those as "(not recorded)". The contract column keeps its
    internal name `office`.

pms_product.uuid is never used here. Confirmed on real data (Oct 2026): it is the ID
Rhapsody uses for a PMS product (K24, K50), and the Rhapsody <-> PMS checks in
dq/checks/rhapsody_pms.py match on it alone. One placeholder UUID is shared by 14,368
products (P4 U10), so any future match, grouping or de-duplication on uuid must first
drop UUIDs found on more than 100 products, as that module does (PLACEHOLDER_ID_ROWS).
The product keys used here, pms_product.id and pms_unique_product.id, are confirmed
unique (P4).

PMS code lists (category, subcategory, product attribute) are matched on their code
only. Confirmed on real data (Oct 2026): details records point to these lists by code,
never by the list's record number (K52-K59, P2.25), so the old record-number fallback
was dropped.
"""

from __future__ import annotations

from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check

CORE = "${core}"

_ALL_FILTERS = frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE})

# --------------------------------------------------------------------------- #
# Module-local helper (placeholders are not covered by the shared macros)
# --------------------------------------------------------------------------- #

# A value is a placeholder when, upper-cased with everything except letters and digits
# removed, it is one of these. '' catches '-', '--', '.', '?', '#', ...  '0' counts as a
# placeholder for identifiers and names, but not for measured values (0 mg nicotine is real).
_VAL_WORDS = (
    "('', 'NA', 'NONE', 'NULL', 'UNKNOWN', 'TBD', 'TBA', 'NOTPROVIDED', 'NOTAPPLICABLE', "
    "'NONEPROVIDED', 'NOTAVAILABLE')"
)
_ID_WORDS = _VAL_WORDS[:-1] + ", '0')"
_LETTERS_DIGITS_ONLY = r"'[^\\p{L}\\p{N}]'"


def _filled(col: str, words: str = _ID_WORDS) -> str:
    """BLANK(col), and also NULL when the value is only a placeholder such as N/A or TBD."""
    return (
        f"CASE WHEN regexp_replace(upper(BLANK({col})), {_LETTERS_DIGITS_ONLY}, '') IN {words} "
        f"THEN CAST(NULL AS STRING) ELSE BLANK({col}) END"
    )


def _value(col: str) -> str:
    """Like _filled, but '0' is a real value (for measurements and units)."""
    return _filled(col, _VAL_WORDS)


# "Other" choice on a PMS list: the code is OTHER/OTH, or the list text is "Other",
# "Other (specify)", "Other - ...", "Cigar - Other", "... (Other)". A real choice whose name
# merely starts with the word (e.g. "Other Tobacco Products") is not treated as "Other".
def _is_other(code: str, text: str) -> str:
    return (
        f"coalesce(ANY_KEY({code}) IN ('OTHER', 'OTH') "
        rf"OR TXT({text}) RLIKE '^OTHER *([(:/,-].*)?$' "
        rf"OR TXT({text}) RLIKE '[(:/,-] *OTHER *[)]?$', false)"
    )


# --------------------------------------------------------------------------- #
# Shared product list
# --------------------------------------------------------------------------- #

_BASE = f"""
WITH up AS (
  SELECT
    NUM_KEY(id)                                 AS up_id,
    {_filled('org_name')}                       AS org_name,
    BLANK(ctgry_code)                           AS ctgry_code,
    BLANK(subctgry_code)                        AS subctgry_code,
    BLANK(ctgry_other_text)                     AS up_ctgry_other,
    BLANK(subctgry_other_text)                  AS up_subctgry_other,
    BLANK(flavor_code)                          AS flavor_code,
    BLANK(flavor_other_text)                    AS up_flavor_other,
    {_value('nicotine_val')}                    AS nicotine_val,
    {_value('nicotine_unit_code')}              AS nicotine_unit,
    BLANK(nicotine_unit_other_text)             AS up_nicotine_unit_other,
    BLANK(eliquid_volume_unit_other_text)       AS up_eliquid_unit_other
  FROM {CORE}.pms_unique_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(id)
                             ORDER BY halo_update_ts DESC NULLS LAST, halo_insert_ts DESC NULLS LAST) = 1
),
pr AS (
  SELECT
    NUM_KEY(id)                                       AS product_id,
    NUM_KEY(unique_product_id)                        AS up_id,
    stdz_stn                                          AS stn_raw,
    {_filled('stdz_stn')}                             AS stn,
    {_filled('stdz_pdnumber')}                        AS pd_number,
    {_filled('tp_number')}                            AS tp_number,
    {_filled('static_id')}                            AS static_id,
    BLANK(product_source)                             AS product_source,
    product_name                                      AS product_name_raw,
    {_filled('product_name')}                         AS product_name,
    brand                                             AS brand_raw,
    {_filled('brand')}                                AS brand,
    sub_brand                                         AS sub_brand_raw,
    manufacturer                                      AS manufacturer_raw,
    {_filled('manufacturer')}                         AS manufacturer,
    coalesce(stdz_product_name_contains_error, false) AS name_flagged,
    BLANK(ctgry_other_text)                           AS ctgry_other,
    BLANK(subctgry_other_text)                        AS subctgry_other,
    coalesce(flavor_not_provided, false)              AS flavor_np,
    BLANK(flavor_other_text)                          AS flavor_other,
    coalesce(nicotine_not_provided, false)            AS nicotine_np,
    BLANK(nicotine_unit_other_text)                   AS nicotine_unit_other,
    coalesce(pak_mass_not_provided, false) OR coalesce(pak_not_provided, false) AS pak_mass_np,
    {_value('pak_mass_val')}                          AS pak_mass_val,
    {_value('pak_mass_unit_code')}                    AS pak_mass_unit,
    coalesce(portion_mass_not_provided, false)        AS portion_mass_np,
    {_value('portion_mass_val')}                      AS portion_mass_val,
    {_value('portion_mass_unit_code')}                AS portion_mass_unit,
    coalesce(eliquid_volume_not_provided, false)      AS eliquid_np,
    {_value('eliquid_volume_val')}                    AS eliquid_val,
    {_value('eliquid_volume_unit_code')}              AS eliquid_unit,
    BLANK(eliquid_volume_unit_other_text)             AS eliquid_unit_other,
    create_date,
    update_date,
    date_first_us_market,
    halo_insert_ts
  FROM {CORE}.pms_product
  WHERE coalesce(halo_is_deleted, false) = false
  QUALIFY row_number() OVER (PARTITION BY NUM_KEY(id)
                             ORDER BY update_date DESC NULLS LAST, halo_update_ts DESC NULLS LAST) = 1
),
rh_sub AS (
  SELECT
    STN_KEY(stn)                  AS stn_key,
    BLANK(stdz_submission_type)   AS submission_type,
    BLANK(organization)           AS office
  FROM {CORE}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY STN_KEY(stn)
                             ORDER BY halo_update_ts DESC NULLS LAST, halo_insert_ts DESC NULLS LAST) = 1
),
prod0 AS (
  SELECT
    pr.*,
    STN_KEY(pr.stn)               AS stn_key,
    up.up_id IS NOT NULL          AS has_details,
    up.org_name,
    up.ctgry_code,
    up.subctgry_code,
    up.up_ctgry_other,
    up.up_subctgry_other,
    up.flavor_code,
    up.up_flavor_other,
    up.nicotine_val,
    up.nicotine_unit,
    up.up_nicotine_unit_other,
    up.up_eliquid_unit_other,
    rs.submission_type,
    rs.office,
    coalesce(pr.pd_number, pr.tp_number, pr.static_id,
             concat('PMS record ', coalesce(pr.product_id, '(no number)'))) AS product_ref,
    CAST(coalesce(
      CASE WHEN CAST(pr.create_date AS DATE) BETWEEN DATE'2009-06-22' AND date_add(current_date(), 1)
           THEN pr.create_date END,
      pr.halo_insert_ts) AS DATE)                                         AS record_date
  FROM pr
  LEFT JOIN up      ON up.up_id = pr.up_id
  LEFT JOIN rh_sub rs ON rs.stn_key = STN_KEY(pr.stn)
),
prod AS (
  SELECT
    prod0.*,
    CASE WHEN count(*) OVER (PARTITION BY product_ref) > 1
         THEN concat(product_ref, ' (PMS record ', coalesce(product_id, '?'), ')')
         ELSE product_ref END AS record_key
  FROM prod0
)
"""

_BASE_NOTE = (
    "Population: every product in slvr_ctp_core.pms_product with halo_is_deleted not true, one row per "
    "NUM_KEY(id) (latest update_date). PMS has no other delete/inactive flag on products. Linked details come "
    "from slvr_ctp_core.pms_unique_product (halo_is_deleted not true, one row per NUM_KEY(id)) via "
    "NUM_KEY(unique_product_id) = NUM_KEY(id), so the left join never multiplies rows. Blank strings and "
    "placeholders count as blank. A placeholder is a value that, upper-cased with non-letters/digits removed, "
    "is one of '' / NA / NONE / NULL / UNKNOWN / TBD / TBA / NOTPROVIDED / NOTAPPLICABLE / NONEPROVIDED / "
    "NOTAVAILABLE ('0' also counts for IDs and names). record_key = PD number, else TP number, else static_id, "
    "else 'PMS record <id>'. ' (PMS record <id>)' is appended when two products share it. record_date = "
    "create_date when between 2009-06-22 and current_date()+1, else halo_insert_ts. Submission type and "
    "organization = stdz_submission_type and organization of slvr_ctp_core.rhapsody_submissions "
    "(halo_is_deleted not true; latest halo_update_ts per STN_KEY(stn)) matched on STN_KEY(pms stdz_stn); "
    "organization fills the office output column. Both are NULL when the STN is not in Rhapsody. Confirmed on "
    "real data (Oct 2026): rhapsody_submissions.organization is the applicant company (P2.17: 3,679 distinct "
    "values), not a CTP office; pms_product.id, pms_unique_product.id and rhapsody_submissions.stn are unique "
    "(P4), so the one-row-per-id steps change nothing today and no join here can multiply rows. Confirmed on real "
    "data (Oct 2026): only 59.8% of PMS STNs are in Rhapsody (K30), so many products (most GF and AS STNs, "
    "P2.32) show submission type and organization as not recorded. pms_product.uuid is not used (one "
    "placeholder UUID sits on 14,368 products, P4 U10)."
)

_ID_ASSUMPTION = (
    "Stewards find products in PMS by PD number. The ID shown is the PD number, else the TP number, else the "
    "static product ID, else the PMS record number."
)
_SUB_ASSUMPTION = (
    "A product takes its submission type and organization from the Rhapsody submission with the same STN. "
    "If Rhapsody has no such STN, both show as not recorded."
)


def _sql(body: str) -> str:
    return _BASE + body


# --------------------------------------------------------------------------- #
# Measurement fragments for PMS_PRODUCT_ATTRIBUTE_CONFLICT
# --------------------------------------------------------------------------- #

# alias, steward label, value, unit code, typed-in 'other' unit text (or None), not-provided flag
_MEASURES = (
    ("nic", "nicotine", "nicotine_val", "nicotine_unit",
     "coalesce(up_nicotine_unit_other, nicotine_unit_other)", "nicotine_np"),
    ("pak", "package mass", "pak_mass_val", "pak_mass_unit", None, "pak_mass_np"),
    ("eliq", "e-liquid volume", "eliquid_val", "eliquid_unit",
     "coalesce(eliquid_unit_other, up_eliquid_unit_other)", "eliquid_np"),
    ("por", "portion mass", "portion_mass_val", "portion_mass_unit", None, "portion_mass_np"),
)
_PLAIN_NUMBER = r"'^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)$'"


def _measure_cols(a: str, val: str, unit: str, other: str | None, np: str) -> str:
    cleaned = rf"regexp_replace({val}, '[,\\s]', '')"
    other_sql = other or "CAST(NULL AS STRING)"
    return f"""
    CASE WHEN {cleaned} RLIKE {_PLAIN_NUMBER} THEN try_cast({cleaned} AS DOUBLE) END AS {a}_num,
    coalesce(TXT({unit}) IN ('OTHER', 'OTH'), false)                               AS {a}_unit_is_other,
    CASE WHEN coalesce(TXT({unit}) IN ('OTHER', 'OTH'), false) THEN {other_sql}
         ELSE coalesce({unit}, {other_sql}) END                                      AS {a}_unit_eff"""


def _measure_problem(a: str, val: str, np: str) -> str:
    return f"""
    CASE
      WHEN {val} IS NULL THEN NULL
      WHEN {np} AND coalesce({a}_num, 1) <> 0 THEN 'np'
      WHEN {a}_num IS NULL THEN 'num'
      WHEN {a}_num < 0 THEN 'neg'
      WHEN {a}_unit_eff IS NULL AND {a}_num <> 0 THEN 'unit'
    END AS {a}_p"""


def _measure_reason(a: str, label: str, val: str) -> str:
    return f"""
        CASE {a}_p
          WHEN 'np'   THEN concat('{label} is marked not provided but shows "', {val}, '"')
          WHEN 'num'  THEN concat('{label} value "', {val}, '" is not a plain number')
          WHEN 'neg'  THEN concat('{label} value "', {val}, '" is negative')
          WHEN 'unit' THEN CASE WHEN {a}_unit_is_other
                                THEN concat('{label} value "', {val}, '" has unit "Other" but the unit text is blank')
                                ELSE concat('{label} value "', {val}, '" has no unit') END
        END"""


def _measure_display(a: str, val: str, unit: str, np: str, alias: str) -> str:
    return (
        f"  nullif(concat_ws(' ', {val}, coalesce({a}_unit_eff, {unit}),\n"
        f"                   CASE WHEN {np} THEN '[marked not provided]' END), '') AS `{alias}`"
    )


_ATTR_SQL = (
    "\n, v AS (\n  SELECT\n    prod.*,"
    + ",".join(_measure_cols(a, val, unit, other, np) for a, _, val, unit, other, np in _MEASURES)
    + "\n  FROM prod\n),\nf AS (\n  SELECT\n    v.*,"
    + ",".join(_measure_problem(a, val, np) for a, _, val, _, _, np in _MEASURES)
    + "\n  FROM v\n),\ng AS (\n  SELECT\n    f.*,\n"
    + "    coalesce(" + " OR ".join(f"{a}_p IS NOT NULL" for a, *_ in _MEASURES) + ", false) AS bad,\n"
    + "    coalesce(" + " OR ".join(f"{a}_p = 'np'" for a, *_ in _MEASURES) + ", false) AS any_np,\n"
    + "    coalesce(" + " OR ".join(f"{a}_p IN ('num', 'neg')" for a, *_ in _MEASURES) + ", false) AS any_num\n"
    + "  FROM f\n)\n"
    + r"""SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  bad                                                             AS is_issue,
  CASE
    WHEN NOT bad   THEN NULL
    WHEN any_np    THEN '"Not provided" conflict'
    WHEN any_num   THEN 'Unusable number'
    ELSE 'Blank unit'
  END                                                             AS issue_category,
  CASE WHEN bad THEN
    concat('Product ', record_key, ': ',
      concat_ws('; ',"""
    + ",".join(_measure_reason(a, label, val) for a, label, val, *_ in _MEASURES)
    + r"""),
      '.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  org_name                                                        AS `Company`,
"""
    + ",\n".join(
        _measure_display(a, val, unit, np, alias)
        for (a, _, val, unit, _, np), alias in zip(
            _MEASURES, ("Nicotine", "Package mass", "E-liquid volume", "Portion mass")
        )
    )
    + "\nFROM g\n"
)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

CHECKS: list[Check] = [
    # ----------------------------------------------------------------- Completeness
    Check(
        id="PMS_PRODUCT_NO_STN_OR_STATIC_ID",
        title="PMS products missing a valid STN or static ID",
        systems=("pms",),
        entity="Products",
        dimension="Completeness",
        severity="High",
        question="Does every PMS product have a valid STN and a static product ID?",
        why="The STN ties a product to its submission. The static product ID identifies the product across its "
            "versions in PMS. A missing value drops the product out of submission counts and makes it hard to "
            "find.",
        how_to_fix="Search PMS for the PD number or product name shown. Find the submission in eSUB or Rhapsody "
                   "and copy its STN exactly as written there. A valid STN is letters followed by digits, such "
                   "as PM0001234. If the static product ID is blank, ask the PMS product team to generate one. "
                   "Do not type it by hand.",
        record_label="products",
        # Submission type / organization come from the STN, so a product with a missing or wrong STN can never
        # carry them: those filters would hide exactly the problems this check looks for. Date filter only.
        filters=frozenset({FILTER_DATE}),
        date_label="Product created date",
        warn_below=0.99,
        fail_below=0.95,
        technical_note=_BASE_NOTE + " Fails when stdz_stn is blank or a placeholder, or when STN_KEY(stdz_stn) "
        "does not match ^[A-Z]{2,4}[0-9]{4,10}([.-][A-Z0-9]+)?$. Also fails when static_id is blank or a "
        "placeholder. Only the date filter is declared. Submission type and organization come from the STN "
        "lookup, so they are always blank when the STN is the problem. Confirmed on real data (Oct 2026): STNs "
        "carry no spaces, lower-case letters, dashes or dots in any system (P2.33), and 99.85% of PMS products "
        "sit on PM STNs (P2.32: 6,472 STNs carry 26.6M products), so STN_KEY changes nothing today. This check "
        "does not ask whether the STN is in Rhapsody (only 59.8% of PMS STNs are, K30).",
        assumptions=(
            _ID_ASSUMPTION,
            "Every PMS product needs an STN and a static product ID. That includes bulk and co-bundled "
            "products. The Product source field is shown so any group of products with a valid reason to "
            "have no STN is easy to spot.",
            "A valid STN is two to four letters followed by four to ten digits. A short suffix after a dot "
            "or dash is allowed. The rule is loose on purpose. It catches only clearly wrong values such as "
            "'N/A' or two STNs typed into one field.",
        ),
        sql=_sql(r"""
, f AS (
  SELECT
    prod.*,
    stn IS NULL                                                                     AS no_stn,
    coalesce(NOT (stn_key RLIKE '^[A-Z]{2,4}[0-9]{4,10}([.-][A-Z0-9]+)?$'), false) AS bad_stn,
    static_id IS NULL                                                               AS no_static
  FROM prod
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  coalesce(no_stn OR bad_stn OR no_static, false)                 AS is_issue,
  CASE
    WHEN no_stn AND no_static THEN 'Both blank'
    WHEN no_stn               THEN 'Blank STN'
    WHEN bad_stn              THEN 'Unusual STN'
    WHEN no_static            THEN 'Blank static ID'
  END                                                             AS issue_category,
  CASE WHEN no_stn OR bad_stn OR no_static THEN
    concat('Product ', record_key,
           CASE WHEN product_name IS NOT NULL THEN concat(' ("', product_name, '")') ELSE '' END,
           ' ',
           concat_ws(' and ',
             CASE WHEN no_stn AND BLANK(stn_raw) IS NOT NULL
                  THEN concat('has the placeholder "', BLANK(stn_raw), '" as its STN')
                  WHEN no_stn THEN 'has no STN' END,
             CASE WHEN bad_stn THEN concat('has the unusual STN "', stn,
                                           '" (expected letters then digits, such as PM0001234)') END,
             CASE WHEN no_static THEN 'has no static product ID' END),
           '.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  static_id                                                       AS `Static product ID`,
  pd_number                                                       AS `PD number`,
  tp_number                                                       AS `TP number`,
  product_name_raw                                                AS `Product name`,
  org_name                                                        AS `Company`,
  product_source                                                  AS `Product source`,
  product_id                                                      AS `PMS record number`
FROM f
"""),
    ),
    Check(
        id="PMS_PRODUCT_MISSING_NAME_BRAND_MAKER",
        title="PMS products with blank name or maker fields",
        systems=("pms",),
        entity="Products",
        dimension="Completeness",
        severity="Medium",
        question="Does every PMS product have a name and brand, plus a manufacturer?",
        why="Reviewers and public product lists find products by name and brand. A blank or placeholder value "
            "such as 'N/A' makes the product hard to find and leaves gaps in manufacturer reports.",
        how_to_fix="Open the product in PMS. Copy each missing value from the product list in the original "
                   "submission (eSUB or Rhapsody). Replace placeholders such as 'N/A' with the real value. If "
                   "the company did not supply a value, say so in the PMS comments. Then ask the review team "
                   "whether to request it.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        technical_note=_BASE_NOTE + " Fails when any of product_name / brand / manufacturer is blank or a "
        "placeholder. sub_brand is shown but not required.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "Every product should have all three fields. If PMS began collecting brand and manufacturer on a "
            "set date, start the date filter on that date.",
            "Values such as N/A, NONE, UNKNOWN, TBD, NOT PROVIDED, '-', '?' or '0' are placeholders, not real names.",
        ),
        sql=_sql(r"""
, f AS (
  SELECT
    prod.*,
    product_name IS NULL AS no_name,
    brand IS NULL        AS no_brand,
    manufacturer IS NULL AS no_maker,
    CAST(product_name IS NULL AS INT) + CAST(brand IS NULL AS INT) + CAST(manufacturer IS NULL AS INT) AS n_blank
  FROM prod
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  coalesce(n_blank > 0, false)                                    AS is_issue,
  CASE
    WHEN n_blank > 1 THEN 'Several fields blank'
    WHEN no_name     THEN 'Blank name'
    WHEN no_brand    THEN 'Blank brand'
    WHEN no_maker    THEN 'Blank manufacturer'
  END                                                             AS issue_category,
  CASE WHEN n_blank > 0 THEN
    concat('Product ', record_key,
           CASE WHEN stn IS NOT NULL THEN concat(' on STN ', stn) ELSE '' END,
           ' is missing: ',
           concat_ws(', ',
             CASE WHEN no_name THEN
               CASE WHEN BLANK(product_name_raw) IS NOT NULL
                    THEN concat('product name (shows "', BLANK(product_name_raw), '")')
                    ELSE 'product name' END END,
             CASE WHEN no_brand THEN
               CASE WHEN BLANK(brand_raw) IS NOT NULL
                    THEN concat('brand (shows "', BLANK(brand_raw), '")')
                    ELSE 'brand' END END,
             CASE WHEN no_maker THEN
               CASE WHEN BLANK(manufacturer_raw) IS NOT NULL
                    THEN concat('manufacturer (shows "', BLANK(manufacturer_raw), '")')
                    ELSE 'manufacturer' END END),
           '.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  brand_raw                                                       AS `Brand`,
  sub_brand_raw                                                   AS `Sub-brand`,
  manufacturer_raw                                                AS `Manufacturer`,
  org_name                                                        AS `Company`,
  CAST(create_date AS DATE)                                       AS `Created date`
FROM f
"""),
    ),
    Check(
        id="PMS_PRODUCT_DETAILS_RECORD_MISSING",
        title="PMS products missing their details record",
        systems=("pms",),
        entity="Products",
        dimension="Completeness",
        severity="High",
        question="Is every PMS product linked to a details record that still exists?",
        why="Each PMS product links to a details record that holds its category and flavor. Without that record "
            "the product has no category. It is then left out of official counts by category or counted in "
            "the wrong one.",
        how_to_fix="Open the product in PMS and look at its category and flavor. If they are empty or the page "
                   "shows an error, contact the PMS product team. Send them the PD number and the details "
                   "record number shown here. They can relink the product or restore the deleted record.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        warn_below=0.995,
        fail_below=0.98,
        technical_note=_BASE_NOTE + " Fails when pms_product.unique_product_id is NULL. Also fails when no "
        "pms_unique_product row has that id (NUM_KEY on both sides) or every such row is soft-deleted. The "
        "exists/deleted lookup is pre-aggregated to one row per id. Confirmed on real data (Oct 2026): "
        "pms_product.unique_product_id = pms_unique_product.id is the details link, and every filled value "
        "finds its record (K31: 20,443,326 of 20,443,326), so 'Details not found' should stay rare.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "Every product should have a details record.",
        ),
        sql=_sql(r"""
, up_all AS (
  SELECT
    NUM_KEY(id)                                                                 AS up_id,
    max(CASE WHEN coalesce(halo_is_deleted, false) = false THEN 1 ELSE 0 END)   AS is_live
  FROM ${core}.pms_unique_product
  WHERE id IS NOT NULL
  GROUP BY NUM_KEY(id)
),
f AS (
  SELECT
    prod.*,
    CASE
      WHEN prod.up_id IS NULL   THEN 'Not linked'
      WHEN ua.up_id IS NULL     THEN 'Does not exist'
      WHEN ua.is_live = 0       THEN 'Deleted'
    END AS problem
  FROM prod
  LEFT JOIN up_all ua ON ua.up_id = prod.up_id
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  coalesce(problem IS NOT NULL, false)                            AS is_issue,
  CASE problem
    WHEN 'Not linked'     THEN 'No details link'
    WHEN 'Does not exist' THEN 'Details not found'
    WHEN 'Deleted'        THEN 'Details deleted'
  END                                                             AS issue_category,
  CASE
    WHEN problem = 'Not linked' THEN
      concat('Product ', record_key,
             CASE WHEN product_name IS NOT NULL THEN concat(' ("', product_name, '")') ELSE '' END,
             ' has no linked details record, so its category is unknown.')
    WHEN problem IS NOT NULL THEN
      concat('Product ', record_key,
             CASE WHEN product_name IS NOT NULL THEN concat(' ("', product_name, '")') ELSE '' END,
             ' points to details record ', up_id,
             CASE WHEN problem = 'Deleted' THEN ', which was deleted in PMS.'
                  ELSE ', which does not exist in PMS.' END)
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  up_id                                                           AS `Details record number`,
  CAST(create_date AS DATE)                                       AS `Created date`,
  product_id                                                      AS `PMS record number`
FROM f
"""),
    ),
    # ----------------------------------------------------------------- Consistency
    Check(
        id="PMS_PRODUCT_CATEGORY_NOT_RECOGNIZED",
        title="PMS products with no valid category or subcategory",
        systems=("pms",),
        entity="Products",
        dimension="Consistency",
        severity="High",
        question="Does every PMS product have a category and subcategory from the approved PMS lists?",
        why="Official counts and dashboards by product type depend on the category. A blank or unknown category "
            "drops the product from those counts or puts it in the wrong group. An 'Other' category with no "
            "description cannot be classified at all.",
        how_to_fix="Open the product in PMS. Pick the right category and subcategory from the drop-down, based on "
                   "the product description in the submission. If the value is not in the drop-down, report it "
                   "to the PMS product team. Also report a retired value that still appears on new products. "
                   "When you choose 'Other', type a short description of the product type.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        technical_note=_BASE_NOTE + " Population: products whose details record exists (products with no "
        "details record are counted by PMS_PRODUCT_DETAILS_RECORD_MISSING instead). pms_unique_product."
        "ctgry_code / subctgry_code are matched with ANY_KEY to pms_category / pms_subcategory (halo_is_deleted "
        "not true) on ANY_KEY(code) only; the lookup is reduced to one row per code, the live entry first (not "
        "retired, then active = true, then latest update_date, then id). Confirmed on real data (Oct 2026): "
        "details records reference categories / subcategories by CODE, never by the list record number (K52: 9 of "
        "9 category codes, K54: 36 of 37 subcategory codes; K53, K55: 0% by record number; P2.25), so the former "
        "NUM_KEY(id) fallback was dropped: it could only add false matches. Confirmed on real data (Oct 2026): "
        "subcategory code BIDI is not on the list (P2.26), so its products show as 'Unknown subcategory'. "
        "Confirmed on real data (Oct 2026): the 9 pms_category codes sit on 17 rows, each code with an active and "
        "an inactive entry with different names (P4 U12); the active entry wins, so its name is used and the "
        "inactive twin never makes a product "
        "'Retired'. 'Retired' = active = false or inactive_date <= now, and is flagged only when the product was "
        "created after the retirement date (inactive_date, else the reference row's update_date); retired codes "
        "with no known date are not flagged. A blank subcategory is flagged only when the category is blank or "
        "at least one other product in the same category (ANY_KEY(ctgry_code)) has a subcategory, so categories "
        "that have no subcategories are not flagged. 'Other' = code OTHER/OTH, or list text 'Other', 'Other "
        "(...)', 'Other - ...', '... - Other', '... (Other)' (not 'Other <word>'). It is flagged only when "
        "*_other_text is blank on pms_product and on pms_unique_product.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "A product needs a subcategory whenever other products in its category have one. If no product in "
            "a category has a subcategory, we assume PMS has no subcategories for that category.",
            "A retired category is a problem only on products created after it was retired. Older products "
            "keep the category that was valid at the time.",
            "The 'Other' choice in the PMS lists has the code OTHER or the name 'Other'. The name may carry a "
            "note in parentheses or after a dash. A real choice whose name only starts with 'Other' is not "
            "treated as 'Other'.",
        ),
        sql=_sql(r"""
, cat_ref AS (
  -- Matched on the list code only: Confirmed on real data (Oct 2026), details records use the code, never the
  -- list record number (K52, K53, P2.25).
  SELECT match_value, ref_text, is_retired, retired_on
  FROM (
    SELECT ANY_KEY(code) AS match_value, BLANK(text) AS ref_text,
           (coalesce(active, true) = false OR inactive_date <= current_timestamp()) AS is_retired,
           coalesce(inactive_date, CASE WHEN coalesce(active, true) = false THEN update_date END) AS retired_on,
           active, update_date, id AS ref_id
    FROM ${core}.pms_category
    WHERE coalesce(halo_is_deleted, false) = false AND ANY_KEY(code) IS NOT NULL
  ) c
  -- the live (not retired) entry wins: Confirmed on real data (Oct 2026), every pms_category code has an
  -- active and an inactive entry with different names (P4 U12)
  QUALIFY row_number() OVER (PARTITION BY match_value
                             ORDER BY coalesce(is_retired, false), coalesce(active, true) DESC,
                                      update_date DESC NULLS LAST, ref_id) = 1
),
sub_ref AS (
  -- Matched on the list code only, as in cat_ref (K54, K55, P2.25).
  SELECT match_value, ref_text, is_retired, retired_on
  FROM (
    SELECT ANY_KEY(code) AS match_value, BLANK(text) AS ref_text,
           (coalesce(active, true) = false OR inactive_date <= current_timestamp()) AS is_retired,
           coalesce(inactive_date, CASE WHEN coalesce(active, true) = false THEN update_date END) AS retired_on,
           active, update_date, id AS ref_id
    FROM ${core}.pms_subcategory
    WHERE coalesce(halo_is_deleted, false) = false AND ANY_KEY(code) IS NOT NULL
  ) s
  -- the live (not retired) entry wins, as in cat_ref
  QUALIFY row_number() OVER (PARTITION BY match_value
                             ORDER BY coalesce(is_retired, false), coalesce(active, true) DESC,
                                      update_date DESC NULLS LAST, ref_id) = 1
),
f AS (
  SELECT
    p.*,
    cr.ref_text AS ctgry_text,
    sr.ref_text AS subctgry_text,
    p.ctgry_code IS NULL                                             AS cat_blank,
    p.ctgry_code IS NOT NULL AND cr.match_value IS NULL              AS cat_unknown,
    coalesce(cr.is_retired AND CAST(p.create_date AS DATE) > CAST(cr.retired_on AS DATE), false) AS cat_retired,
    CAST(cr.retired_on AS DATE)                                      AS cat_retired_on,
    max(CASE WHEN p.subctgry_code IS NOT NULL THEN 1 ELSE 0 END)
      OVER (PARTITION BY ANY_KEY(p.ctgry_code))                      AS cat_uses_sub,
    p.subctgry_code IS NOT NULL AND sr.match_value IS NULL           AS sub_unknown,
    coalesce(sr.is_retired AND CAST(p.create_date AS DATE) > CAST(sr.retired_on AS DATE), false) AS sub_retired,
    CAST(sr.retired_on AS DATE)                                      AS sub_retired_on,
    """ + _is_other("p.ctgry_code", "cr.ref_text") + r"""
      AND coalesce(p.ctgry_other, p.up_ctgry_other) IS NULL          AS cat_other_blank,
    """ + _is_other("p.subctgry_code", "sr.ref_text") + r"""
      AND coalesce(p.subctgry_other, p.up_subctgry_other) IS NULL    AS sub_other_blank
  FROM prod p
  LEFT JOIN cat_ref cr ON cr.match_value = ANY_KEY(p.ctgry_code)
  LEFT JOIN sub_ref sr ON sr.match_value = ANY_KEY(p.subctgry_code)
  WHERE p.has_details
),
g AS (
  SELECT
    f.*,
    coalesce(subctgry_code IS NULL AND (cat_blank OR cat_uses_sub = 1), false) AS sub_blank
  FROM f
),
h AS (
  SELECT
    g.*,
    coalesce(cat_blank OR cat_unknown OR cat_retired OR sub_blank OR sub_unknown OR sub_retired
             OR cat_other_blank OR sub_other_blank, false) AS bad
  FROM g
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  bad                                                             AS is_issue,
  CASE
    WHEN NOT bad                         THEN NULL
    WHEN cat_blank                       THEN 'Blank category'
    WHEN cat_unknown                     THEN 'Unknown category'
    WHEN cat_retired                     THEN 'Retired category'
    WHEN sub_blank                       THEN 'Blank subcategory'
    WHEN sub_unknown                     THEN 'Unknown subcategory'
    WHEN sub_retired                     THEN 'Retired subcategory'
    ELSE '"Other" not described'
  END                                                             AS issue_category,
  CASE WHEN bad THEN
    concat('Product ', record_key, ': ',
      concat_ws('; ',
        CASE WHEN cat_blank THEN 'category is blank' END,
        CASE WHEN cat_unknown THEN concat('category "', ctgry_code, '" is not on the PMS list') END,
        CASE WHEN cat_retired THEN concat('category "', coalesce(ctgry_text, ctgry_code), '" was retired on ',
                                          CAST(cat_retired_on AS STRING), ' but the product was created ',
                                          CAST(CAST(create_date AS DATE) AS STRING)) END,
        CASE WHEN sub_blank THEN 'subcategory is blank' END,
        CASE WHEN sub_unknown THEN concat('subcategory "', subctgry_code, '" is not on the PMS list') END,
        CASE WHEN sub_retired THEN concat('subcategory "', coalesce(subctgry_text, subctgry_code), '" was retired on ',
                                          CAST(sub_retired_on AS STRING), ' but the product was created ',
                                          CAST(CAST(create_date AS DATE) AS STRING)) END,
        CASE WHEN cat_other_blank THEN 'category is "Other" with no description' END,
        CASE WHEN sub_other_blank THEN 'subcategory is "Other" with no description' END),
      '.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  org_name                                                        AS `Company`,
  ctgry_code                                                      AS `Category code`,
  ctgry_text                                                      AS `Category name`,
  subctgry_code                                                   AS `Subcategory code`,
  subctgry_text                                                   AS `Subcategory name`,
  up_id                                                           AS `Details record number`
FROM h
"""),
    ),
    Check(
        id="PMS_PRODUCT_FLAVOR_NOT_RECOGNIZED",
        title="PMS products with no valid flavor",
        systems=("pms",),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does every PMS product have a flavor from the PMS list, with a description when 'Other' is "
                 "chosen?",
        why="Flavor is part of the product's regulatory record. Flavored-product reports and reviews rely on it. "
            "A blank or unknown flavor leaves the product out of those reports or puts it in the wrong group.",
        how_to_fix="Open the product in PMS and pick the flavor from the drop-down, based on the product "
                   "description in the submission. If the company did not state a flavor, check the 'not "
                   "provided' box instead of leaving it blank. If the flavor is not in the drop-down, report it "
                   "to the PMS product team. When you choose 'Other', type the flavor in the description box.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        technical_note=_BASE_NOTE + " Population: products whose details record exists. Flavor = "
        "pms_unique_product.flavor_code matched with ANY_KEY to slvr_ctp_core.pms_product_attribute "
        "(halo_is_deleted not true) on ANY_KEY(code) only, one row per code (active first, then latest "
        "update_date, then id). Confirmed on real data (Oct 2026): all 6 flavor codes on details records are "
        "attribute list codes (K56) and none is a list record number (K57; P2.25), so the former NUM_KEY(id) "
        "fallback was dropped. Description = pms_unique_product.flavor_other_text, else "
        "pms_product.flavor_other_text. Fails when: flavor code and description are blank and "
        "pms_product.flavor_not_provided is not true, and at least one other product in the same category "
        "(ANY_KEY(ctgry_code)) has a flavor; the code is not in the attribute list; or the flavor is 'Other' "
        "(code OTHER/OTH, or list text 'Other' / 'Other (...)' / '... - Other') with no description. Confirmed on "
        "real data (Oct 2026): pms_product_attribute codes are unique (P4 U14), so each code has one name. The "
        "Rhapsody flavor comparison uses the same list.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "A blank flavor is a problem only in categories where other products have a flavor. If no product "
            "in a category has one, we assume PMS does not collect flavor there.",
            "Checking the 'flavor not provided' box is a valid answer and is not flagged.",
        ),
        sql=_sql(r"""
, attr_ref AS (
  -- Matched on the list code only: Confirmed on real data (Oct 2026), flavor codes are list codes, never list
  -- record numbers (K56, K57, P2.25).
  SELECT match_value, ref_text
  FROM (
    SELECT ANY_KEY(code) AS match_value, BLANK(text) AS ref_text, active, update_date, id AS ref_id
    FROM ${core}.pms_product_attribute
    WHERE coalesce(halo_is_deleted, false) = false AND ANY_KEY(code) IS NOT NULL
  ) a
  QUALIFY row_number() OVER (PARTITION BY match_value
                             ORDER BY coalesce(active, true) DESC, update_date DESC NULLS LAST, ref_id) = 1
),
f AS (
  SELECT
    p.*,
    ar.ref_text                                                       AS flavor_text,
    ar.match_value IS NOT NULL                                        AS flavor_known,
    coalesce(p.up_flavor_other, p.flavor_other)                       AS flavor_desc,
    max(CASE WHEN p.flavor_code IS NOT NULL OR coalesce(p.up_flavor_other, p.flavor_other) IS NOT NULL
             THEN 1 ELSE 0 END) OVER (PARTITION BY ANY_KEY(p.ctgry_code)) AS cat_uses_flavor
  FROM prod p
  LEFT JOIN attr_ref ar ON ar.match_value = ANY_KEY(p.flavor_code)
  WHERE p.has_details
),
g AS (
  SELECT
    f.*,
    coalesce(flavor_code IS NULL AND flavor_desc IS NULL AND NOT flavor_np AND cat_uses_flavor = 1, false)
                                                                      AS flavor_blank,
    coalesce(flavor_code IS NOT NULL AND NOT flavor_known, false)     AS flavor_unknown,
    """ + _is_other("flavor_code", "flavor_text") + r""" AND flavor_desc IS NULL AS flavor_other_blank
  FROM f
),
h AS (
  SELECT g.*, coalesce(flavor_blank OR flavor_unknown OR flavor_other_blank, false) AS bad FROM g
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  bad                                                             AS is_issue,
  CASE
    WHEN NOT bad          THEN NULL
    WHEN flavor_blank     THEN 'Blank flavor'
    WHEN flavor_unknown   THEN 'Unknown flavor'
    ELSE '"Other" not described'
  END                                                             AS issue_category,
  CASE
    WHEN flavor_blank THEN
      concat('Product ', record_key,
             CASE WHEN product_name IS NOT NULL THEN concat(' ("', product_name, '")') ELSE '' END,
             ' has no flavor and is not marked "flavor not provided".')
    WHEN flavor_unknown THEN
      concat('Product ', record_key,
             CASE WHEN product_name IS NOT NULL THEN concat(' ("', product_name, '")') ELSE '' END,
             ' has flavor "', flavor_code, '", which is not on the PMS list.',
             CASE WHEN flavor_other_blank THEN ' It also has no flavor description.' ELSE '' END)
    WHEN flavor_other_blank THEN
      concat('Product ', record_key,
             CASE WHEN product_name IS NOT NULL THEN concat(' ("', product_name, '")') ELSE '' END,
             ' has flavor "', coalesce(flavor_text, flavor_code), '" but no flavor description.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  org_name                                                        AS `Company`,
  flavor_code                                                     AS `Flavor code`,
  flavor_text                                                     AS `Flavor name`,
  flavor_desc                                                     AS `Flavor description`,
  CASE WHEN flavor_np THEN 'Yes' ELSE 'No' END                    AS `Flavor not provided`,
  up_id                                                           AS `Details record number`
FROM h
"""),
    ),
    Check(
        id="PMS_PRODUCT_POSSIBLE_DUPLICATE",
        title="Duplicate PMS products on the same STN",
        systems=("pms",),
        entity="Products",
        dimension="Consistency",
        severity="High",
        question="Does each product appear only once on its STN in PMS?",
        why="A product entered twice on one submission counts twice in product totals. The two copies can also "
            "end up with different decisions on record.",
        how_to_fix="Open the STN in PMS and compare the PMS records shown here. If they are the same product, keep "
                   "the record linked in Rhapsody. Ask the PMS product team to retire the other one. If they "
                   "share a PD number but are different products, ask the PMS product team to give each its own "
                   "PD number.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        warn_below=0.995,
        fail_below=0.98,
        technical_note=_BASE_NOTE + " Population: products with an STN. Flags a product when another product "
        "on the same STN_KEY(stn) has (a) the same name key AND the same details record, or (b) the same "
        "ANY_KEY(PD number). Name key = TXT(name) keeping only letters, digits, '.' and '%'. Same details "
        "record = same NUM_KEY(unique_product_id), or both blank. Same name with different details is NOT "
        "flagged (usually a different strength or pack size). Groups are pre-aggregated to one row per group "
        "value and left-joined back, so each product appears once. Every member of a group is flagged. Products "
        "are told apart by pms_product.id only; uuid is not used (one placeholder UUID sits on 14,368 products, "
        "P4 U10). Confirmed on real data (Oct 2026): no STN + PD number pair repeats as stored (P4 U15), so "
        "'Duplicate PD number' "
        "can only fire on values that differ just in case, spacing or leading zeros (ANY_KEY), or on new entries.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "Two products on one STN are the same product entered twice when they share a details record and "
            "their names match. Names match when they have the same letters and digits in the same order. "
            "Products with the same name but different details, such as a different nicotine strength, count "
            "as different products.",
            "Each PMS record is a separate product listing, not an older version kept alongside the current one.",
        ),
        sql=_sql(r"""
, d AS (
  SELECT
    prod.*,
    nullif(regexp_replace(TXT(product_name), '[^\\p{L}\\p{N}.%]+', ''), '') AS name_key,
    coalesce(up_id, '(none)')                                               AS up_grp,
    ANY_KEY(pd_number)                                                      AS pd_key
  FROM prod
  WHERE stn_key IS NOT NULL
),
by_name AS (
  SELECT
    stn_key, name_key, up_grp,
    count(*)                                                            AS n_copies,
    array_join(slice(sort_array(collect_set(product_id)), 1, 10), ', ') AS ids
  FROM d
  WHERE name_key IS NOT NULL
  GROUP BY stn_key, name_key, up_grp
  HAVING count(*) > 1
),
by_pd AS (
  SELECT
    stn_key, pd_key,
    count(*)                                                            AS n_copies,
    array_join(slice(sort_array(collect_set(product_id)), 1, 10), ', ') AS ids
  FROM d
  WHERE pd_key IS NOT NULL
  GROUP BY stn_key, pd_key
  HAVING count(*) > 1
),
f AS (
  SELECT
    d.*,
    bn.stn_key IS NOT NULL AS name_dup,
    bp.stn_key IS NOT NULL AS pd_dup,
    bn.n_copies            AS name_copies,
    bn.ids                 AS name_ids,
    bp.n_copies            AS pd_copies,
    bp.ids                 AS pd_ids
  FROM d
  LEFT JOIN by_name bn ON bn.stn_key = d.stn_key AND bn.name_key = d.name_key AND bn.up_grp = d.up_grp
  LEFT JOIN by_pd   bp ON bp.stn_key = d.stn_key AND bp.pd_key = d.pd_key
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  coalesce(name_dup OR pd_dup, false)                             AS is_issue,
  CASE
    WHEN name_dup THEN 'Duplicate name'
    WHEN pd_dup   THEN 'Duplicate PD number'
  END                                                             AS issue_category,
  CASE WHEN name_dup OR pd_dup THEN
    concat_ws(' ',
      CASE WHEN name_dup THEN
        concat('Name "', product_name, '" appears on ', CAST(name_copies AS STRING),
               ' products for STN ', stn, ' (PMS records ', name_ids,
               CASE WHEN name_copies > 10 THEN ', ...' ELSE '' END, ')',
               CASE WHEN up_id IS NULL THEN ', none with a details record.'
                    ELSE concat(', all with details record ', up_id, '.') END) END,
      CASE WHEN pd_dup THEN
        concat('PD number ', pd_number, ' is used by ', CAST(pd_copies AS STRING),
               ' products on STN ', stn, ' (PMS records ', pd_ids,
               CASE WHEN pd_copies > 10 THEN ', ...' ELSE '' END, ').') END)
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  org_name                                                        AS `Company`,
  greatest(coalesce(name_copies, 1), coalesce(pd_copies, 1))      AS `Copies on STN`,
  coalesce(name_ids, pd_ids)                                      AS `Matching PMS records`,
  product_id                                                      AS `PMS record number`,
  up_id                                                           AS `Details record number`,
  CAST(create_date AS DATE)                                       AS `Created date`
FROM f
"""),
    ),
    Check(
        id="PMS_PRODUCT_NAME_LOOKS_WRONG",
        title="PMS product names that look wrong",
        systems=("pms",),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Is every PMS product name readable and accepted by the automatic name check?",
        why="Letters and public product lists show the product name. Hidden characters print badly on letters. "
            "A name that is only an ID will not match the same product in Rhapsody or eSUB.",
        how_to_fix="Open the product in PMS and compare its name with the product list in the submission. Retype "
                   "the name if it has odd symbols or line breaks. If the name is only a number or ID, replace "
                   "it with the real product name.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        technical_note=_BASE_NOTE + " Population: products with a non-blank, non-placeholder name (blank names "
        "are counted by PMS_PRODUCT_MISSING_NAME_BRAND_MAKER). Fails when stdz_product_name_contains_error is "
        "true; when the name contains control characters (\\x00-\\x1F, \\x7F, so tabs and line breaks too) or "
        "the Unicode replacement character U+FFFD; or when STN_KEY(name) (upper-cased, whitespace removed) "
        "is only digits/punctuation with at least 5 digits, or equals STN_KEY of the product's STN / PD number "
        "/ TP number.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "The 'product name contains error' flag comes from the automatic clean-up that runs when PMS data "
            "is copied for reporting. A flagged name needs a steward's review. The data engineering team owns "
            "the exact rule.",
            "A name made of five or more digits, such as '1234567', is an ID typed into the name box. Short "
            "number names such as the cigarette brand '1839' are not flagged.",
        ),
        sql=_sql(r"""
, f AS (
  SELECT
    prod.*,
    coalesce(product_name RLIKE '[\\x00-\\x1F\\x7F]|\\x{FFFD}', false)        AS bad_chars,
    coalesce((STN_KEY(product_name) RLIKE '^[0-9.,/#-]+$'
              AND length(regexp_replace(product_name, '[^0-9]', '')) >= 5)
             OR STN_KEY(product_name) = stn_key
             OR STN_KEY(product_name) = STN_KEY(pd_number)
             OR STN_KEY(product_name) = STN_KEY(tp_number), false)            AS id_only
  FROM prod
  WHERE product_name IS NOT NULL
),
g AS (
  SELECT f.*, coalesce(bad_chars OR id_only OR name_flagged, false) AS bad FROM f
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  bad                                                             AS is_issue,
  CASE
    WHEN bad_chars    THEN 'Unreadable characters'
    WHEN id_only      THEN 'Name is an ID'
    WHEN name_flagged THEN 'Failed name check'
  END                                                             AS issue_category,
  CASE WHEN bad THEN
    concat('Product ', record_key, ' is named "',
           regexp_replace(product_name, '[\\x00-\\x1F\\x7F]', ' '), '": ',
           concat_ws('; ',
             CASE WHEN bad_chars THEN 'it has hidden or unreadable characters' END,
             CASE WHEN id_only THEN 'it is only a number or ID, not a product name' END,
             CASE WHEN name_flagged THEN 'the automatic name check flagged it' END),
           '.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  tp_number                                                       AS `TP number`,
  product_name_raw                                                AS `Product name`,
  brand_raw                                                       AS `Brand`,
  org_name                                                        AS `Company`,
  CASE WHEN name_flagged THEN 'Yes' ELSE 'No' END                 AS `Failed name check`
FROM g
"""),
    ),
    Check(
        id="PMS_PRODUCT_ATTRIBUTE_CONFLICT",
        title="PMS measurements with a bad value or no unit",
        systems=("pms",),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="When a PMS product measurement has a value, is it a usable number with a unit?",
        why="Product comparisons and nicotine reports use these measurements. A value that is negative, has no "
            "unit, is not a number, or is marked 'not provided' skews those reports.",
        how_to_fix="Open the product in PMS. Compare the measurement named in the reason with the product details "
                   "in the submission. Clear the 'not provided' box or remove the value, whichever is wrong. "
                   "Choose the missing unit, or type it when the unit is 'Other'. Enter only the number in the "
                   "number box, for example 5 with the unit mg/mL.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        technical_note=_BASE_NOTE + " Attributes: nicotine (pms_product.nicotine_not_provided with "
        "pms_unique_product.nicotine_val / nicotine_unit_code, other-unit text from pms_unique_product else "
        "pms_product nicotine_unit_other_text), package mass (pms_product pak_mass_val / pak_mass_unit_code; "
        "not-provided = pak_mass_not_provided OR pak_not_provided), e-liquid volume (pms_product "
        "eliquid_volume_val / eliquid_volume_unit_code / eliquid_volume_unit_other_text, else the "
        "pms_unique_product one), portion mass (pms_product portion_mass_val / portion_mass_unit_code). Values "
        "and unit codes that are blank or placeholders (N/A, NONE, NOT PROVIDED, ...) count as blank. A value "
        "is a number when it matches ^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)$ once commas and whitespace are removed "
        "(then try_cast to DOUBLE; no NaN/Infinity/exponents). Effective unit = the 'other' text when the unit "
        "code is OTHER/OTH, else the unit code, else the 'other' text. Per attribute, first matching problem: "
        "not-provided flag set and a value that is not numeric zero; not a plain number; negative; non-zero "
        "with no effective unit. Zero values are never flagged. Nicotine is only evaluated when the details "
        "record exists.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "A value of 0 next to a 'not provided' mark is a filler, not a contradiction.",
            "Measurement values should be plain numbers, with the unit chosen separately. A value such as '5%' "
            "or 'about 2 g' counts as not a number. Commas are ignored, so '1,000' is 1000.",
            "When the unit is 'Other', the typed unit description is the unit.",
        ),
        sql=_sql(_ATTR_SQL),
    ),
    # ----------------------------------------------------------------- Timeliness
    Check(
        id="PMS_PRODUCT_DATES_IMPOSSIBLE",
        title="PMS products with a missing or impossible date",
        systems=("pms",),
        entity="Products",
        dimension="Timeliness",
        severity="Medium",
        question="Are the dates on every PMS product believable and in the right order?",
        why="The created date sets which reporting period includes the product. The first-marketed date affects "
            "which marketing pathway applies. An impossible date points to a typo or a bad data load and puts "
            "the product in the wrong period.",
        how_to_fix="Open the product in PMS. Compare its dates with the received date in eSUB or Rhapsody and with "
                   "the first-marketed date in the submission. If the first-marketed date was mistyped, correct "
                   "it in PMS. PMS sets the created and last-updated dates itself. If one of them is wrong, send "
                   "the PD number to the PMS product team.",
        record_label="products",
        filters=_ALL_FILTERS,
        date_label="Product created date",
        technical_note=_BASE_NOTE + " Fails when create_date is NULL; when CAST(create_date AS DATE) is before "
        "2009-06-22 (Tobacco Control Act); when any of create_date / update_date / date_first_us_market (as "
        "dates) is later than current_date() + 1 day (one day allowed for time-zone differences); or when "
        "CAST(update_date AS DATE) < CAST(create_date AS DATE) (same-day differences are ignored; only "
        "evaluated when create_date is itself plausible). Products whose created date is missing or implausible "
        "are dated by halo_insert_ts so the date filter does not hide them.",
        assumptions=(
            _ID_ASSUMPTION,
            _SUB_ASSUMPTION,
            "The first-marketed date is when the product went on sale in the US. It is not a planned launch "
            "date, so it should never be in the future.",
            "PMS sets the created and last-updated dates when a record is saved. No PMS product can have a "
            "created date before the Tobacco Control Act of June 22, 2009.",
        ),
        sql=_sql(r"""
, f AS (
  SELECT
    prod.*,
    CAST(create_date AS DATE)                                       AS created_d,
    CAST(update_date AS DATE)                                       AS updated_d,
    CAST(date_first_us_market AS DATE)                              AS market_d,
    create_date IS NULL                                             AS no_created,
    coalesce(CAST(create_date AS DATE) > date_add(current_date(), 1), false)          AS created_future,
    coalesce(CAST(create_date AS DATE) < DATE'2009-06-22', false)                     AS created_too_early,
    coalesce(CAST(update_date AS DATE) > date_add(current_date(), 1), false)          AS updated_future,
    coalesce(CAST(date_first_us_market AS DATE) > date_add(current_date(), 1), false) AS market_future,
    coalesce(CAST(update_date AS DATE) < CAST(create_date AS DATE)
             AND CAST(create_date AS DATE) BETWEEN DATE'2009-06-22' AND date_add(current_date(), 1), false)
                                                                    AS updated_before
  FROM prod
),
g AS (
  SELECT f.*,
         coalesce(no_created OR created_future OR created_too_early OR updated_future OR market_future
                  OR updated_before, false) AS bad
  FROM f
)
SELECT
  record_key                                                      AS record_key,
  record_date                                                     AS record_date,
  submission_type                                                 AS submission_type,
  office                                                          AS office,
  bad                                                             AS is_issue,
  CASE
    WHEN no_created        THEN 'Blank created date'
    WHEN created_future    THEN 'Future created date'
    WHEN created_too_early THEN 'Created before 2009'
    WHEN updated_before    THEN 'Updated before created'
    WHEN updated_future    THEN 'Future update date'
    WHEN market_future     THEN 'Future market date'
  END                                                             AS issue_category,
  CASE WHEN bad THEN
    concat('Product ', record_key, ': ',
      concat_ws('; ',
        CASE WHEN no_created THEN 'the created date is blank' END,
        CASE WHEN created_future THEN concat('created date ', CAST(created_d AS STRING), ' is in the future') END,
        CASE WHEN created_too_early THEN concat('created date ', CAST(created_d AS STRING),
                                                ' is before the Tobacco Control Act (2009-06-22)') END,
        CASE WHEN updated_before THEN concat('last updated ', CAST(updated_d AS STRING), ', ',
                                             CAST(datediff(created_d, updated_d) AS STRING),
                                             ' day(s) before it was created (', CAST(created_d AS STRING), ')') END,
        CASE WHEN updated_future THEN concat('last-updated date ', CAST(updated_d AS STRING), ' is in the future') END,
        CASE WHEN market_future THEN concat('first-marketed date ', CAST(market_d AS STRING),
                                            ' is in the future') END),
      '.')
  END                                                             AS issue_reason,
  stn_raw                                                         AS `STN`,
  pd_number                                                       AS `PD number`,
  product_name_raw                                                AS `Product name`,
  created_d                                                       AS `Created date`,
  updated_d                                                       AS `Last updated`,
  market_d                                                        AS `First marketed (US)`,
  CAST(halo_insert_ts AS DATE)                                    AS `Date copied from PMS`
FROM g
"""),
    ),
]
