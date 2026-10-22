# Deploy from your GFE (no Git in the workspace)

Use this guide when CDERONE says "GitHub is not enabled for this workspace" and the Databricks
CLI is not available. You import **one notebook**, run it, and it writes the whole app into your
workspace. Then you create the refresh job and the app in the Databricks UI.

Menu names can differ slightly between Databricks versions (for example **Workflows** is called
**Jobs & Pipelines** in newer workspaces).

## Before you start

* You can sign in to CDERONE from your GFE.
* You are allowed to create **Apps** and **Jobs**. If **Compute > Apps** is missing, ask the
  workspace admin to enable Databricks Apps for you.
* You know which **SQL warehouse** to use. Note its name.

## 1. Get the installer onto your GFE

The installer is one file: `dist/ctp_dq_installer.py` (about 360 KB).

* **If github.com opens on your GFE:** sign in as `brianchang-analytica`, open the
  `ctp-data-quality` repo, go to `dist/ctp_dq_installer.py`, and click **Download raw file**.
* **If GitHub is blocked:** copy the file to the GFE with an agency-approved transfer method.

## 2. Put the app in the workspace

1. In CDERONE, open **Workspace** and go to your home folder (**Users > your name**).
2. Click the **⋮** menu next to your folder (or right-click it) and choose **Import**.
3. Choose **File**, browse to `ctp_dq_installer.py`, and click **Import**. It opens as a notebook
   named `ctp_dq_installer`.
4. At the top right, click **Connect** and pick **Serverless** (or a cluster on Databricks Runtime
   13.3 LTS or later).
5. Click **Run all**. The last cell prints `Wrote 45 files to /Workspace/Users/<you>/ctp_dq_app`.
6. Refresh the workspace browser. A `ctp_dq_app` folder now holds the app.

## 3. Get write access for the results (one time)

The refresh job saves its results in `ctp_pd_catalog.slvr_ctp_dq`. Ask the owner of that schema
to run this in the SQL editor:

```sql
GRANT USE CATALOG ON CATALOG ctp_pd_catalog TO `<your email>`;
GRANT USE SCHEMA, SELECT, CREATE TABLE, MODIFY ON SCHEMA ctp_pd_catalog.slvr_ctp_dq TO `<your email>`;
```

You also need `SELECT` on the source schemas the checks read: `slvr_ctp_core`, `gold_ctp_edw`,
`brnz_esub_itrac`, `brnz_rhapsody_public`, `brnz_pms_product` and `brnz_pms_reference`.

If you would rather use a schema you already own, use its name in two places: the
`--results-schema` job parameter in step 4, and `DQ_RESULTS_SCHEMA` in
`ctp_dq_app/app.yaml` (open the file in the workspace and edit it).

## 4. Create and run the refresh job

1. Open **Workflows** and click **Create job**.
2. Name the job `CTP data quality refresh`.
3. Set up the task:
   * **Type:** Python script
   * **Source:** Workspace
   * **Path:** `/Workspace/Users/<you>/ctp_dq_app/jobs/refresh_snapshot.py`
   * **Compute:** Serverless (or a small job cluster)
   * **Parameters:** `["--catalog","ctp_pd_catalog","--results-schema","slvr_ctp_dq","--checks","core"]`
4. Click **Create**, then **Run now**. The first run uses `core` (the 24 checks the app shows) so
   it finishes sooner. Expect 15 to 60 minutes.
5. When the run is green, edit the task parameters and change `core` to `all`.
6. Add a schedule: **Add trigger > Scheduled**, weekdays at 5:00 AM, time zone
   **America/New_York**. Add your email under **Job notifications > On failure**.

If a run fails, the other checks still save their results. The reason is in the job run's output
and in the `error` column of `slvr_ctp_dq.dq_app_check_runs`.

## 5. Create the app

1. Open **Compute > Apps** and click **Create app**.
2. Choose **Create a custom app** and name it `ctp-data-quality`.
3. Under **App resources**, click **Add resource > SQL warehouse**. Pick your warehouse, set the
   permission to **Can use**, and set the resource key to **`sql-warehouse`**.
4. Click **Create app** and wait for the app compute to show **Running** (a few minutes).
5. Click **Deploy**, choose the folder `/Workspace/Users/<you>/ctp_dq_app`, and confirm.
   Follow progress under **Deployments**. The first deploy installs the Python packages.

## 6. Give the app read access

The app page shows the app's **service principal**. Copy its name or application ID and run this
in the SQL editor (or ask the schema owner to run it):

```sql
GRANT USE CATALOG ON CATALOG ctp_pd_catalog TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.slvr_ctp_dq   TO `<app service principal>`;
-- "Trace a submission" reads the sources live:
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.slvr_ctp_core TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.gold_ctp_edw  TO `<app service principal>`;
```

## 7. Open, check and share

1. Click the app's URL. The footer should read **Data as of** with the time of the job run.
2. Open **Checks** and inspect one check to confirm records load.
3. On the app page, open **Permissions** and give stewards **Can use**.

## If something goes wrong

| What you see | What to do |
|---|---|
| **Compute > Apps** is missing | Ask the workspace admin to enable Databricks Apps. |
| The deploy fails while installing packages | The workspace may not reach PyPI. Ask the platform team for the package mirror, then deploy again. |
| "The saved results have not been created yet" | The refresh job has not finished a run, or the app lacks `SELECT` on `slvr_ctp_dq` (step 6). |
| "You do not have permission" on a check | Missing grant from step 3 (job) or step 6 (app). |
| The job run is very slow or times out | Run with `core` first. Ask for a larger serverless budget or job cluster. |
| Import refuses the file | Make sure the file name ends in `.py` and choose **File**, not **URL**. |

## Updating the app later

1. On a machine with the code, run `python tools/build_installer.py`. It rebuilds
   `dist/ctp_dq_installer.py`.
2. Import the new installer into the workspace (replace the old notebook) and click **Run all**.
3. On the app page, click **Deploy** again. The refresh job picks up the new code on its next run.
