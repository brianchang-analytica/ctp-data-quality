# CTP Data Quality Center (Databricks App)

A Streamlit app for CTP data stewards to find **completeness**, **consistency** and **timeliness**
problems across **eSUB**, **Rhapsody**, **PMS** and the **Reporting warehouse (EDW)** — especially
records that should be identical 1-to-1 across systems but are not.

This repository holds two front ends over the same check engine:

**MVP (`app.py`, deployed by default)** — deliberately few signals so stewards can review and test:

| Page | What a steward does there |
|---|---|
| **Overview** | Five tiles (Across systems, eSUB, Rhapsody, PMS, Data feeds), each with one status and "N of M checks need attention", plus the 5 checks to start with. |
| **Checks** | Pick an area; checks are listed most-serious first with one number each. *Inspect* opens the records to fix, a plain-English "what's wrong" per record, why it matters / how to fix, and **Download to Excel**. |
| **Trace a submission** | Type one STN; see its key details from every system side by side, with differences highlighted. |
| **About** | How to read the app, the checks in this version (downloadable), and the rules still being confirmed. |

The only filter is the time period. The MVP shows a curated **core set of 24 checks**
(`dq/checks/core.py`): 10 cross-system 1-to-1 matches, 3–4 per system, and data-feed freshness.
Set `DQ_CHECK_SET=all` to show every check in the catalog in the same simple layout.

**v2 design (`app_v2.py`, kept for a future build)** — the fuller experience: health rings and
per-dimension meters, a "Do systems agree?" reconciliation page, sidebar filters (submission type,
organization, kind of problem, impact), trend and breakdown charts in each check, and a technical-details
mode. Run it with `streamlit run app_v2.py`. Pages live in `v2/views/`.

---

## Run it locally (demo mode)

With no warehouse configured the app runs on clearly-labelled **synthetic demo data**, so the
layout can be reviewed anywhere:

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Connecting to the data

The app does not need to be real time, so it reads a **daily snapshot** instead of running
every check against the source tables while a steward waits.

```
 source tables                 scheduled job (weekdays 5 AM)              Databricks App
 eSUB / Rhapsody / PMS  --->   jobs/refresh_snapshot.py runs every  --->  reads the results through
 Reporting (Unity Catalog)     check once and saves, in slvr_ctp_dq:     the SQL warehouse, applies
                                 dq_app_check_counts  grouped counts     the steward's filters
                                 dq_app_check_rows    problem records
                                 dq_app_check_runs    refresh log
```

* **Fast pages.** The app reads small results tables, so pages load in seconds.
* **Small nightly write.** Only problem records are saved in full. Everything else is saved as
  counts grouped by date, submission type, organization and problem type. PMS alone holds about
  26.7 million products, so saving every checked record is not practical.
* **Every filter still works.** The counts keep the same columns the filters use, so totals,
  trends and breakdowns match a live run exactly (`tools/smoke_snapshot.py` checks this).
* **Simple access.** The app's service principal needs `SELECT` on the results schema. Only the
  job needs to read the source tables.
* **Safe failures.** If a check fails during the refresh, it keeps the previous day's results,
  the app marks it as stale, and the job ends with an error so its alert fires.
* **Same SQL everywhere.** The job runs exactly the SQL the app would run live.

Two exceptions read the source tables live: **Trace a submission** (one STN at a time) and
`DQ_SOURCE=live`, which skips the snapshot entirely.

### Setup from a GFE without Git or the CLI

Follow [`DEPLOY_GFE.md`](DEPLOY_GFE.md). It uses `dist/ctp_dq_installer.py`, a single notebook
that writes the whole app into your workspace when you run it. Without any Python compute, use
`dist/app_bundle/` instead: three files (the whole app packed into `app.py`) that deploy straight
to Databricks Apps in live mode.

### Setup with a bundle (recommended)

`databricks.yml` defines both the app and the refresh job.

```bash
databricks bundle deploy -t prod --var="warehouse_id=<SQL warehouse id>"
databricks bundle run ctp_dq_refresh -t prod      # first refresh; then it runs on schedule
databricks bundle run ctp_data_quality -t prod    # start the app
```

### Manual setup

1. **Code.** Import this folder into a Git folder or `/Workspace/Users/<you>/ctp_dq_app`.
2. **Refresh job.** *Workflows → Create job → Python script* task pointing at
   `jobs/refresh_snapshot.py`, with parameters
   `--catalog ctp_pd_catalog --results-schema slvr_ctp_dq --checks all`. Use serverless or a small
   job cluster. Schedule it for weekdays at 5:00 AM Eastern and add the data team to failure alerts.
   Run it once now.
3. **App.** *Compute → Apps → Create app → Custom*. Add an *SQL warehouse* resource with
   permission **Can use** and resource key **`sql-warehouse`**. Deploy from the folder.
4. **Share.** Give stewards *Can use* on the app.

### Permissions

```sql
-- The refresh job's run-as identity reads the sources and writes the results
GRANT USE CATALOG ON CATALOG ctp_pd_catalog TO `<job identity>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.slvr_ctp_core        TO `<job identity>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.gold_ctp_edw         TO `<job identity>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_esub_itrac      TO `<job identity>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_rhapsody_public TO `<job identity>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_pms_product     TO `<job identity>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_pms_reference   TO `<job identity>`;
GRANT USE SCHEMA, SELECT, CREATE TABLE, MODIFY ON SCHEMA ctp_pd_catalog.slvr_ctp_dq TO `<job identity>`;

-- The app's service principal (shown on the app page) reads the results
GRANT USE CATALOG ON CATALOG ctp_pd_catalog TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.slvr_ctp_dq TO `<app service principal>`;
-- For "Trace a submission", also grant SELECT on slvr_ctp_core and gold_ctp_edw.
```

**Optional: run as each steward.** Set `DQ_AUTH_MODE` to `user` in `app.yaml` and enable *User
authorization* with the `sql` scope. Queries then run with each steward's own Unity Catalog
permissions.

**Python packages.** Databricks Apps installs `requirements.txt` at deploy time. If the workspace
cannot reach PyPI, point it at the agency's package mirror. The refresh job needs no extra packages.

## Configuration (`app.yaml` env)

| Variable | Default | Purpose |
|---|---|---|
| `DATABRICKS_WAREHOUSE_ID` | from the `sql-warehouse` resource | Warehouse the app reads through. Unset → demo mode. |
| `DQ_SOURCE` | `snapshot` | `snapshot` reads the refresh job's results; `live` runs every check on demand. |
| `DQ_RESULTS_SCHEMA` | `slvr_ctp_dq` | Schema holding `dq_app_check_counts`, `dq_app_check_rows` and `dq_app_check_runs`. |
| `DQ_CATALOG` | `ctp_pd_catalog` | Unity Catalog catalog. |
| `DQ_SCHEMA_CORE`, `DQ_SCHEMA_EDW`, `DQ_SCHEMA_REPORTING`, `DQ_SCHEMA_DQ`, `DQ_SCHEMA_BRNZ_*` | see `dq/settings.py` | Override schema names if they differ between environments. |
| `DQ_AUTH_MODE` | `app` | `app` = service principal; `user` = on-behalf-of the signed-in steward. |
| `DQ_CACHE_MINUTES` | `30` | How long the app reuses a result before asking the warehouse again. |
| `DQ_DETAIL_ROW_LIMIT` | `5000` | Max flagged records loaded into a check's table/download. |
| `DQ_MAX_PARALLEL_QUERIES` | `6` | Checks run concurrently on the warehouse. |
| `DQ_DEMO_MODE` | `false` | Force synthetic data even with a warehouse. |
| `DQ_CHECK_SET` | `core` | MVP shows the curated core set; `all` shows every check. |

## Confirmed on real data

`tools/sql/confirm_match_keys.sql` was run on the real data in October 2026. Its results settled
how the systems link (for example Rhapsody `product_id` = PMS `uuid`) and what most status codes
mean. They are summarized in [`reference/confirmed_facts.md`](reference/confirmed_facts.md), and
every check cites the facts it relies on. Two checks were retired because the data showed they could
never fire: eSUB "STN being amended" (the field never holds an STN) and the eSUB vs Rhapsody FEI
comparison (no STN has an FEI in both systems).

Still open, listed on the **About** page:

* What eSUB's inactive, deleted and hold flags mean (script sections P2.01 to P2.06 were not returned).
* Whether Rhapsody submission status `1` means open and `0` closed (only the counts are known).
* Which bundle statuses count as finished (ACCEPTED, FILED and OCE REVIEW COMPLETED are treated as open).

To re-check after a schema change, run `tools/sql/confirm_match_keys.sql` (or the shorter
`confirm_match_keys_rerun.sql`) in the SQL editor.

## How it works

```
app.py, views/         MVP front end (default)
app_v2.py, v2/views/   fuller v2 design, kept for a future build
ui/simple.py           MVP rows, time-period control and check dialog
ui/                    shared glass theme, components, cached data access (+ v2 sidebar / check dialog)
dq/checks/core.py      the MVP's curated core check set and its five areas
dq/sqlfrag.py          shared SQL macros (STN_KEY, CO_NAME, ...) so every check matches records identically
dq/model.py            business vocabulary (systems, areas, kinds of problems) and the Check contract
dq/checks/*.py         the checks, one module per system or system pair
dq/checks/AUTHORING.md how to write a check (output contract + correctness rules)
dq/engine.py           wraps each check's SQL into summary / records / trend / breakdown queries
dq/lookup.py           "Trace a submission" queries
dq/db.py               Databricks SQL connection (service principal or on-behalf-of-user)
dq/demo.py             synthetic data for demo mode
jobs/refresh_snapshot.py  scheduled job that saves every check's results for the app
databricks.yml         bundle: the app plus the refresh job
tools/validate_checks.py  static validator: SQL, columns vs inventory, types, plain-language style
tools/smoke_duckdb.py     runs every check end to end in DuckDB on synthetic rows
tools/smoke_snapshot.py   proves snapshot totals, trends and breakdowns equal live ones
tools/build_installer.py  packs the app into dist/ctp_dq_installer.py, a one-file installer notebook
tools/build_app_bundle.py packs the app into dist/app_bundle/, three files for Databricks Apps
reference/table_inventory.csv  Unity Catalog column inventory the validator checks against
```

Every check is one SQL query that returns **one row per record evaluated** with a fixed set of
leading columns (`record_key, record_date, submission_type, office, is_issue, issue_category,
issue_reason`) followed by friendly detail columns. The engine wraps that one query to produce the
pass rate, the records list, the trend and the breakdowns, and applies the sidebar filters with
bound parameters. All user input reaches SQL as parameters, never as text.

### Adding or changing a check

1. Read `dq/checks/AUTHORING.md`.
2. Add a `Check(...)` to the module for that system (or pair).
3. `python tools/validate_checks.py --module <module>` — must report 0 errors.
4. Redeploy. The check appears on every relevant page automatically.

If a table's columns change, refresh `reference/table_inventory.csv` from `information_schema`
and re-run the validator.

### History

Every refresh appends one row per check to `dq_app_check_runs`, with its counts. A future
version can chart those runs to show whether each problem is shrinking over time.
