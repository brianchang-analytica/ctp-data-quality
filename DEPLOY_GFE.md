# Deploy from your GFE (no Git in the workspace)

Use this guide when CDERONE says "GitHub is not enabled for this workspace" and the Databricks
CLI is not available. You import **one notebook**, run it, and it writes the whole app into your
workspace. Then you create the refresh job and the app in the Databricks UI.

**No Python compute?** If running the installer fails with "You are not authorized to create
clusters" and no Serverless option is offered, skip to
[No Python compute: the 3-file app](#no-python-compute-the-3-file-app) at the end.

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
5. Click **Run all**. The last cell prints `Wrote 47 files to /Workspace/Users/<you>/ctp_dq_app`.
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
2. Open **Checks** and open one check to confirm records load.
3. On the app page, open **Permissions** and give stewards **Can use**.

## Optional: manage the rules in the workspace

The **Rule list** page shows every rule the app checks. Its wording and limits can come from a file
in the workspace, so you can change them without deploying the app again. This works with either
setup in this guide.

1. In the app, open **Rule list**, expand **Manage these rules in Databricks** and click
   **Download the rule file**. You get `ctp_dq_rules.json`.
2. In **Workspace**, open **Shared**, click **Create > Folder** and name it `ctp_dq`. Open it,
   click **⋮ > Import**, choose **File** and pick `ctp_dq_rules.json`. If Import refuses it, click
   **Create > File**, name it `ctp_dq_rules.json` and paste the contents.
3. Click the file's **⋮** menu and choose **Share**. Add the app's service principal (shown on the
   app page under **Authorization**) with **Can read**.
4. Open the app folder's `app.yaml` and add this under `env:`:

   ```yaml
     - name: DQ_RULES_PATH
       value: /Workspace/Shared/ctp_dq/ctp_dq_rules.json
   ```

5. On the app page, click **Deploy**. **Rule list** should now say "Wording from the workspace file".

To change a rule later, open `ctp_dq_rules.json` in the workspace and edit `title`,
`what_it_checks`, `why_it_matters`, `how_to_fix`, `impact`, `needs_attention_below_pct`,
`action_needed_below_pct` or `assumptions`. The app picks up the change within 10 minutes, or right
away after **Re-run all checks**. The checks' logic stays in the app, so a typo cannot break a check;
**Rule list** names any value it could not use.

## If something goes wrong

| What you see | What to do |
|---|---|
| **Compute > Apps** is missing | Ask the workspace admin to enable Databricks Apps. |
| The deploy fails while installing packages | The workspace may not reach PyPI. Ask the platform team for the package mirror, then deploy again. |
| "The saved results have not been created yet" | The refresh job has not finished a run, or the app lacks `SELECT` on `slvr_ctp_dq` (step 6). |
| "You do not have permission" on a check | Missing grant from step 3 (job) or step 6 (app). |
| The job run is very slow or times out | Run with `core` first. Ask for a larger serverless budget or job cluster. |
| Import refuses the file | Make sure the file name ends in `.py` and choose **File**, not **URL**. |
| **Rule list** says "Could not read the rule file" | Check that `DQ_RULES_PATH` matches the file's path exactly and that the app's service principal has **Can read** on it. |

## Updating the app later

1. On a machine with the code, run `python tools/build_installer.py`. It rebuilds
   `dist/ctp_dq_installer.py`.
2. Import the new installer into the workspace (replace the old notebook) and click **Run all**.
3. On the app page, click **Deploy** again. The refresh job picks up the new code on its next run.

## No Python compute: the 3-file app

Use this when your account cannot create clusters and has no serverless compute. The app runs on
Databricks Apps' own compute, so it needs no cluster. It runs in **live mode**: there is no
nightly refresh job, and the app runs the checks through the SQL warehouse when someone opens it.

The first visit after the results expire waits a few minutes while the 24 checks run against the
full tables. Results are then reused for 4 hours.

### 1. Get the three files onto your GFE

They are in the repo folder `dist/app_bundle/`:

* `app.py`: the whole app packed into one file (about 260 KB)
* `app.yaml`: the app settings, already set to live mode
* `requirements.txt`: the Python packages

On GitHub, open each file and click **Download raw file**. Or copy them with an agency-approved
transfer method.

### 2. Upload them into one workspace folder

1. In **Workspace**, go to your home folder and click **Create > Folder**. Name it
   `ctp_dq_app_live`.
2. Open the new folder. Click **⋮ > Import**, choose **File**, and import `app.py`.
3. Repeat for `app.yaml` and `requirements.txt`. The folder must hold exactly these three files.

If Import refuses `app.yaml` or `requirements.txt`, click **Create > File** instead, give it
the same name, and paste the file's contents.

If `app.py` opens as a notebook after import, delete it and import it again with **File**.
It must stay a plain file.

### 3. Create the app

1. Open **Compute > Apps** and click **Create app**.
2. Choose **Create a custom app** and name it `ctp-data-quality`.
3. Under **App resources**, click **Add resource > SQL warehouse**. Pick your warehouse, set the
   permission to **Can use**, and set the resource key to **`sql-warehouse`**.
4. Click **Create app** and wait for **Running**.
5. Click **Deploy** and choose the folder `/Workspace/Users/<you>/ctp_dq_app_live`.

### 4. Give the app read access to the source data

In live mode the app reads the source tables directly. Copy the app's service principal from the
app page and run this in the SQL editor (or ask the schema owner to):

```sql
GRANT USE CATALOG ON CATALOG ctp_pd_catalog TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.slvr_ctp_core        TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.gold_ctp_edw         TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_esub_itrac      TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_rhapsody_public TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_pms_product     TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.brnz_pms_reference   TO `<app service principal>`;
GRANT USE SCHEMA, SELECT ON SCHEMA ctp_pd_catalog.slvr_ctp_dq          TO `<app service principal>`;
```

### 5. Open, check and share

1. Click the app's URL. The first load can take a few minutes. The footer then reads
   **Live results from** with the time.
2. On the app page, open **Permissions** and give stewards **Can use**.

### Switching to the nightly refresh later

When you get Python compute, follow steps 2 to 4 at the top of this guide to set up the refresh
job. Then change `DQ_SOURCE` from `live` to `snapshot` in `app.yaml` and deploy again. Pages then
load in seconds.

### Updating the 3-file app

On a machine with the code, run `python tools/build_app_bundle.py`. Upload the new `app.py`
over the old one, then click **Deploy** on the app page.
