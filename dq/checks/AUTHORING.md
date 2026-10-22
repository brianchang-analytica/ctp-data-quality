# Writing a data-quality check

Every check is a `Check(...)` (see `dq/model.py`) in one of the modules listed in
`dq/checks/__init__.py`. `dq/checks/_example.py` is a complete, validated example — copy its shape.

The audience is **CTP data stewards who are not technical**. They think in *systems*
(eSUB, Rhapsody, PMS, Reporting) and *business things* (submissions/STNs, products,
bundles, companies, letters). They never see schema, table, or column names unless they
turn on "technical details".

## 1. The SQL output contract

The `sql` is a single Databricks SQL `SELECT` (CTEs allowed) that returns **one row per
record evaluated** — passing *and* failing — because the app computes
`pass rate = 1 - issues / rows`. The first seven output columns must be exactly:

| column | type | meaning |
|---|---|---|
| `record_key` | STRING | What a steward types into the source system to find the record (usually the STN; for products the product ID; for feeds the friendly feed name). Never NULL — use `coalesce(..., '(no STN)')`. |
| `record_date` | DATE | The business date the date filter applies to (received date for submissions, created date for products, load date for feeds). Use `coalesce` fallbacks so it is rarely NULL. Cast to DATE. |
| `submission_type` | STRING | Rhapsody's standardized submission type (`stdz_submission_type`) when the record can be tied to a submission, else `CAST(NULL AS STRING)`. |
| `office` | STRING | Rhapsody `organization` when available, else `CAST(NULL AS STRING)`. Despite the column name, this field holds the **applicant company** (confirmed on real data); stewards see it as "Organization". |
| `is_issue` | BOOLEAN | TRUE when the record fails. **Never NULL** — wrap in `coalesce(<expr>, false)`. |
| `issue_category` | STRING | Short grouping label for charts, NULL when not an issue. A handful of distinct values per check, e.g. `'Only in eSUB'`, `'Only in Rhapsody'`, `'Dates differ'`, `'Blank company'`. |
| `issue_reason` | STRING | One plain-English sentence specific to this row, NULL when not an issue. e.g. `Received 2024-03-02 in eSUB but 2024-03-05 in Rhapsody (3 days apart).` |

Then **3–10 detail columns** with friendly backtick aliases a steward understands:
`` `STN` ``, `` `Company` ``, `` `eSUB received date` ``, `` `Rhapsody received date` ``,
`` `Product name` ``. For cross-system checks put the two systems' values side by side and
prefix each with the system name. Always include STN and company when they are available.

Declare `filters=` with only the filters the output actually supports:
`FILTER_DATE` (almost always), `FILTER_SUBMISSION_TYPE` and `FILTER_OFFICE` only when those
columns are really populated. Set `date_label` to what `record_date` means ("Received date",
"Product created date", "Last refreshed").

## 2. Table references

Never hard-code the catalog. Use placeholders, which render to `` `catalog`.`schema` ``:

| placeholder | schema | what it is |
|---|---|---|
| `${core}` | slvr_ctp_core | Harmonized copies of eSUB (`esub_*`), Rhapsody (`rhapsody_*`) and PMS (`pms_*`) tables. **Prefer these.** |
| `${edw}` | gold_ctp_edw | Reporting warehouse dimensions/facts (`dim_submission`, `dim_product`, `dim_bundle`, `fact_bundle_submission_product`, ...) |
| `${reporting}` | gold_ctp_reporting | Derived metric tables behind dashboards |
| `${dq}` | slvr_ctp_dq | Existing standardization rules/mappings/log |
| `${brnz_esub}` `${brnz_rhap}` `${brnz_pms}` `${brnz_pms_ref}` `${brnz_cdr}` | bronze | Raw copies as ingested (mostly STRING typed). Use only for "did everything arrive" counts. |

The exact columns of every table are in `reference/table_inventory.csv`
(columns: `table_schema, table_name, column_name, data_type`). Look them up — do not guess.
Column comments are blank in Unity Catalog, so meaning must be inferred from names; when a
meaning or a code value is a guess, say so in `assumptions=`.

### Shared macros (required for keys, names and flags)

So that a record matches the same way in every check, normalize with the macros in
`dq/sqlfrag.py` instead of hand-written expressions. They are expanded automatically when
the SQL is rendered:

| macro | use for |
|---|---|
| `STN_KEY(x)` | every STN comparison or grouping (upper-case, all whitespace removed) |
| `NUM_KEY(x)` / `ANY_KEY(x)` | numeric IDs / IDs of unknown type (so `123.000000` = `123` = `'123'`) |
| `TXT(x)` | comparing free text (upper, trimmed, inner spaces collapsed; blank → NULL) |
| `CO_NAME(x)` | comparing company names (punctuation and INC/LLC/CO/CORP… removed) |
| `BLANK(x)` | "is this field blank?" and display values (trimmed; blank → NULL) |
| `FLAG_ON(x)` | Y/N-style flags such as eSUB `INACTIVE_IND` / `DELETED_IND` (never NULL) |

### Naming and dates

* Detail column aliases must not equal a contract column name, ignoring case
  (`` `Office` `` clashes with `office`). Use `` `Organization` ``, `` `Submission type (Rhapsody)` ``.
* A mistyped date (e.g. received in 2099) must not be hidden by the date filter. For
  `record_date`, take the first plausible business date (between 2009-06-22, the Tobacco
  Control Act, and today) from the candidates, falling back to the load date.
* A presence comparison may be written as two directional checks ("in eSUB but missing from
  Rhapsody", "in Rhapsody with no eSUB record") when their populations, grace periods or fix
  owners differ.

## 3. Correctness rules (the reviewer will check every one)

1. **Soft deletes.** Silver (`${core}`) rows: `coalesce(halo_is_deleted, false) = false`.
   Gold (`${edw}`) rows: `halo_is_current = true AND coalesce(halo_is_deleted, false) = false`.
   Also respect source flags where they exist — Rhapsody `deleted` (boolean), eSUB
   `DELETED_IND` / `INACTIVE_IND` (strings; treat `upper(trim(x)) IN ('Y','YES','1','T','TRUE')`
   as set). State in `technical_note` which flags you applied.
2. **No fan-out.** Joins must not multiply rows. Aggregate or de-duplicate the many side in a
   CTE first (e.g. `GROUP BY stn`, or `ROW_NUMBER() ... QUALIFY rn = 1` to pick the latest).
   The output must have one row per `record_key` unless the check is explicitly about pairs.
3. **Key types.** Keys are not typed consistently (e.g. `rhapsody_submission_products.bundle_id`
   is STRING but `rhapsody_bundle.id` is BIGINT; eSUB keys are DECIMAL). Databricks SQL runs in
   ANSI mode, where comparing a STRING to a number casts the string and **fails the whole query**
   on one bad value. Always compare keys as normalized strings:
   `upper(trim(CAST(a AS STRING))) = upper(trim(CAST(b AS STRING)))`, or use `try_cast`.
   For DECIMAL keys, cast through BIGINT first so `123.000000` matches `123`:
   `CAST(try_cast(x AS BIGINT) AS STRING)`.
4. **NULL logic.** `a <> b` is NULL when either side is NULL. Decide explicitly: a value present
   in one system and blank in the other is usually a mismatch — use `NOT (a <=> b)` with both
   sides normalized, and treat blank strings as NULL: `nullif(trim(x), '')`.
5. **Text comparison.** Normalize before comparing names: `upper(regexp_replace(trim(x), '\\s+', ' '))`,
   and for company names also strip punctuation and common suffixes (INC, LLC, LTD, CO, CORP,
   CORPORATION, COMPANY) so "Acme Inc." equals "ACME, INC". Report the *original* values in the
   detail columns. Write SQL in Python raw strings (`r"""..."""`) so backslashes survive.
6. **Dates.** Use `datediff(end, start)` (days) and `current_date()`. Do not flag future dates
   that are legitimately in the future (goal dates, due dates). Allow a small tolerance where
   systems legitimately differ (e.g. timezone → ±1 day) and say so in `technical_note`.
7. **Dialect.** Databricks SQL only: `count_if`, `datediff`, `date_diff`, `try_cast`,
   `regexp_replace`, `rlike`, `QUALIFY`, `<=>`, `nullif`, `concat_ws`, `array_join`,
   `collect_set`. No Oracle/Postgres syntax (`NVL2`, `::`, `ILIKE` is fine in DBSQL but avoid).
   No `SELECT *` in the outer query.
8. **Performance.** Runs live against full tables on a SQL warehouse. No cross joins, no
   correlated subqueries; prefer pre-aggregated CTEs and equi-joins.
9. **Denominator.** Choose the population deliberately and describe it in `technical_note`
   (e.g. "all non-deleted Rhapsody submissions received since 2016"). A presence check between
   two systems uses a FULL OUTER JOIN on the matching key so "only in A" and "only in B" are both
   counted, with `record_key` = the key from whichever side exists.
10. **Thresholds.** Defaults are green at ≥98% pass, red below 90%. For checks where any failure
    is serious (duplicate STNs, a feed that stopped), tighten: `warn_below=0.999, fail_below=0.99`.

## 4. Steward-facing text

* `title` — states the problem in ≤ 90 characters: "Submissions logged in eSUB but missing from Rhapsody".
* `question` — the yes/no question the check answers: "Does every submission logged in eSUB appear in Rhapsody?"
* `why` — 1–2 sentences on the business consequence (wrong counts, missed deadlines, broken search).
* `how_to_fix` — concrete steps a steward can take (which system to open, what to compare, who owns it).
* Never use: null, join, table, schema, column, key, boolean, halo, bronze/silver/gold, SQL.
  Say "blank", "missing", "linked", "record", "field", "ID".
* `severity`: **High** = affects official counts, review clocks/deadlines or regulatory records;
  **Medium** = makes reports/search unreliable; **Low** = cosmetic or rare.
* `technical_note` — for engineers: tables, match keys, filters, tolerance. Table names OK here.
* `assumptions` — plain-English list of anything unconfirmed (match keys, code meanings).

## 5. Validate

From the `ctp_dq_app` folder:

```
python tools/validate_checks.py --module <your_module>
```

Zero ERRORs is required. Every WARN must be fixed, or — only for a DuckDB dialect gap you have
confirmed is not a real problem — explained in a `# validator:` comment above the check.
