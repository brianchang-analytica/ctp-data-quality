"""Rhapsody <-> PMS: does Rhapsody's view of each product agree with the PMS master record?

Rhapsody keeps its own copy of every product it reviews (name, category, flavor,
TP number, nicotine source, package, ...) and links products to submissions (STNs).
PMS is the master list of products. These checks compare the two, product by product:

  * presence  - linked in Rhapsody but missing from PMS; in PMS under an STN but
                not linked to that STN in Rhapsody
  * agreement - name, category/subcategory, flavor, TP number, nicotine source,
                package type/quantity, STN
  * freshness - Rhapsody's copy older than the latest change in PMS

All SQL is assembled from the shared building blocks below, so the product match
(Rhapsody product ID = PMS UUID, confirmed on real data: K24, K50) and every
normalization rule live in exactly one place. Placeholder product IDs (one value shared
by thousands of products, see PLACEHOLDER_ID_ROWS) are found from the data and removed
before any match, grouping or de-duplication. Keys, STNs, blank checks and free text use
the shared macros in dq/sqlfrag.py (STN_KEY, ANY_KEY, NUM_KEY, TXT, BLANK); the only
module-local normalization is `_label`, which builds on TXT and additionally ignores
punctuation for code/label values (category, flavor, ...). PMS code lists are matched
on their code only (confirmed: K52-K59).
"""

from __future__ import annotations

from dq.model import FILTER_DATE, FILTER_OFFICE, FILTER_SUBMISSION_TYPE, Check

# =========================================================================== #
# HOW A RHAPSODY PRODUCT IS MATCHED TO A PMS PRODUCT  -- change it here only.
# =========================================================================== #
# Confirmed on real data (Oct 2026), tools/sql/confirm_match_keys.sql Part 1:
#   * Rhapsody product_id = pms_product.uuid for 100% of products, regular and bulk lists together
#     (K24: 2,257,680 of 2,257,769 regular IDs; K50: 26,657,121 of 26,657,210 regular + bulk IDs).
#   * pms_product.id never matches a Rhapsody product_id: 0% on the product list, the submission-product
#     links and the bulk list (K22, K23, K29, K49). So there is no PMS record-number path and no fallback.
#   * The static IDs match 0.1% (K27); the PMS static ID and unique product number 0% (K25, K26, K28).
#   * TP numbers agree 100% (K51). They are compared once products are matched (RHAP_PMS_TP_NUMBER_DIFFERS),
#     never used to match them.
#
# RHAPSODY_PRODUCT_REF: field on rhapsody_products / rhapsody_bulk_products that holds the PMS reference.
#   As "product_id", submission links are matched on their own product_id.
# PMS_PRODUCT_REFS: the ONE pms_product field that reference equals. It stays a tuple, under this name,
#   because dq/lookup.py imports both constants; it must hold exactly one field.
# Both sides are normalized with the shared ANY_KEY macro.
RHAPSODY_PRODUCT_REF = "product_id"
PMS_PRODUCT_REFS = ("uuid",)

_RHAP_REF_LABEL = {
    "product_id": "Rhapsody product ID",
    "static_product_id": "Rhapsody static product ID",
    "static_product_id_number": "Rhapsody static product ID number",
}
_PMS_REF_LABEL = {"uuid": "PMS universal product ID (UUID)"}
if RHAPSODY_PRODUCT_REF not in _RHAP_REF_LABEL:
    raise ValueError(f"RHAPSODY_PRODUCT_REF must be one of {sorted(_RHAP_REF_LABEL)}")
if len(PMS_PRODUCT_REFS) != 1 or PMS_PRODUCT_REFS[0] not in _PMS_REF_LABEL:
    raise ValueError(f"PMS_PRODUCT_REFS must be exactly one of {sorted(_PMS_REF_LABEL)} (confirmed: ('uuid',))")
_PMS_REF = PMS_PRODUCT_REFS[0]
_PMS_REF_NAME = _PMS_REF_LABEL[_PMS_REF]

# PLACEHOLDER PRODUCT IDS. Confirmed on real data (Oct 2026) (confirm_match_keys.sql P4 U07 / U10): one
# Rhapsody product_id (regular + bulk lists, 26.67M rows) sits on 14,359 rows with different product names,
# and one pms_product.uuid sits on 14,368 PMS products. Every other product ID / UUID is unique. Joining two
# un-de-duplicated sides on such a value fans out to ~200 million rows, and de-duplicating on it collapses
# ~14k different products into one arbitrary product. So a value found on more than PLACEHOLDER_ID_ROWS
# product rows is a placeholder: the values are found from the data on every run (never hard-coded) and are
# removed BEFORE any match, grouping or de-duplication on Rhapsody product_id or on a PMS match field.
PLACEHOLDER_ID_ROWS = 100

# STN PREFIXES RHAPSODY HANDLES. Confirmed on real data (Oct 2026): only 59.8% of PMS STNs are in Rhapsody
# (K30: 25,847 of 43,222), and PMS holds 15,767 GF and 1,675 AS STNs that Rhapsody mostly lacks (P2.32). A
# check that expects every PMS STN in Rhapsody evaluates only STN prefixes (first two characters of
# STN_KEY) where at least this share of the prefix's PMS STNs is found in Rhapsody. The share is computed
# from the data on every run (CTE prefix_cover); no prefix is hard-coded.
STN_PREFIX_MIN_SHARE = 0.5

# Timing allowances (days), so records still being set up are not flagged.
PMS_LINK_GRACE_DAYS = 14  # a new PMS product has this long to be linked to its submission in Rhapsody
SYNC_GRACE_DAYS = 2  # a PMS change has this long to reach Rhapsody (overnight refresh + lakehouse load)
STALE_TOLERANCE_DAYS = 1  # Rhapsody's PMS date may trail PMS by this much (time zones)

CORE = "${core}"
_EARLIEST = "DATE '2009-06-22'"  # Tobacco Control Act: no CTP business date can be earlier

# --------------------------------------------------------------------------- #
# Normalization helpers (SQL expression builders on top of dq/sqlfrag.py macros)
# --------------------------------------------------------------------------- #

# Values that only say "nothing was provided"; after _label they look like this.
_NOT_PROVIDED = "('NA', 'NOTPROVIDED', 'NOTAPPLICABLE', 'NONEPROVIDED')"


def _label(expr: str) -> str:
    """Code / label as comparable text: shared TXT() with every non-alphanumeric removed
    ('E-Cigarette' = 'e cigarette' = 'ECIGARETTE'). The shared macros keep punctuation, so this
    stays module-local; it is used only for category / flavor / nicotine / package values."""
    return f"nullif(regexp_replace(TXT({expr}), '[^A-Z0-9]', ''), '')"


def _lob(expr: str) -> str:
    """_label, with values that only say 'N/A' / 'not provided' / 'not applicable' treated as blank."""
    lbl = _label(expr)
    return f"CASE WHEN {lbl} IN {_NOT_PROVIDED} THEN CAST(NULL AS STRING) ELSE {lbl} END"


def _txt_or_blank(expr: str) -> str:
    """Shared TXT(), with 'N/A' / 'not provided' / 'not applicable' treated as blank."""
    return f"CASE WHEN {_label(expr)} IN {_NOT_PROVIDED} THEN CAST(NULL AS STRING) ELSE TXT({expr}) END"


def _lead_num(expr: str) -> str:
    """Leading number of a quantity ('20', '20.0', '20 sticks' -> 20; '1,000' -> 1000); NULL if none. Never throws."""
    return (
        f"try_cast(nullif(regexp_extract(regexp_replace(TXT({expr}), ',', ''), '^([0-9]+([.][0-9]+)?)', 1), '') "
        f"AS DECIMAL(18,4))"
    )


def _shown(expr: str) -> str:
    """Original value for a steward-facing sentence, '(blank)' when empty."""
    return f"coalesce(BLANK({expr}), '(blank)')"


def _plausible(*candidates: str, fallback: str) -> str:
    """First candidate that is a plausible business date (2009-06-22 .. today), else the load date."""
    picks = [
        f"CASE WHEN CAST({c} AS DATE) BETWEEN {_EARLIEST} AND current_date() THEN CAST({c} AS DATE) END"
        for c in candidates
    ]
    return "coalesce(" + ", ".join(picks + [f"CAST({fallback} AS DATE)"]) + ")"


def _same(rhap: tuple[str, ...], pms: tuple[str, ...]) -> str:
    """TRUE when both sides are blank, or any Rhapsody form equals any PMS form. Never NULL."""
    rhs = ", ".join(pms)
    any_eq = " OR ".join(f"coalesce({r} IN ({rhs}), false)" for r in rhap)
    return f"((coalesce({', '.join(rhap)}) IS NULL AND coalesce({rhs}) IS NULL) OR {any_eq})"


def _stn_list(stn_expr: str) -> str:
    """Sorted list of up to 10 STNs for display, with '(+N more)' when there are more."""
    return (
        f"concat_ws(' ', array_join(slice(array_sort(collect_set({stn_expr})), 1, 10), ', '), "
        f"CASE WHEN size(collect_set({stn_expr})) > 10 "
        f"THEN concat('(+', CAST(size(collect_set({stn_expr})) - 10 AS STRING), ' more)') END)"
    )


def _with(*parts: str) -> str:
    return "WITH " + ",\n".join(p.strip() for p in parts) + "\n"


# --------------------------------------------------------------------------- #
# Shared building blocks (CTEs)
# --------------------------------------------------------------------------- #

_COPY_FIELDS = (
    "product_id, product_name, product_category, product_subcategory, characterizing_flavor, "
    "tp_number, nicotine_source, package_type, package_quantity, active, inactive_date, "
    "static_product_id, static_product_id_number, pms_created_date_time, pms_updated_date_time, "
    "halo_update_ts, halo_checksum_key"
)
_COPY_COLS = _COPY_FIELDS + ", coalesce(halo_insert_ts, halo_update_ts) AS load_ts"

# Rhapsody's product copy: regular + bulk product lists, one row per Rhapsody product ID.
#   rhap_copy_counted  every copy row with the number of rows sharing its product ID
#   rhap_placeholder   product IDs on more than PLACEHOLDER_ID_ROWS rows, one row each (never matched)
#   rhap_copy          every other product ID, de-duplicated only AFTER the placeholders are removed
CTE_RHAP_COPY = f"""
rhap_copy_all AS (
  SELECT {_COPY_COLS}, 1 AS copy_rank
  FROM {CORE}.rhapsody_products
  WHERE coalesce(halo_is_deleted, false) = false
  UNION ALL
  SELECT {_COPY_COLS}, 2 AS copy_rank
  FROM {CORE}.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
),
rhap_copy_counted AS (
  SELECT {_COPY_FIELDS}, load_ts, copy_rank,
         ANY_KEY(product_id)                              AS rhap_product_norm,
         count(*) OVER (PARTITION BY ANY_KEY(product_id)) AS n_rows
  FROM rhap_copy_all
  WHERE ANY_KEY(product_id) IS NOT NULL
),
rhap_placeholder AS (
  SELECT rhap_product_norm, max(n_rows) AS n_rows
  FROM rhap_copy_counted
  WHERE n_rows > {PLACEHOLDER_ID_ROWS}
  GROUP BY rhap_product_norm
),
rhap_copy AS (
  SELECT
    rhap_product_norm,
    BLANK(product_id)                      AS rhap_product_id,
    ANY_KEY({RHAPSODY_PRODUCT_REF})        AS match_key,
    product_name, product_category, product_subcategory, characterizing_flavor,
    tp_number, nicotine_source, package_type, package_quantity, active, inactive_date,
    pms_created_date_time, pms_updated_date_time, load_ts,
    coalesce(TXT(active) IN ('N', 'NO', '0', 'F', 'FALSE', 'INACTIVE'), false)
      OR coalesce(inactive_date <= current_timestamp(), false) AS rhap_inactive
  FROM rhap_copy_counted
  WHERE n_rows <= {PLACEHOLDER_ID_ROWS}
  QUALIFY row_number() OVER (
    PARTITION BY rhap_product_norm
    ORDER BY pms_updated_date_time DESC NULLS LAST, halo_update_ts DESC NULLS LAST, copy_rank,
             BLANK(product_id), halo_checksum_key) = 1
)"""

# With product_id as the reference, a link's own (normalized) product ID is the match value.
_LINK_FALLBACK = "l.rhap_product_norm" if RHAPSODY_PRODUCT_REF == "product_id" else "CAST(NULL AS STRING)"

# Rhapsody's submission <-> product links, one row per (STN, Rhapsody product), with the PMS match value.
# A link whose product ID is a placeholder (rhap_placeholder) keeps its row, with placeholder_rows set and
# NO match value, so it can never match PMS or be grouped with other products (needs CTE_RHAP_COPY).
CTE_LINKS = f"""
dead_bundles AS (
  SELECT NUM_KEY(id) AS bundle_norm
  FROM {CORE}.rhapsody_bundle
  WHERE id IS NOT NULL
  GROUP BY 1
  HAVING max(CASE WHEN coalesce(deleted, false) = false AND coalesce(halo_is_deleted, false) = false
                  THEN 1 ELSE 0 END) = 0
),
link_rows AS (
  SELECT stn, product_id, bundle_id, coalesce(halo_insert_ts, halo_update_ts) AS load_ts
  FROM {CORE}.rhapsody_submission_products
  WHERE coalesce(halo_is_deleted, false) = false
  UNION ALL
  SELECT stn, product_id, CAST(bundle_id AS STRING) AS bundle_id, coalesce(halo_insert_ts, halo_update_ts) AS load_ts
  FROM {CORE}.rhapsody_bulk_products
  WHERE coalesce(halo_is_deleted, false) = false
),
links AS (
  SELECT
    STN_KEY(l.stn)               AS stn_norm,
    ANY_KEY(l.product_id)        AS rhap_product_norm,
    min(BLANK(l.stn))            AS stn,
    min(BLANK(l.product_id))     AS rhap_product_id,
    min(l.load_ts)               AS link_load_ts
  FROM link_rows l
  LEFT JOIN dead_bundles d ON d.bundle_norm = ANY_KEY(l.bundle_id)
  WHERE d.bundle_norm IS NULL
    AND STN_KEY(l.stn) IS NOT NULL
    AND ANY_KEY(l.product_id) IS NOT NULL
  GROUP BY 1, 2
),
link_keys AS (
  SELECT l.stn_norm, l.stn, l.rhap_product_norm, l.rhap_product_id, l.link_load_ts,
         x.n_rows AS placeholder_rows,
         CASE WHEN x.rhap_product_norm IS NULL THEN coalesce(c.match_key, {_LINK_FALLBACK}) END AS match_key
  FROM links l
  LEFT JOIN rhap_placeholder x ON x.rhap_product_norm = l.rhap_product_norm
  LEFT JOIN rhap_copy c        ON c.rhap_product_norm = l.rhap_product_norm
)"""

# Rhapsody submissions, one row per normalized STN.
CTE_SUBS = f"""
subs AS (
  SELECT
    STN_KEY(stn)           AS stn_norm,
    BLANK(stn)             AS stn,
    date_received,
    stdz_submission_type,
    organization,
    coalesce(BLANK(abpc_company_name), BLANK(itrac_company), BLANK(abc_company)) AS company
  FROM {CORE}.rhapsody_submissions
  WHERE coalesce(halo_is_deleted, false) = false
    AND STN_KEY(stn) IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY STN_KEY(stn)
    ORDER BY date_received DESC NULLS LAST, halo_update_ts DESC NULLS LAST, BLANK(stn), halo_checksum_key) = 1
)"""

# Most recently received linked submission (plausible dates only; undated ones last, unlinked never;
# ties broken by STN so the pick is deterministic). 'YYYY-MM-DD|STN' sorts like the date.
_SUB_ORDER = (
    "CASE WHEN s.stn_norm IS NOT NULL THEN concat(CAST(coalesce(CASE WHEN s.date_received <= current_date() "
    "THEN s.date_received END, DATE '1900-01-01') AS STRING), '|', s.stn_norm) END"
)

# Per Rhapsody product: the submissions it is linked to (needs CTE_LINKS + CTE_SUBS). Placeholder product
# IDs are left out: grouping on one would merge thousands of different products into one row.
CTE_PROD_SUB = f"""
prod_sub AS (
  SELECT
    k.rhap_product_norm,
    min(k.rhap_product_id)                               AS rhap_product_id,
    max(k.match_key)                                     AS match_key,
    count(DISTINCT k.stn_norm)                           AS n_stn,
    {_stn_list('k.stn')}                                 AS stn_list,
    collect_set(k.stn_norm)                              AS stn_set,
    min(CASE WHEN s.date_received BETWEEN {_EARLIEST} AND current_date()
             THEN s.date_received END)                   AS first_received,
    min(k.link_load_ts)                                  AS link_load_ts,
    max_by(s.stdz_submission_type, {_SUB_ORDER})         AS submission_type,
    max_by(s.organization, {_SUB_ORDER})                 AS office,
    max_by(s.company, {_SUB_ORDER})                      AS company
  FROM link_keys k
  LEFT JOIN subs s ON s.stn_norm = k.stn_norm
  WHERE k.placeholder_rows IS NULL
  GROUP BY k.rhap_product_norm
)"""

_PMS_COLS = (
    "pms_id, pms_uuid, stdz_stn, pms_stn_norm, product_name, tp_number, nicotine_source, "
    "unique_product_id, create_date, update_date, ctgry_other_text, subctgry_other_text, "
    "flavor_other_text, pak_type_other_text, load_ts, pms_deleted"
)
# PMS products: one row per PMS product id (pms_rows, with its match value = ANY_KEY of the UUID), every
# product with a match value and the number of PMS products sharing that value (pms_keys_all), the same
# without placeholder values (pms_keys: a value on more than PLACEHOLDER_ID_ROWS products, i.e. the shared
# placeholder UUID), and one PMS product per remaining match value (pms_match). pms_rows is de-duplicated on
# the PMS record number (unique, never a placeholder), so the placeholder UUID never collapses products.
CTE_PMS = f"""
pms_rows AS (
  SELECT
    id                                   AS pms_id,
    uuid                                 AS pms_uuid,
    BLANK(stdz_stn)                      AS stdz_stn,
    STN_KEY(stdz_stn)                    AS pms_stn_norm,
    product_name, tp_number, nicotine_source, unique_product_id, create_date, update_date,
    ctgry_other_text, subctgry_other_text, flavor_other_text, pak_type_other_text,
    coalesce(halo_insert_ts, halo_update_ts) AS load_ts,
    coalesce(halo_is_deleted, false)     AS pms_deleted,
    ANY_KEY({_PMS_REF})                  AS match_key
  FROM {CORE}.pms_product
  WHERE id IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY id
    ORDER BY coalesce(halo_is_deleted, false), halo_update_ts DESC NULLS LAST, halo_checksum_key) = 1
),
pms_keys_all AS (
  SELECT match_key, {_PMS_COLS},
         count(*) OVER (PARTITION BY match_key) AS n_sharing
  FROM pms_rows
  WHERE match_key IS NOT NULL
),
pms_keys AS (
  SELECT match_key, {_PMS_COLS}
  FROM pms_keys_all
  WHERE n_sharing <= {PLACEHOLDER_ID_ROWS}
),
pms_match AS (
  SELECT match_key, {_PMS_COLS}
  FROM pms_keys
  QUALIFY row_number() OVER (
    PARTITION BY match_key ORDER BY pms_deleted, update_date DESC NULLS LAST, pms_id DESC) = 1
)"""

# Rhapsody products whose PMS match is a live (not deleted) PMS product, side by side.
CTE_MATCHED = f"""
matched AS (
  SELECT
    c.rhap_product_id,
    c.product_name             AS rhap_name,
    c.product_category         AS rhap_category,
    c.product_subcategory      AS rhap_subcategory,
    c.characterizing_flavor    AS rhap_flavor,
    c.tp_number                AS rhap_tp,
    c.nicotine_source          AS rhap_nicotine,
    c.package_type             AS rhap_pak_type,
    c.package_quantity         AS rhap_pak_qty,
    c.active                   AS rhap_active,
    c.pms_created_date_time    AS rhap_pms_created,
    c.pms_updated_date_time    AS rhap_pms_updated,
    pm.pms_id,
    pm.product_name            AS pms_name,
    pm.tp_number               AS pms_tp,
    pm.nicotine_source         AS pms_nicotine,
    pm.unique_product_id,
    pm.create_date             AS pms_created,
    pm.update_date             AS pms_updated,
    pm.load_ts                 AS pms_load_ts,
    pm.ctgry_other_text        AS pms_ctgry_other,
    pm.subctgry_other_text     AS pms_subctgry_other,
    pm.flavor_other_text       AS pms_flavor_other,
    pm.pak_type_other_text     AS pms_pak_type_other,
    {_plausible('pm.create_date', 'c.pms_created_date_time', fallback='coalesce(pm.load_ts, c.load_ts)')}
                               AS created_dt,
    ps.stn_list,
    ps.submission_type,
    ps.office,
    ps.company
  FROM rhap_copy c
  JOIN pms_match pm ON pm.match_key = c.match_key AND NOT pm.pms_deleted
  LEFT JOIN prod_sub ps ON ps.rhap_product_norm = c.rhap_product_norm
)"""

# PMS product details record (category, flavor, package), one row per id.
CTE_PMS_UNIQUE = f"""
pms_unique AS (
  SELECT id AS unique_id, ctgry_code, ctgry_other_text, subctgry_code, subctgry_other_text,
         flavor_code, flavor_other_text, pak_type_code, pak_type_other_text,
         pak_qty_val, pak_unit_code, pak_unit_other_text
  FROM {CORE}.pms_unique_product
  WHERE coalesce(halo_is_deleted, false) = false
    AND id IS NOT NULL
  QUALIFY row_number() OVER (PARTITION BY id ORDER BY halo_update_ts DESC NULLS LAST, halo_checksum_key) = 1
)"""


def _ref_list(name: str, table: str, has_inactive_date: bool = False) -> str:
    """A PMS reference list (code -> text), one text per code.

    A value matches on the list's code only (the same rule as PMS_PRODUCT_CATEGORY_NOT_RECOGNIZED in
    pms.py). Confirmed on real data (Oct 2026): PMS details records point to the category, subcategory,
    flavor, package type and nicotine source lists by code, never by the list's record number (K52-K59,
    P2.25; record-number matches are 0% for category, subcategory and flavor: K53, K55, K57), so a
    record-number match could only add false matches.
    The live entry wins: not retired (inactive_date in the past, on lists that have one), then active, then
    the latest update. Confirmed on real data (Oct 2026): each of the 9 pms_category codes has an active and
    an inactive entry with different names (P4 U12), so this order decides the name; pms_product_attribute
    codes are unique (P4 U14)."""
    retired = "coalesce(inactive_date <= current_timestamp(), false)" if has_inactive_date else "false"
    return f"""
{name}_all AS (
  SELECT {_label('code')} AS code_norm, BLANK(text) AS ref_text, active, update_date, id,
         {retired} AS dated_out
  FROM {CORE}.{table}
  WHERE coalesce(halo_is_deleted, false) = false
),
{name} AS (
  SELECT code_norm, ref_text
  FROM {name}_all
  WHERE code_norm IS NOT NULL
    AND ref_text IS NOT NULL
  QUALIFY row_number() OVER (
    PARTITION BY code_norm
    ORDER BY dated_out, coalesce(active, true) DESC, update_date DESC NULLS LAST, id) = 1
)"""


CTE_CAT_REF = _ref_list("cat_ref", "pms_category", has_inactive_date=True)
CTE_SUBCAT_REF = _ref_list("subcat_ref", "pms_subcategory", has_inactive_date=True)
CTE_ATTR_REF = _ref_list("attr_ref", "pms_product_attribute")  # this list has no inactive_date

# Common front half of every product-attribute comparison.
_MATCHED_BASE = (CTE_RHAP_COPY, CTE_LINKS, CTE_SUBS, CTE_PROD_SUB, CTE_PMS, CTE_MATCHED)

# --------------------------------------------------------------------------- #
# Shared technical notes and assumptions
# --------------------------------------------------------------------------- #

_MATCH_TECH = (
    f"PRODUCT MATCH (set once in RHAPSODY_PRODUCT_REF / PMS_PRODUCT_REFS at the top of dq/checks/rhapsody_pms.py): "
    f"ANY_KEY(Rhapsody {RHAPSODY_PRODUCT_REF}) = ANY_KEY(pms_product.{_PMS_REF}), with no other path or fallback. "
    "Confirmed on real data (Oct 2026): Rhapsody product_id = pms_product.uuid for 100% of products on the "
    "regular and bulk lists (K24, K50); pms_product.id never matches (0%: K22, K23, K29, K49); the static IDs "
    "match 0.1% (K27). PMS products with a blank uuid cannot be matched. ANY_KEY = numeric form when the value "
    "parses as a number, else upper/trim. PLACEHOLDERS (PLACEHOLDER_ID_ROWS): a Rhapsody product_id on more than "
    f"{PLACEHOLDER_ID_ROWS} copy rows (rhap_placeholder), or a PMS uuid on more than {PLACEHOLDER_ID_ROWS} "
    "PMS products (pms_keys_all.n_sharing), is found from the data on every run and removed before any match, "
    "grouping or de-duplication. Confirmed on real data (Oct 2026): one Rhapsody product_id sits on 14,359 rows "
    "with different names and one pms_product.uuid on 14,368 products (P4 U07, U10); every other product_id, "
    "uuid and pms_product.id is unique (P4). PMS rows are de-duplicated on pms_product.id, never on uuid. If "
    "several PMS rows still share a uuid: non-deleted first, then latest update_date, then highest id. NO "
    "FAN-OUT: every join is against a side reduced to one row per join value (rhap_copy, rhap_placeholder, subs, "
    "pms_match, link_pairs, key_stns, pms_flags, pms_shared, eval_prefix, pms_unique, the PMS lists), so each "
    "output row is one product. Confirmed on real data (Oct 2026): STNs are clean in every system (P2.33: no "
    "spaces, lower case, dashes or dots), and rhapsody_submissions.stn, rhapsody_bundle.id and "
    "pms_unique_product.id are unique (P4), so those de-duplication steps change nothing today. "
)
_COPY_TECH = (
    "Rhapsody product copy = slvr_ctp_core.rhapsody_products UNION ALL rhapsody_bulk_products "
    "(halo_is_deleted excluded; neither has a deleted flag). Placeholder product_ids are removed first, then "
    "one row per ANY_KEY(product_id) keeps the latest pms_updated_date_time. Products whose only ID is a "
    "placeholder are never compared. Confirmed on real data (Oct 2026): the bulk list has 24.4M rows on only "
    "10 STNs and 23 bundles (K16, K47), does not overlap the regular list (K15), and its product IDs are PMS "
    "UUIDs like the regular list's (K50). "
)
_LINK_TECH = (
    "Rhapsody links = rhapsody_submission_products UNION ALL (stn, product_id, bundle_id) of rhapsody_bulk_products, "
    "halo_is_deleted excluded; links whose bundle has only deleted/halo_is_deleted rhapsody_bundle rows are dropped "
    "(bundle_id STRING vs id BIGINT compared as ANY_KEY / NUM_KEY text); de-duplicated to one row per "
    "(STN_KEY(stn), ANY_KEY(product_id)). A link to a placeholder product_id gets no match value and is left out "
    "of the per-product grouping (prod_sub). "
)
_SUB_TECH = (
    "Submission type / organization / company come from the most recently received linked rhapsody_submissions "
    "row (halo_is_deleted excluded, one row per STN_KEY). Organization = rhapsody_submissions.organization, the "
    "applicant company (Confirmed on real data (Oct 2026): 3,679 distinct companies, not CTP offices, P2.17); it fills "
    "the office output column. Products with no linked submission show them blank and match the '(not "
    "recorded)' filter value. "
)
_PMS_TECH = (
    "PMS = slvr_ctp_core.pms_product, one row per id; rows with halo_is_deleted are never compared "
    "(they only drive the 'Deleted in PMS' category). "
)
_REF_TECH = (
    "PMS list lookups (_ref_list) match on _label(code) only and keep one entry per code: the live entry "
    "(inactive_date not passed, on pms_category / pms_subcategory), then active = true, then the latest "
    "update_date, then id. Confirmed on real data (Oct 2026): PMS category, subcategory, flavor, package type "
    "and nicotine source values link to their lists by code, never by the list's record number (K52-K59, "
    "P2.25; record-number matches 0%: K53, K55, K57), so the former record-number fallback was dropped. Each of "
    "the 9 pms_category codes has an active and an inactive entry with different names, and the active entry's "
    "name is used (P4 U12); pms_product_attribute codes are unique (P4 U14). "
)
_DATE_TECH = (
    "record_date = first plausible date (2009-06-22 .. today) among the candidates named below, else the "
    "lakehouse load date (halo_insert_ts), so mistyped dates are not hidden by the date filter. "
)
_LABEL_TECH = (
    "Values are compared with _label = TXT() with every non-alphanumeric removed; 'N/A', 'not provided', "
    "'not applicable' and 'none provided' count as blank. Both sides are also translated through the PMS list "
    "(matched on the list code only). Match when both are blank or ANY Rhapsody form (as stored, "
    "translated) equals ANY PMS form: the stored value or code, the translated name, the 'other' text and the "
    "'name - other' display (the last two only where PMS has them). "
)

# The Rhapsody -> PMS product match itself is settled (K24, K50; see _MATCH_TECH), so it is no longer listed
# as an assumption. What remains below are rules this module chose, not facts tested on the data.
_PLACEHOLDER_ASSUMPTION = (
    f"An ID used on more than {PLACEHOLDER_ID_ROWS} products is treated as a placeholder and is never used to "
    "match products."
)
_BULK_ASSUMPTION = (
    "Submission links from Rhapsody's bulk product list count the same as links from its regular product list."
)
_BUNDLE_ASSUMPTION = "Product links on a bundle marked deleted in Rhapsody are ignored."
_LABEL_ASSUMPTION = (
    "The comparison looks only at letters and digits, ignoring capital letters. 'N/A', 'not provided', "
    "'not applicable' and 'none provided' count as blank. Rhapsody's value matches when it equals the PMS "
    "code or the PMS name for that code. It also matches the 'other' description entered in PMS. Different "
    "wording for the same thing still counts as a difference. For example 'Tobacco' does not match "
    "'Tobacco-derived'."
)
_DETAILS_ASSUMPTION = (
    "Products with no PMS details record are skipped. The PMS check for missing details records reports them."
)

_PRODUCT_FILTERS = frozenset({FILTER_DATE, FILTER_SUBMISSION_TYPE, FILTER_OFFICE})


# --------------------------------------------------------------------------- #
# 1. Linked in Rhapsody, missing from PMS
# --------------------------------------------------------------------------- #

SQL_LINKED_NOT_IN_PMS = _with(
    CTE_RHAP_COPY, CTE_LINKS, CTE_SUBS, CTE_PROD_SUB, CTE_PMS,
    f"""
chk AS (
  SELECT
    ps.rhap_product_id               AS record_id,
    ps.rhap_product_id, ps.match_key, ps.n_stn, ps.stn_list,
    ps.submission_type, ps.office, ps.company,
    {_plausible('ps.first_received', 'c.pms_created_date_time', fallback='coalesce(c.load_ts, ps.link_load_ts)')}
                                     AS record_dt,
    c.product_name                   AS rhap_name,
    c.active                         AS rhap_active,
    coalesce(c.rhap_inactive, false) AS rhap_inactive,
    pm.match_key IS NOT NULL         AS in_pms,
    pm.pms_id,
    coalesce(pm.pms_deleted, false)  AS pms_deleted,
    CAST(NULL AS BIGINT)             AS placeholder_rows
  FROM prod_sub ps
  LEFT JOIN rhap_copy c  ON c.rhap_product_norm = ps.rhap_product_norm
  LEFT JOIN pms_match pm ON pm.match_key = ps.match_key
  UNION ALL
  -- A placeholder product ID cannot tell its products apart, so it is reported once per linked STN
  -- (link_keys is one row per STN and product ID; subs is one row per STN).
  SELECT
    concat(k.rhap_product_id, ' (STN ', k.stn, ')') AS record_id,
    k.rhap_product_id, CAST(NULL AS STRING), CAST(1 AS BIGINT), k.stn,
    s.stdz_submission_type, s.organization, s.company,
    {_plausible('s.date_received', fallback='k.link_load_ts')},
    CAST(NULL AS STRING), CAST(NULL AS STRING), false, false, CAST(NULL AS BIGINT), false,
    k.placeholder_rows
  FROM link_keys k
  LEFT JOIN subs s ON s.stn_norm = k.stn_norm
  WHERE k.placeholder_rows IS NOT NULL
)""",
) + r"""
SELECT
  coalesce(record_id, '(no product ID)')                               AS record_key,
  record_dt                                                            AS record_date,
  CAST(submission_type AS STRING)                                      AS submission_type,
  CAST(office AS STRING)                                               AS office,
  coalesce(placeholder_rows IS NOT NULL OR NOT in_pms OR pms_deleted, false) AS is_issue,
  CASE
    WHEN placeholder_rows IS NOT NULL    THEN 'Placeholder ID'
    WHEN match_key IS NULL               THEN 'No PMS reference'
    WHEN NOT in_pms AND rhap_inactive    THEN 'Missing (inactive)'
    WHEN NOT in_pms                      THEN 'Missing from PMS'
    WHEN pms_deleted                     THEN 'Deleted in PMS'
  END                                                                  AS issue_category,
  CASE
    WHEN placeholder_rows IS NOT NULL THEN
      concat('Rhapsody links product ', coalesce(rhap_product_id, '(no product ID)'), ' to ', stn_list, ', but ',
             CAST(placeholder_rows AS STRING),
             ' Rhapsody products share this ID, so none of them can be matched to PMS.')
    WHEN match_key IS NULL THEN
      concat('Rhapsody links product ', coalesce(rhap_product_id, '(no product ID)'), ' to ', stn_list,
             ' but gives no PMS reference for it.')
    WHEN NOT in_pms AND rhap_inactive THEN
      concat('Rhapsody links inactive product ', coalesce(rhap_product_id, '(no product ID)'), ' to ',
             stn_list, ', but no PMS product has this ID as its UUID.')
    WHEN NOT in_pms THEN
      concat('Rhapsody links product ', coalesce(rhap_product_id, '(no product ID)'), ' to ', stn_list,
             ', but no PMS product has this ID as its UUID.')
    WHEN pms_deleted THEN
      concat('Rhapsody links product ', coalesce(rhap_product_id, '(no product ID)'), ' to ', stn_list,
             ', but PMS product ', coalesce(CAST(pms_id AS STRING), '(no number)'), ' is deleted.')
  END                                                                  AS issue_reason,
  rhap_product_id                                                      AS `Product ID`,
  stn_list                                                             AS `STNs (Rhapsody)`,
  company                                                              AS `Company`,
  rhap_name                                                            AS `Name (Rhapsody)`,
  rhap_active                                                          AS `Active (Rhapsody)`,
  n_stn                                                                AS `Linked submissions`,
  CASE WHEN pms_deleted THEN CAST(pms_id AS STRING) END                AS `Deleted PMS ID`,
  placeholder_rows                                                     AS `Products sharing this ID`
FROM chk
"""

# --------------------------------------------------------------------------- #
# 2. In PMS under an STN, not linked to that STN in Rhapsody
# --------------------------------------------------------------------------- #

SQL_PMS_NOT_LINKED = _with(
    CTE_RHAP_COPY, CTE_LINKS, CTE_SUBS, CTE_PMS,
    f"""
link_pairs AS (
  SELECT DISTINCT stn_norm, match_key FROM link_keys WHERE match_key IS NOT NULL
),
key_stns AS (
  SELECT match_key, {_stn_list('stn')} AS rhap_stn_list
  FROM link_keys
  WHERE match_key IS NOT NULL
  GROUP BY match_key
),
rhap_stns AS (
  SELECT stn_norm FROM subs
  UNION
  SELECT stn_norm FROM links
),
pms_stns AS (
  SELECT DISTINCT pms_stn_norm
  FROM pms_rows
  WHERE NOT pms_deleted
    AND pms_stn_norm IS NOT NULL
),
prefix_cover AS (
  -- Per STN prefix (first two characters): how many distinct PMS STNs there are and how many Rhapsody has.
  -- Computed from the data on every run (rhap_stns is one row per STN, so the join cannot multiply rows).
  SELECT
    substr(p.pms_stn_norm, 1, 2)          AS stn_prefix,
    count(*)                              AS n_pms_stns,
    count_if(rs.stn_norm IS NOT NULL)     AS n_found
  FROM pms_stns p
  LEFT JOIN rhap_stns rs ON rs.stn_norm = p.pms_stn_norm
  GROUP BY substr(p.pms_stn_norm, 1, 2)
),
eval_prefix AS (
  -- Prefixes Rhapsody handles: at least {STN_PREFIX_MIN_SHARE:.0%} of their PMS STNs are in Rhapsody.
  SELECT stn_prefix, n_pms_stns, n_found
  FROM prefix_cover
  WHERE n_found >= {STN_PREFIX_MIN_SHARE} * n_pms_stns
),
pms_flags AS (
  SELECT
    k.pms_id,
    max(CASE WHEN lp.match_key IS NOT NULL THEN 1 ELSE 0 END) AS linked_to_own_stn,
    max(ks.rhap_stn_list)                                     AS rhap_stn_list
  FROM pms_keys k
  LEFT JOIN link_pairs lp ON lp.match_key = k.match_key AND lp.stn_norm = k.pms_stn_norm
  LEFT JOIN key_stns ks   ON ks.match_key = k.match_key
  WHERE NOT k.pms_deleted
  GROUP BY k.pms_id
),
pms_shared AS (
  -- PMS products whose UUID is a placeholder (shared by more than {PLACEHOLDER_ID_ROWS} products)
  SELECT pms_id, max(n_sharing) AS n_sharing
  FROM pms_keys_all
  WHERE n_sharing > {PLACEHOLDER_ID_ROWS}
  GROUP BY pms_id
),
chk AS (
  SELECT
    r.pms_id, r.stdz_stn, r.product_name,
    {_plausible('r.create_date', 'r.update_date', fallback='r.load_ts')} AS record_dt,
    coalesce(f.linked_to_own_stn, 0) = 1   AS linked_to_own_stn,
    f.rhap_stn_list,
    rs.stn_norm IS NOT NULL                AS stn_in_rhapsody,
    r.match_key IS NULL                    AS no_uuid,
    sh.n_sharing,
    ep.stn_prefix, ep.n_pms_stns, ep.n_found,
    s.stdz_submission_type, s.organization, s.company
  FROM pms_rows r
  JOIN eval_prefix ep     ON ep.stn_prefix = substr(r.pms_stn_norm, 1, 2)
  LEFT JOIN pms_flags f   ON f.pms_id = r.pms_id
  LEFT JOIN pms_shared sh ON sh.pms_id = r.pms_id
  LEFT JOIN rhap_stns rs  ON rs.stn_norm = r.pms_stn_norm
  LEFT JOIN subs s        ON s.stn_norm = r.pms_stn_norm
  WHERE NOT r.pms_deleted
    AND r.pms_stn_norm IS NOT NULL
    -- grace period, products created in PMS in the last {PMS_LINK_GRACE_DAYS} days may not be linked yet
    AND NOT coalesce(CAST(r.create_date AS DATE) > date_add(current_date(), -{PMS_LINK_GRACE_DAYS})
                     AND CAST(r.create_date AS DATE) <= current_date(), false)
)""",
) + rf"""
SELECT
  CAST(pms_id AS STRING)                                AS record_key,
  record_dt                                             AS record_date,
  CAST(stdz_submission_type AS STRING)                  AS submission_type,
  CAST(organization AS STRING)                          AS office,
  NOT linked_to_own_stn                                 AS is_issue,
  CASE
    WHEN linked_to_own_stn                          THEN NULL
    WHEN rhap_stn_list IS NOT NULL                  THEN 'Different STN'
    WHEN stn_in_rhapsody AND n_sharing IS NOT NULL  THEN 'Placeholder ID'
    WHEN stn_in_rhapsody AND no_uuid                THEN 'No UUID'
    WHEN stn_in_rhapsody                            THEN 'Not linked'
    ELSE 'STN not found'
  END                                                   AS issue_category,
  CASE
    WHEN linked_to_own_stn THEN NULL
    WHEN rhap_stn_list IS NOT NULL THEN
      concat('PMS lists product ', CAST(pms_id AS STRING), ' under ', stdz_stn,
             ', but Rhapsody links it only to ', rhap_stn_list, '.')
    WHEN stn_in_rhapsody AND n_sharing IS NOT NULL THEN
      concat('PMS lists product ', CAST(pms_id AS STRING), ' under ', stdz_stn,
             ', but Rhapsody does not link it to that STN. Its {_PMS_REF_NAME} is a placeholder shared by ',
             CAST(n_sharing AS STRING), ' PMS products, so it cannot be used to find the link.')
    WHEN stn_in_rhapsody AND no_uuid THEN
      concat('PMS lists product ', CAST(pms_id AS STRING), ' under ', stdz_stn,
             ', but it has no {_PMS_REF_NAME}, the ID Rhapsody uses to link a product, so Rhapsody cannot link it.')
    WHEN stn_in_rhapsody THEN
      concat('PMS lists product ', CAST(pms_id AS STRING), ' under ', stdz_stn,
             ', but Rhapsody does not link it to any submission.')
    ELSE
      concat('PMS lists product ', CAST(pms_id AS STRING), ' under ', stdz_stn,
             ', but that STN is not in Rhapsody.')
  END                                                   AS issue_reason,
  CAST(pms_id AS STRING)                                AS `PMS product ID`,
  stdz_stn                                              AS `STN (PMS)`,
  company                                               AS `Company`,
  product_name                                          AS `Name (PMS)`,
  rhap_stn_list                                         AS `STNs (Rhapsody)`,
  CASE WHEN stn_in_rhapsody THEN 'Yes' ELSE 'No' END    AS `Found in Rhapsody`,
  concat(stn_prefix, ': ', CAST(n_found AS STRING), ' of ', CAST(n_pms_stns AS STRING),
         ' PMS STNs found in Rhapsody')                 AS `STNs with this prefix in Rhapsody`,
  n_sharing                                             AS `Products sharing its ID`
FROM chk
"""

# --------------------------------------------------------------------------- #
# 3. Product name
# --------------------------------------------------------------------------- #

SQL_NAME = _with(
    *_MATCHED_BASE,
    """
cmp AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, created_dt,
    rhap_name, pms_name,
    TXT(rhap_name)  AS r_val,
    TXT(pms_name)   AS p_val
  FROM matched
)""",
) + f"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  created_dt                                                   AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  NOT (r_val <=> p_val)                                        AS is_issue,
  CASE
    WHEN r_val <=> p_val THEN NULL
    WHEN r_val IS NULL   THEN 'Blank in Rhapsody'
    WHEN p_val IS NULL   THEN 'Blank in PMS'
    ELSE 'Names differ'
  END                                                          AS issue_category,
  CASE WHEN NOT (r_val <=> p_val) THEN
    concat('Rhapsody names product ', coalesce(rhap_product_id, '(no product ID)'), ' "', {_shown('rhap_name')},
           '" but PMS names it "', {_shown('pms_name')}, '".')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_name                                                    AS `Name (Rhapsody)`,
  pms_name                                                     AS `Name (PMS)`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM cmp
"""

# --------------------------------------------------------------------------- #
# 4. Category / subcategory
# --------------------------------------------------------------------------- #

_P_CAT_OTHER = "coalesce(BLANK(u.ctgry_other_text), BLANK(m.pms_ctgry_other))"
_P_SUB_OTHER = "coalesce(BLANK(u.subctgry_other_text), BLANK(m.pms_subctgry_other))"
_P_CAT_DISP = f"nullif(concat_ws(' - ', coalesce(pc.ref_text, BLANK(u.ctgry_code)), {_P_CAT_OTHER}), '')"
_P_SUB_DISP = f"nullif(concat_ws(' - ', coalesce(psc.ref_text, BLANK(u.subctgry_code)), {_P_SUB_OTHER}), '')"

SQL_CATEGORY = _with(
    *_MATCHED_BASE, CTE_PMS_UNIQUE, CTE_CAT_REF, CTE_SUBCAT_REF,
    f"""
cmp AS (
  SELECT
    m.rhap_product_id, m.pms_id, m.stn_list, m.submission_type, m.office, m.company, m.created_dt,
    m.rhap_category, m.rhap_subcategory,
    {_P_CAT_DISP}                   AS pms_category,
    {_P_SUB_DISP}                   AS pms_subcategory,
    nullif(concat_ws(' / ', BLANK(u.ctgry_code), BLANK(u.subctgry_code)), '') AS pms_codes,
    {_lob('m.rhap_category')}       AS r_cat_raw,
    {_lob('rc.ref_text')}           AS r_cat_txt,
    {_lob('pc.ref_text')}           AS p_cat_txt,
    {_lob('u.ctgry_code')}          AS p_cat_code,
    {_lob(_P_CAT_OTHER)}            AS p_cat_other,
    {_lob(_P_CAT_DISP)}             AS p_cat_disp,
    {_lob('m.rhap_subcategory')}    AS r_sub_raw,
    {_lob('rsc.ref_text')}          AS r_sub_txt,
    {_lob('psc.ref_text')}          AS p_sub_txt,
    {_lob('u.subctgry_code')}       AS p_sub_code,
    {_lob(_P_SUB_OTHER)}            AS p_sub_other,
    {_lob(_P_SUB_DISP)}             AS p_sub_disp
  FROM matched m
  JOIN pms_unique u         ON u.unique_id = m.unique_product_id
  LEFT JOIN cat_ref pc      ON pc.code_norm = {_label('u.ctgry_code')}
  LEFT JOIN cat_ref rc      ON rc.code_norm = {_label('m.rhap_category')}
  LEFT JOIN subcat_ref psc  ON psc.code_norm = {_label('u.subctgry_code')}
  LEFT JOIN subcat_ref rsc  ON rsc.code_norm = {_label('m.rhap_subcategory')}
),
flags AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, created_dt,
    rhap_category, rhap_subcategory, pms_category, pms_subcategory, pms_codes,
    NOT {_same(('r_cat_raw', 'r_cat_txt'), ('p_cat_txt', 'p_cat_code', 'p_cat_other', 'p_cat_disp'))} AS cat_differs,
    NOT {_same(('r_sub_raw', 'r_sub_txt'), ('p_sub_txt', 'p_sub_code', 'p_sub_other', 'p_sub_disp'))} AS sub_differs
  FROM cmp
)""",
) + f"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  created_dt                                                   AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  cat_differs OR sub_differs                                   AS is_issue,
  CASE
    WHEN cat_differs AND sub_differs THEN 'Both differ'
    WHEN cat_differs                 THEN 'Category differs'
    WHEN sub_differs                 THEN 'Subcategory differs'
  END                                                          AS issue_category,
  CASE WHEN cat_differs OR sub_differs THEN
    concat('Rhapsody shows the category / subcategory of product ', coalesce(rhap_product_id, '(no product ID)'),
           ' as "', {_shown('rhap_category')}, ' / ', {_shown('rhap_subcategory')}, '" but PMS shows "',
           {_shown('pms_category')}, ' / ', {_shown('pms_subcategory')}, '".')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_category                                                AS `Category (Rhapsody)`,
  pms_category                                                 AS `Category (PMS)`,
  rhap_subcategory                                             AS `Subcategory (Rhapsody)`,
  pms_subcategory                                              AS `Subcategory (PMS)`,
  pms_codes                                                    AS `PMS codes`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM flags
"""

# --------------------------------------------------------------------------- #
# 5. Characterizing flavor
# --------------------------------------------------------------------------- #

_P_FLAVOR_OTHER = "coalesce(BLANK(u.flavor_other_text), BLANK(m.pms_flavor_other))"
_P_FLAVOR_DISP = f"nullif(concat_ws(' - ', coalesce(fa.ref_text, BLANK(u.flavor_code)), {_P_FLAVOR_OTHER}), '')"

SQL_FLAVOR = _with(
    *_MATCHED_BASE, CTE_PMS_UNIQUE, CTE_ATTR_REF,
    f"""
cmp AS (
  SELECT
    m.rhap_product_id, m.pms_id, m.stn_list, m.submission_type, m.office, m.company, m.created_dt,
    m.rhap_flavor,
    {_P_FLAVOR_DISP}                AS pms_flavor,
    BLANK(u.flavor_code)            AS pms_flavor_code,
    {_lob('m.rhap_flavor')}         AS r_raw,
    {_lob('ra.ref_text')}           AS r_txt,
    {_lob('fa.ref_text')}           AS p_txt,
    {_lob('u.flavor_code')}         AS p_code,
    {_lob(_P_FLAVOR_OTHER)}         AS p_other,
    {_lob(_P_FLAVOR_DISP)}          AS p_disp
  FROM matched m
  JOIN pms_unique u      ON u.unique_id = m.unique_product_id
  LEFT JOIN attr_ref fa  ON fa.code_norm = {_label('u.flavor_code')}
  LEFT JOIN attr_ref ra  ON ra.code_norm = {_label('m.rhap_flavor')}
),
flags AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, created_dt,
    rhap_flavor, pms_flavor, pms_flavor_code,
    coalesce(r_raw, r_txt) IS NULL                       AS r_blank,
    coalesce(p_txt, p_code, p_other, p_disp) IS NULL     AS p_blank,
    NOT {_same(('r_raw', 'r_txt'), ('p_txt', 'p_code', 'p_other', 'p_disp'))} AS differs
  FROM cmp
)""",
) + f"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  created_dt                                                   AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  differs                                                      AS is_issue,
  CASE
    WHEN NOT differs THEN NULL
    WHEN r_blank     THEN 'Blank in Rhapsody'
    WHEN p_blank     THEN 'Blank in PMS'
    ELSE 'Flavors differ'
  END                                                          AS issue_category,
  CASE WHEN differs THEN
    concat('Rhapsody shows the flavor of product ', coalesce(rhap_product_id, '(no product ID)'),
           ' as "', {_shown('rhap_flavor')}, '" but PMS shows "', {_shown('pms_flavor')}, '".')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_flavor                                                  AS `Flavor (Rhapsody)`,
  pms_flavor                                                   AS `Flavor (PMS)`,
  pms_flavor_code                                              AS `Flavor code (PMS)`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM flags
"""

# --------------------------------------------------------------------------- #
# 6. TP number
# --------------------------------------------------------------------------- #

SQL_TP = _with(
    *_MATCHED_BASE,
    f"""
cmp AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company,
    created_dt, rhap_name, rhap_tp, pms_tp,
    {_txt_or_blank('rhap_tp')}  AS r_val,
    {_txt_or_blank('pms_tp')}   AS p_val
  FROM matched
)""",
) + f"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  created_dt                                                   AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  NOT (r_val <=> p_val)                                        AS is_issue,
  CASE
    WHEN r_val <=> p_val THEN NULL
    WHEN r_val IS NULL   THEN 'Blank in Rhapsody'
    WHEN p_val IS NULL   THEN 'Blank in PMS'
    ELSE 'TP numbers differ'
  END                                                          AS issue_category,
  CASE WHEN NOT (r_val <=> p_val) THEN
    concat('Rhapsody shows TP number ', {_shown('rhap_tp')}, ' for product ',
           coalesce(rhap_product_id, '(no product ID)'), ' but PMS shows ', {_shown('pms_tp')}, '.')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_name                                                    AS `Name (Rhapsody)`,
  rhap_tp                                                      AS `TP number (Rhapsody)`,
  pms_tp                                                       AS `TP number (PMS)`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM cmp
"""

# --------------------------------------------------------------------------- #
# 7. Nicotine source
# --------------------------------------------------------------------------- #

SQL_NICOTINE = _with(
    *_MATCHED_BASE, CTE_ATTR_REF,
    f"""
cmp AS (
  SELECT
    m.rhap_product_id, m.pms_id, m.stn_list, m.submission_type, m.office, m.company,
    m.created_dt, m.rhap_name, m.rhap_nicotine,
    coalesce(pa.ref_text, BLANK(m.pms_nicotine))   AS pms_nicotine,
    {_lob('m.rhap_nicotine')}                      AS r_raw,
    {_lob('ra.ref_text')}                          AS r_txt,
    {_lob('m.pms_nicotine')}                       AS p_raw,
    {_lob('pa.ref_text')}                          AS p_txt
  FROM matched m
  LEFT JOIN attr_ref pa ON pa.code_norm = {_label('m.pms_nicotine')}
  LEFT JOIN attr_ref ra ON ra.code_norm = {_label('m.rhap_nicotine')}
),
flags AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, created_dt,
    rhap_name, rhap_nicotine, pms_nicotine,
    coalesce(r_raw, r_txt) IS NULL                                AS r_blank,
    coalesce(p_raw, p_txt) IS NULL                                AS p_blank,
    NOT {_same(('r_raw', 'r_txt'), ('p_raw', 'p_txt'))}          AS differs
  FROM cmp
)""",
) + f"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  created_dt                                                   AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  differs                                                      AS is_issue,
  CASE
    WHEN NOT differs THEN NULL
    WHEN r_blank     THEN 'Blank in Rhapsody'
    WHEN p_blank     THEN 'Blank in PMS'
    ELSE 'Nicotine sources differ'
  END                                                          AS issue_category,
  CASE WHEN differs THEN
    concat('Rhapsody shows the nicotine source of product ', coalesce(rhap_product_id, '(no product ID)'),
           ' as "', {_shown('rhap_nicotine')}, '" but PMS shows "', {_shown('pms_nicotine')}, '".')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_name                                                    AS `Name (Rhapsody)`,
  rhap_nicotine                                                AS `Nicotine source (Rhapsody)`,
  pms_nicotine                                                 AS `Nicotine source (PMS)`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM flags
"""

# --------------------------------------------------------------------------- #
# 8. Package type / quantity
# --------------------------------------------------------------------------- #

_P_PAK_OTHER = "coalesce(BLANK(u.pak_type_other_text), BLANK(m.pms_pak_type_other))"
_P_PAK_DISP = f"nullif(concat_ws(' - ', coalesce(pa.ref_text, BLANK(u.pak_type_code)), {_P_PAK_OTHER}), '')"

SQL_PACKAGE = _with(
    *_MATCHED_BASE, CTE_PMS_UNIQUE, CTE_ATTR_REF,
    f"""
cmp AS (
  SELECT
    m.rhap_product_id, m.pms_id, m.stn_list, m.submission_type, m.office, m.company, m.created_dt,
    m.rhap_name, m.rhap_pak_type, m.rhap_pak_qty,
    {_P_PAK_DISP}                   AS pms_pak_type,
    nullif(concat_ws(' ', BLANK(u.pak_qty_val),
                     coalesce(BLANK(u.pak_unit_other_text), BLANK(u.pak_unit_code))), '') AS pms_pak_qty,
    {_lob('m.rhap_pak_type')}       AS r_type_raw,
    {_lob('ra.ref_text')}           AS r_type_txt,
    {_lob('pa.ref_text')}           AS p_type_txt,
    {_lob('u.pak_type_code')}       AS p_type_code,
    {_lob(_P_PAK_OTHER)}            AS p_type_other,
    {_lob(_P_PAK_DISP)}             AS p_type_disp,
    {_lob('m.rhap_pak_qty')}        AS r_qty_lbl,
    {_lob('u.pak_qty_val')}         AS p_qty_lbl,
    {_lead_num('m.rhap_pak_qty')}   AS r_qty_num,
    {_lead_num('u.pak_qty_val')}    AS p_qty_num
  FROM matched m
  JOIN pms_unique u      ON u.unique_id = m.unique_product_id
  LEFT JOIN attr_ref pa  ON pa.code_norm = {_label('u.pak_type_code')}
  LEFT JOIN attr_ref ra  ON ra.code_norm = {_label('m.rhap_pak_type')}
),
flags AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, created_dt,
    rhap_name, rhap_pak_type, rhap_pak_qty, pms_pak_type, pms_pak_qty,
    NOT {_same(('r_type_raw', 'r_type_txt'), ('p_type_txt', 'p_type_code', 'p_type_other', 'p_type_disp'))}
                                                              AS type_differs,
    NOT ((r_qty_lbl IS NULL AND p_qty_lbl IS NULL)
         OR coalesce(r_qty_num = p_qty_num, false)
         OR coalesce(r_qty_lbl = p_qty_lbl, false))           AS qty_differs
  FROM cmp
)""",
) + f"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  created_dt                                                   AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  type_differs OR qty_differs                                  AS is_issue,
  CASE
    WHEN type_differs AND qty_differs THEN 'Both differ'
    WHEN type_differs                 THEN 'Type differs'
    WHEN qty_differs                  THEN 'Quantity differs'
  END                                                          AS issue_category,
  CASE WHEN type_differs OR qty_differs THEN
    concat('Rhapsody shows the package of product ', coalesce(rhap_product_id, '(no product ID)'), ' as "',
           {_shown('rhap_pak_type')}, '", quantity "', {_shown('rhap_pak_qty')}, '", but PMS shows "',
           {_shown('pms_pak_type')}, '", quantity "', {_shown('pms_pak_qty')}, '".')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_name                                                    AS `Name (Rhapsody)`,
  rhap_pak_type                                                AS `Package type (Rhapsody)`,
  pms_pak_type                                                 AS `Package type (PMS)`,
  rhap_pak_qty                                                 AS `Quantity (Rhapsody)`,
  pms_pak_qty                                                  AS `Quantity (PMS)`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM flags
"""

# --------------------------------------------------------------------------- #
# 9. STN on the PMS product vs the STN(s) Rhapsody links it to
# --------------------------------------------------------------------------- #

SQL_STN = _with(
    CTE_RHAP_COPY, CTE_LINKS, CTE_SUBS, CTE_PROD_SUB, CTE_PMS,
    f"""
chk AS (
  SELECT
    ps.rhap_product_id, ps.stn_list, ps.n_stn, ps.submission_type, ps.office, ps.company,
    pm.pms_id, pm.stdz_stn, pm.pms_stn_norm, pm.product_name AS pms_name,
    {_plausible('pm.create_date', 'ps.first_received', fallback='coalesce(pm.load_ts, ps.link_load_ts)')}
                                                                                   AS record_dt,
    pm.pms_stn_norm IS NULL                                                        AS no_pms_stn,
    pm.pms_stn_norm IS NULL OR NOT coalesce(array_contains(ps.stn_set, pm.pms_stn_norm), false) AS differs
  FROM prod_sub ps
  JOIN pms_match pm ON pm.match_key = ps.match_key AND NOT pm.pms_deleted
)""",
) + r"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  record_dt                                                    AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  differs                                                      AS is_issue,
  CASE
    WHEN NOT differs THEN NULL
    WHEN no_pms_stn  THEN 'Blank STN'
    ELSE 'Different STN'
  END                                                          AS issue_category,
  CASE
    WHEN NOT differs THEN NULL
    WHEN no_pms_stn THEN
      concat('PMS has no STN for product ', coalesce(rhap_product_id, '(no product ID)'),
             '. Rhapsody links it to ', stn_list, '.')
    ELSE
      concat('PMS lists product ', coalesce(rhap_product_id, '(no product ID)'), ' under ', stdz_stn,
             ', but Rhapsody links it to ', stn_list, '.')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stdz_stn                                                     AS `STN (PMS)`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  pms_name                                                     AS `Name (PMS)`,
  CAST(pms_id AS STRING)                                       AS `PMS product ID`
FROM chk
"""

# --------------------------------------------------------------------------- #
# 10. Rhapsody's copy older than the latest PMS change
# --------------------------------------------------------------------------- #

SQL_STALE = _with(
    *_MATCHED_BASE,
    f"""
cmp AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, rhap_name, rhap_active,
    CAST(coalesce(pms_updated, pms_created) AS DATE)             AS pms_changed,
    CAST(coalesce(rhap_pms_updated, rhap_pms_created) AS DATE)   AS rhap_refreshed,
    {_plausible('coalesce(pms_updated, pms_created)', 'pms_created', fallback='pms_load_ts')} AS record_dt
  FROM matched
),
flags AS (
  SELECT
    rhap_product_id, pms_id, stn_list, submission_type, office, company, rhap_name, rhap_active,
    pms_changed, rhap_refreshed, record_dt,
    datediff(pms_changed, rhap_refreshed) AS days_behind,
    coalesce(datediff(current_date(), pms_changed) >= {SYNC_GRACE_DAYS}
             AND (rhap_refreshed IS NULL OR datediff(pms_changed, rhap_refreshed) > {STALE_TOLERANCE_DAYS}),
             false) AS stale
  FROM cmp
)""",
) + r"""
SELECT
  coalesce(rhap_product_id, '(no product ID)')                 AS record_key,
  record_dt                                                    AS record_date,
  CAST(submission_type AS STRING)                              AS submission_type,
  CAST(office AS STRING)                                       AS office,
  stale                                                        AS is_issue,
  CASE
    WHEN NOT stale              THEN NULL
    WHEN rhap_refreshed IS NULL THEN 'No PMS date'
    WHEN days_behind > 30       THEN '31+ days behind'
    WHEN days_behind > 7        THEN '8-30 days behind'
    ELSE '2-7 days behind'
  END                                                          AS issue_category,
  CASE
    WHEN NOT stale THEN NULL
    WHEN rhap_refreshed IS NULL THEN
      concat('PMS last changed product ', coalesce(rhap_product_id, '(no product ID)'), ' on ',
             CAST(pms_changed AS STRING),
             ', but the Rhapsody copy has no PMS date and may never have been refreshed.')
    ELSE
      concat('PMS last changed product ', coalesce(rhap_product_id, '(no product ID)'), ' on ',
             CAST(pms_changed AS STRING), ', but the Rhapsody copy shows PMS as of ',
             CAST(rhap_refreshed AS STRING), ' (', CAST(days_behind AS STRING), ' days behind).')
  END                                                          AS issue_reason,
  rhap_product_id                                              AS `Product ID`,
  stn_list                                                     AS `STNs (Rhapsody)`,
  company                                                      AS `Company`,
  rhap_name                                                    AS `Name (Rhapsody)`,
  pms_changed                                                  AS `Changed in PMS`,
  rhap_refreshed                                               AS `PMS date (Rhapsody)`,
  days_behind                                                  AS `Days behind`,
  rhap_active                                                  AS `Active (Rhapsody)`
FROM flags
"""

# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

CHECKS: list[Check] = [
    Check(
        id="RHAP_PMS_LINKED_PRODUCT_NOT_IN_PMS",
        title="Linked Rhapsody products missing from PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Completeness",
        severity="High",
        question="Does every product linked to a submission in Rhapsody exist in PMS?",
        why="PMS is the master list of tobacco products. A product that is missing or deleted in PMS drops out "
            "of product counts and searches. Reviewers also cannot compare its details with the master record. "
            "A product that shares its ID with thousands of other products cannot be matched at all.",
        how_to_fix="Rhapsody's product ID is the product's UUID in PMS, so search PMS by UUID for the ID shown. "
                   "If PMS has the product under a different UUID, correct the "
                   "product on the submission in Rhapsody. If someone deleted it in PMS by mistake, ask the PMS "
                   "team to restore it. If the deletion was on purpose, ask the Rhapsody team to remove it from "
                   "the submission. 'No PMS reference' means Rhapsody does not say which PMS product it is. "
                   "'Placeholder ID' means Rhapsody gives this same ID to many different products, so the ID is "
                   "listed once for each linked STN. Ask the Rhapsody and PMS teams to give each product its own "
                   "ID.",
        sql=SQL_LINKED_NOT_IN_PMS,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Received date (earliest linked submission)",
        warn_below=0.99,
        fail_below=0.95,
        technical_note=(
            _MATCH_TECH + _LINK_TECH + _COPY_TECH + _SUB_TECH + _PMS_TECH + _DATE_TECH
            + "Population: every distinct Rhapsody product with at least one live submission link (one row per "
            "ANY_KEY(product_id), all its STNs listed, first 10 shown). Issue: no pms_product row has the product_id "
            "as its uuid, or the only match is halo_is_deleted. Confirmed on real data (Oct 2026): 100% of regular "
            "and bulk product IDs are PMS UUIDs (K50), so this check should flag few products beyond the placeholder "
            "ID. record_date candidates: earliest plausible date_received "
            "of the linked submissions, then the copy's pms_created_date_time; fallback = copy / link load date. "
            "Inactive = Rhapsody active in (N, NO, 0, F, FALSE, INACTIVE) or inactive_date in the past; reported "
            "as its own category, still counted as an issue. 'Placeholder ID' = a linked product_id found in "
            "rhap_placeholder (more than PLACEHOLDER_ID_ROWS copy rows). It is always an issue (the product has "
            "no unique ID), is never looked up in PMS and is reported once per linked STN: record_key = "
            "'<product ID> (STN <STN>)', submission type / organization / company and record_date (plausible "
            "date_received, else link load date) from that STN. UNION ALL of the two populations keeps "
            "record_key unique."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            _BULK_ASSUMPTION,
            _BUNDLE_ASSUMPTION,
            "A product marked inactive in Rhapsody should still exist in PMS. It gets its own category so it "
            "can be reviewed separately.",
        ),
    ),
    Check(
        id="RHAP_PMS_PMS_PRODUCT_NOT_LINKED_TO_ITS_STN",
        title="PMS products not linked to their STN in Rhapsody",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Completeness",
        severity="High",
        question="Does Rhapsody link each PMS product to the STN that PMS records, for STN types Rhapsody handles?",
        why="Reviewers may miss a product that Rhapsody does not show on its submission. Letters and decisions "
            "may then omit it. Product counts per submission will also disagree between the systems.",
        how_to_fix="Open the STN in Rhapsody and review its product list. If the product belongs there, ask the "
                   "Rhapsody team to add it. They can also re-send the product list from PMS. If PMS has the "
                   "wrong STN, correct the STN on the product in PMS. 'STN not found' usually means Rhapsody does "
                   "not have the submission yet, or the STN in PMS has a typo. 'Different STN' products also "
                   "appear in the check for differing product STNs. 'Placeholder ID' means the PMS product shares "
                   "its UUID with many other products, so a link cannot be traced. 'No UUID' means the PMS product "
                   "has no UUID, the ID Rhapsody uses to find it. In both cases ask the PMS team to give the "
                   "product its own UUID. Only STN types that Rhapsody mostly has are checked. The type is the "
                   "first two letters of the STN, such as PM. A type is skipped when fewer than half of its PMS "
                   "STNs are in Rhapsody, since Rhapsody does not handle that type. Products added to "
                   f"PMS in the last {PMS_LINK_GRACE_DAYS} days are not checked yet.",
        sql=SQL_PMS_NOT_LINKED,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date (PMS)",
        warn_below=0.99,
        fail_below=0.95,
        technical_note=(
            _MATCH_TECH + _LINK_TECH + _PMS_TECH + _DATE_TECH
            + "Population: every non-deleted pms_product row with a non-blank STN_KEY(stdz_stn) (one row per PMS "
            "id) whose STN prefix is evaluated, except products whose create_date is within the last "
            f"{PMS_LINK_GRACE_DAYS} days (PMS_LINK_GRACE_DAYS). STN PREFIX GATE (STN_PREFIX_MIN_SHARE = "
            f"{STN_PREFIX_MIN_SHARE:.0%}): prefix = substr(STN_KEY(stdz_stn), 1, 2). prefix_cover counts, per prefix, "
            "the distinct STNs of non-deleted PMS products and how many of them are in rhap_stns "
            "(rhapsody_submissions or any live link); only prefixes where n_found >= "
            f"{STN_PREFIX_MIN_SHARE} * n_pms_stns (eval_prefix, one row per prefix, inner-joined) are evaluated. "
            "It is computed from the data on every run and no prefix is hard-coded. Products under other "
            "prefixes are left out of the population (neither pass nor fail). Confirmed on real data (Oct 2026): "
            "only 59.8% of PMS STNs are in Rhapsody (K30: 25,847 of 43,222), and PMS holds 15,767 GF and 1,675 AS "
            "STNs that Rhapsody mostly lacks (P2.32), so without the gate those products would all fail as 'STN "
            "not found'. Pass when Rhapsody has a link (STN_KEY(stdz_stn), product) whose product_id equals the "
            "product's uuid (pms_keys, placeholders removed). Placeholder product_ids on the Rhapsody side have no "
            "match value, so they never count as a link. Category order: 'Different STN' (linked to other STNs) > "
            "'Placeholder ID' (STN is in Rhapsody, not linked, and the product's uuid is on more than "
            "PLACEHOLDER_ID_ROWS PMS products, pms_shared) > 'No UUID' (STN is in Rhapsody and the product's uuid "
            "is blank, so no Rhapsody link can name it) > 'Not linked' (STN exists in rhapsody_submissions or any "
            "link) > 'STN not found'. Without the placeholder rule these products passed whenever their STN had "
            "any placeholder link. Submission type / "
            "organization / company come from the Rhapsody submission with PMS's STN (blank when that STN is "
            "not in Rhapsody). record_date candidates: create_date, update_date. Overlaps "
            "RHAP_PMS_PRODUCT_STN_DIFFERS for products linked only to other STNs (seen from the PMS side here, "
            "from the Rhapsody side there)."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            "The STN on a PMS product identifies the product's original submission. Rhapsody should list the "
            "product on that submission.",
            f"Rhapsody handles an STN type when at least {STN_PREFIX_MIN_SHARE:.0%} of the PMS STNs of that type "
            "are in Rhapsody. The type is the first two letters of the STN. Products of other types are not "
            "checked.",
            f"A new PMS product should be linked to its submission in Rhapsody within {PMS_LINK_GRACE_DAYS} days.",
            _BULK_ASSUMPTION,
            _BUNDLE_ASSUMPTION,
        ),
    ),
    Check(
        id="RHAP_PMS_PRODUCT_NAME_DIFFERS",
        title="Product name differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does each product have the same name in Rhapsody and PMS?",
        why="Letters and decisions use the product name shown in Rhapsody. If it differs from the PMS master "
            "record, a letter can name the product wrongly. Searches by name also give different results in "
            "each system.",
        how_to_fix="Compare the two names in the details. PMS is the master record. If PMS is right, ask the "
                   "Rhapsody team to refresh the product from PMS. If PMS is wrong, ask the PMS team to correct "
                   "it. Differences in capital letters or extra spaces alone are not flagged.",
        sql=SQL_NAME,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "Population: Rhapsody product-copy rows whose match is a non-deleted PMS product. Compares "
            "TXT(rhapsody product_name) vs TXT(pms_product.product_name) null-safely (upper case with spaces "
            "trimmed and collapsed; same rule as the PMS vs Reporting name check); blank on one side only = issue. "
            "record_date candidates: pms create_date, then the copy's pms_created_date_time."
        ),
        assumptions=(_PLACEHOLDER_ASSUMPTION, _BULK_ASSUMPTION),
    ),
    Check(
        id="RHAP_PMS_CATEGORY_DIFFERS",
        title="Category or subcategory differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does each product have the same category and subcategory in Rhapsody and PMS?",
        why="Category and subcategory decide how reports group products (for example cigarettes versus "
            "e-cigarettes). A mismatch makes counts by product type differ between the two systems.",
        how_to_fix="Compare the two values in the details. PMS is the master record, so correct whichever system "
                   "is wrong. If the same pair of values shows on many products, the systems probably word the "
                   "category differently. Tell the data team, who can add a translation instead of fixing "
                   "products one by one.",
        sql=SQL_CATEGORY,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "Population: matched products that have a pms_unique_product details row (id = "
            "pms_product.unique_product_id, both BIGINT; products without one are counted by "
            "PMS_PRODUCT_DETAILS_RECORD_MISSING). PMS category/subcategory = ctgry_code / subctgry_code translated "
            "via pms_category / pms_subcategory; 'other' text from pms_unique_product, else pms_product. "
            + _LABEL_TECH + _REF_TECH
            + "Confirmed on real data (Oct 2026): every filled pms_product.unique_product_id finds its details "
            "record (K31). All 9 category codes on details records are pms_category codes (K52) and 36 of 37 subcategory "
            "codes are pms_subcategory codes (K54); none match a list record number (K53, K55). Subcategory code "
            "BIDI is not on the list (P2.26), so its products show the stored code untranslated. "
            "One row per product; issue if either level differs."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            _LABEL_ASSUMPTION,
            _DETAILS_ASSUMPTION,
            _BULK_ASSUMPTION,
        ),
    ),
    Check(
        id="RHAP_PMS_FLAVOR_DIFFERS",
        title="Flavor differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does each product have the same characterizing flavor in Rhapsody and PMS?",
        why="Flavor drives review and enforcement priorities (for example flavored e-cigarettes). Flavor reports "
            "and searches miss or miscount products when the two systems disagree.",
        how_to_fix="Compare both flavors with the submission documents. PMS is the master record, so correct "
                   "whichever system is wrong. If the same pair of values shows on many products, the systems "
                   "probably word the flavor differently. Tell the data team, who can add a translation.",
        sql=SQL_FLAVOR,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "Population: matched products that have a pms_unique_product details row. PMS flavor = "
            "pms_unique_product.flavor_code translated via pms_product_attribute; 'other' text from "
            "pms_unique_product, else pms_product.flavor_other_text. "
            + _LABEL_TECH + _REF_TECH
            + "Confirmed on real data (Oct 2026): all 6 flavor codes on details records are pms_product_attribute "
            "codes (K56) and none is a list record number (K57); attribute codes are unique (P4 U14), so each "
            "code has one name."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            _LABEL_ASSUMPTION,
            _DETAILS_ASSUMPTION,
            _BULK_ASSUMPTION,
        ),
    ),
    Check(
        id="RHAP_PMS_TP_NUMBER_DIFFERS",
        title="TP number differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does each product have the same TP number in Rhapsody and PMS?",
        why="The TP number identifies the product. If it differs, a TP number search finds the product in one "
            "system only. Matching records between the systems also becomes unreliable.",
        how_to_fix="Compare the two TP numbers in the details. PMS is the master record. If PMS is right, ask the "
                   "Rhapsody team to refresh the product from PMS. If PMS is wrong or blank, ask the PMS team to "
                   "correct it. Differences in capital letters or extra spaces alone are not flagged.",
        sql=SQL_TP,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "Compares TXT(rhapsody tp_number) vs TXT(pms_product.tp_number) null-safely (same rule as the PMS vs "
            "Reporting TP check); 'N/A', 'not provided', 'not applicable' and 'none provided' count as blank; "
            "blank on one side "
            "only = issue. Punctuation is significant ('TP-00123' <> 'TP00123') because a search by TP number "
            "would not find both. Confirmed on real data (Oct 2026): the TP number matches 100% between the "
            "systems (K51: 192,032 of 192,033 Rhapsody TP numbers on the regular and bulk lists are also PMS TP "
            "numbers), so few differences are expected; a rise points to a refresh problem. K51 tests the TP values "
            "as a set, so a TP number on the wrong product would still be flagged here. The TP number is compared "
            "only after products are matched on the UUID, never used to match them."
        ),
        assumptions=(_PLACEHOLDER_ASSUMPTION, _BULK_ASSUMPTION),
    ),
    Check(
        id="RHAP_PMS_NICOTINE_SOURCE_DIFFERS",
        title="Nicotine source differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does each product have the same nicotine source in Rhapsody and PMS?",
        why="Nicotine source (for example tobacco-derived versus non-tobacco nicotine) affects how a product is "
            "regulated and reported. If the systems disagree, reviewers may work from the wrong product "
            "description.",
        how_to_fix="Compare both values with the submission documents. PMS is the master record, so correct "
                   "whichever system is wrong. If the same pair of values shows on many products, the systems "
                   "probably word the source differently. Tell the data team, who can add a translation.",
        sql=SQL_NICOTINE,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "Compares rhapsody nicotine_source vs pms_product.nicotine_source (no details row needed); either "
            "side is translated via pms_product_attribute when it equals a list code. "
            + _LABEL_TECH + _REF_TECH
            + "Confirmed on real data (Oct 2026): all 4 PMS nicotine source values are on pms_product_attribute "
            "(K59), matched by code (P2.25)."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            "Rhapsody may store the nicotine source as a PMS code or as its name. A code is translated to its "
            "name with the PMS list before the comparison.",
            _LABEL_ASSUMPTION,
            _BULK_ASSUMPTION,
        ),
    ),
    Check(
        id="RHAP_PMS_PACKAGE_DIFFERS",
        title="Package type or quantity differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does each product have the same package type and quantity in Rhapsody and PMS?",
        why="Package type and quantity (for example a soft pack of 20 versus a box of 10) help identify a "
            "product. When Rhapsody and PMS disagree, letters may describe a product that differs from the "
            "master record.",
        how_to_fix="Compare the package values with the submission documents. PMS is the master record. If PMS "
                   "is right, ask the Rhapsody team to refresh the product from PMS. If PMS is wrong, ask the PMS "
                   "team to correct it. If the same pair of package types shows on many products, the systems "
                   "probably word the type differently. Tell the data team, who can add a translation.",
        sql=SQL_PACKAGE,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "Population: matched products that have a pms_unique_product details row. Package type: rhapsody "
            "package_type vs pms_unique_product.pak_type_code translated via pms_product_attribute ('other' text "
            "from pms_unique_product, else pms_product.pak_type_other_text). "
            + _LABEL_TECH + _REF_TECH
            + "Package quantity: rhapsody package_quantity vs pms_unique_product.pak_qty_val. Equal when both are "
            "blank or their _label texts are equal. Also equal when their leading numbers are equal (commas "
            "ignored; '20.0' and '20 sticks' both = 20; try_cast never throws). Units are shown, not compared. "
            "Confirmed on real data (Oct 2026): all 22 package type codes on details records are on "
            "pms_product_attribute (K58), matched by code (P2.25)."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            "Rhapsody's package type and quantity are copies of the PMS values.",
            "Package quantities are compared by number only. Units such as sticks or grams are not compared.",
            _LABEL_ASSUMPTION,
            _DETAILS_ASSUMPTION,
            _BULK_ASSUMPTION,
        ),
    ),
    Check(
        id="RHAP_PMS_PRODUCT_STN_DIFFERS",
        title="Product STN differs: Rhapsody vs PMS",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Consistency",
        severity="Medium",
        question="Does Rhapsody link each product to the STN that PMS records for it?",
        why="The STN identifies the submission that included the product. If PMS and Rhapsody disagree, the "
            "product is counted under the wrong submission. A search by STN also returns different products in "
            "each system.",
        how_to_fix="Check the submission documents to find the product's correct STN. Then correct whichever "
                   "system is wrong: the STN on the product in PMS, or the product's submission link in Rhapsody. "
                   "'Blank STN' means the PMS product has no STN.",
        sql=SQL_STN,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Product created date",
        technical_note=(
            _MATCH_TECH + _LINK_TECH + _COPY_TECH + _SUB_TECH + _PMS_TECH + _DATE_TECH
            + "Population: Rhapsody products with at least one live submission link whose match is a non-deleted "
            "PMS product (one row per Rhapsody product; placeholder product_ids have no match value and are left "
            "out). Rhapsody STNs are aggregated per product "
            "(collect_set of STN_KEY) so a product linked to several STNs is one row; pass when "
            "STN_KEY(pms_product.stdz_stn) is among them. Blank stdz_stn = issue. record_date candidates: pms "
            "create_date, then earliest plausible linked received date."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            "PMS records one STN per product, the product's original submission. A product that Rhapsody links "
            "to several submissions passes when PMS's STN is one of them.",
            _BULK_ASSUMPTION,
            _BUNDLE_ASSUMPTION,
        ),
    ),
    Check(
        id="RHAP_PMS_RHAPSODY_COPY_OUT_OF_DATE",
        title="Rhapsody product copy missing latest PMS change",
        systems=("rhapsody", "pms"),
        entity="Products",
        dimension="Timeliness",
        severity="Medium",
        question="Does Rhapsody have the latest PMS changes for each product?",
        why="Rhapsody keeps its own copy of each product's PMS details. Letters and reports in Rhapsody show old "
            "details when that copy falls behind PMS.",
        how_to_fix="Compare the product in both systems. If PMS has newer details, ask the Rhapsody team to "
                   "refresh the product from PMS. If many products fell behind on the same date, the automatic "
                   "PMS refresh has probably stopped. Report it to the data team instead of fixing products one "
                   f"by one. PMS changes from the last {SYNC_GRACE_DAYS} days are not flagged yet.",
        sql=SQL_STALE,
        record_label="products",
        filters=_PRODUCT_FILTERS,
        date_label="Last changed in PMS",
        technical_note=(
            _MATCH_TECH + _COPY_TECH + _PMS_TECH + _SUB_TECH + _DATE_TECH
            + "PMS last change = coalesce(pms_product.update_date, create_date); Rhapsody's view = "
            "coalesce(rhapsody pms_updated_date_time, pms_created_date_time), both cast to DATE. Issue when the "
            f"PMS change is at least {SYNC_GRACE_DAYS} days old (SYNC_GRACE_DAYS; future dates never flagged) and "
            f"is more than {STALE_TOLERANCE_DAYS} day after Rhapsody's PMS date (STALE_TOLERANCE_DAYS, for time "
            "zones), or Rhapsody has no PMS date. A Rhapsody date later than PMS is not flagged. record_date "
            "candidates: PMS last change, then PMS create_date."
        ),
        assumptions=(
            _PLACEHOLDER_ASSUMPTION,
            "Rhapsody's 'PMS updated' date is assumed to be the PMS change date when Rhapsody last copied the "
            "product. A later change in PMS means the copy is out of date.",
            f"Differences of {STALE_TOLERANCE_DAYS} day or less are allowed for time zones. PMS changes from the "
            f"last {SYNC_GRACE_DAYS} days get time to reach Rhapsody.",
            _BULK_ASSUMPTION,
        ),
    ),
]
