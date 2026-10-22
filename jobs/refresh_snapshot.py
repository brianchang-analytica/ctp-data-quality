"""Scheduled refresh: run every data-quality check and save the results for the app.

Runs as a Databricks job (Python script task) on a schedule, e.g. weekdays at 5 AM.
For each check it runs the check's SQL (exactly what the app would run live) once,
into a staging table, and from it replaces that check's slice of

    <catalog>.<results schema>.dq_app_check_counts  records checked / with a problem, grouped by
                                                    date, submission type, organization and problem type
    <catalog>.<results schema>.dq_app_check_rows    the records WITH a problem, one row each
    <catalog>.<results schema>.dq_app_check_runs    one row per check per refresh (status, counts, errors)

Only problem records are kept in full. PMS alone holds about 26.7 million products, so
saving every checked record would rewrite hundreds of millions of rows a night; the
grouped counts give the app the same totals, trends and breakdowns under any filter.

The app (DQ_SOURCE=snapshot) reads only these tables, so pages load fast and the app's
service principal needs SELECT on the results schema only.

A check that fails keeps its previous results; the job finishes the other checks and
then exits with an error so the job's failure notification fires.

Usage (job parameters):
    --catalog ctp_pd_catalog --results-schema slvr_ctp_dq [--checks all|core] [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", default=os.getenv("DQ_CATALOG", "ctp_pd_catalog"))
    ap.add_argument("--results-schema", default=os.getenv("DQ_RESULTS_SCHEMA", "slvr_ctp_dq"))
    ap.add_argument("--checks", choices=["all", "core"], default="all")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--app-root", default=None, help="folder containing the dq package (defaults to this file's parent)")
    args, _ = ap.parse_known_args()
    return args


ARGS = _parse_args()
os.environ["DQ_CATALOG"] = ARGS.catalog
os.environ["DQ_RESULTS_SCHEMA"] = ARGS.results_schema
os.environ["DQ_SOURCE"] = "live"  # the job runs the checks' own SQL
APP_ROOT = Path(ARGS.app_root) if ARGS.app_root else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from pyspark.sql import SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from pyspark.sql import types as T  # noqa: E402

from dq.model import CONTRACT_COLUMNS, Check  # noqa: E402
from dq.settings import SETTINGS  # noqa: E402

_SCHEMA = f"{SETTINGS.catalog}.{SETTINGS.results_schema}"
ROWS = f"{_SCHEMA}.{SETTINGS.rows_table_name}"
COUNTS = f"{_SCHEMA}.{SETTINGS.counts_table_name}"
RUNS = f"{_SCHEMA}.{SETTINGS.runs_table_name}"

RUNS_SCHEMA = T.StructType([
    T.StructField("run_id", T.StringType()),
    T.StructField("check_id", T.StringType()),
    T.StructField("check_title", T.StringType()),
    T.StructField("started_at", T.TimestampType()),
    T.StructField("finished_at", T.TimestampType()),
    T.StructField("status", T.StringType()),
    T.StructField("records_checked", T.LongType()),
    T.StructField("issues", T.LongType()),
    T.StructField("detail_columns", T.StringType()),
    T.StructField("error", T.StringType()),
])

# Columns the app filters and groups by; the counts table keeps exactly these.
_GROUP_COLS = ["record_date", "submission_type", "office", "issue_category"]


def create_tables(spark: SparkSession) -> None:
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {COUNTS} (
          check_id STRING, run_id STRING, refreshed_at TIMESTAMP,
          record_date DATE, submission_type STRING, office STRING COMMENT 'Rhapsody Organization (applicant company)',
          issue_category STRING, checked BIGINT, failed BIGINT
        )
        USING DELTA
        PARTITIONED BY (check_id)
        COMMENT 'CTP Data Quality app: records checked and with a problem per check, grouped by the app filter columns (latest refresh).'
    """)
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {ROWS} (
          check_id STRING, run_id STRING, refreshed_at TIMESTAMP,
          record_key STRING, record_date DATE, submission_type STRING, office STRING,
          is_issue BOOLEAN, issue_category STRING, issue_reason STRING,
          details STRING COMMENT 'The check''s detail columns as a JSON object, in display order'
        )
        USING DELTA
        PARTITIONED BY (check_id)
        COMMENT 'CTP Data Quality app: records with a problem, one row each (latest refresh). Written by jobs/refresh_snapshot.py.'
    """)
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {RUNS} (
          run_id STRING, check_id STRING, check_title STRING,
          started_at TIMESTAMP, finished_at TIMESTAMP, status STRING,
          records_checked BIGINT, issues BIGINT, detail_columns STRING, error STRING
        )
        USING DELTA
        COMMENT 'CTP Data Quality app: one row per check per refresh. Written by jobs/refresh_snapshot.py.'
    """)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _replace_slice(df, table: str, check_id: str) -> None:
    """Overwrite only this check's partition; retry the rare concurrent-write conflict."""
    for attempt in range(3):
        try:
            (df.write.format("delta").mode("overwrite")
               .option("replaceWhere", f"check_id = '{check_id}'").saveAsTable(table))
            return
        except Exception as exc:
            if attempt == 2 or "Concurrent" not in type(exc).__name__ + str(exc):
                raise
            time.sleep(5 * (attempt + 1))


_runs_lock = threading.Lock()


def refresh_check(spark: SparkSession, check: Check, run_id: str) -> dict:
    started = _now()
    t0 = time.perf_counter()
    detail_cols: list[str] = []
    stage = f"{_SCHEMA}.dq_app_stage_{check.id.lower()}"
    try:
        df = spark.sql(check.render_sql(SETTINGS.placeholders()))
        names = df.columns
        if tuple(n.lower() for n in names[: len(CONTRACT_COLUMNS)]) != CONTRACT_COLUMNS:
            raise ValueError(f"output columns {names[:len(CONTRACT_COLUMNS)]} do not follow the contract")
        detail_cols = names[len(CONTRACT_COLUMNS):]
        packed = df.select(
            F.col("record_key").cast("string").alias("record_key"),
            F.col("record_date").cast("date").alias("record_date"),
            F.col("submission_type").cast("string").alias("submission_type"),
            F.col("office").cast("string").alias("office"),
            F.coalesce(F.col("is_issue").cast("boolean"), F.lit(False)).alias("is_issue"),
            F.col("issue_category").cast("string").alias("issue_category"),
            F.col("issue_reason").cast("string").alias("issue_reason"),
            (F.to_json(
                F.struct(*[F.col(f"`{c}`").cast("string").alias(c) for c in detail_cols]),
                {"ignoreNullFields": "false"},
            ) if detail_cols else F.lit("{}")).alias("details"),
        )
        # Run the check once; both outputs below read the staged result.
        packed.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(stage)
        staged = spark.table(stage)
        tag = [F.lit(check.id).alias("check_id"), F.lit(run_id).alias("run_id"),
               F.current_timestamp().alias("refreshed_at")]

        counts = (staged.groupBy(*_GROUP_COLS)
                  .agg(F.count(F.lit(1)).alias("checked"), F.sum(F.col("is_issue").cast("long")).alias("failed"))
                  .select(*tag, *_GROUP_COLS, "checked", "failed"))
        _replace_slice(counts, COUNTS, check.id)

        issues = staged.where(F.col("is_issue")).select(
            *tag, "record_key", "record_date", "submission_type", "office", "is_issue",
            "issue_category", "issue_reason", "details")
        _replace_slice(issues, ROWS, check.id)

        totals = spark.sql(
            f"SELECT SUM(checked) AS n, SUM(failed) AS issues FROM {COUNTS} WHERE check_id = '{check.id}'"
        ).collect()[0]
        result = {"status": "ok", "records_checked": int(totals["n"] or 0), "issues": int(totals["issues"] or 0),
                  "error": None}
    except Exception as exc:
        result = {"status": "error", "records_checked": None, "issues": None,
                  "error": f"{type(exc).__name__}: {exc}"[:4000]}
    finally:
        try:
            spark.sql(f"DROP TABLE IF EXISTS {stage}")
        except Exception:
            pass
    result.update(run_id=run_id, check_id=check.id, check_title=check.title, started_at=started,
                  finished_at=_now(), detail_columns=json.dumps(detail_cols), seconds=time.perf_counter() - t0)
    with _runs_lock:
        spark.createDataFrame([{k: result[k] for k in RUNS_SCHEMA.fieldNames()}], RUNS_SCHEMA) \
            .write.format("delta").mode("append").saveAsTable(RUNS)
    return result


def main() -> int:
    spark = SparkSession.builder.getOrCreate()
    try:
        spark.conf.set("spark.sql.ansi.enabled", "true")  # same strictness as a SQL warehouse
    except Exception:
        pass

    if ARGS.checks == "core":
        os.environ["DQ_CHECK_SET"] = "core"
        from dq.checks.core import active_checks

        checks = active_checks()
    else:
        from dq.checks import ALL_CHECKS

        checks = list(ALL_CHECKS)

    create_tables(spark)
    run_id = f"{_now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    print(f"Refreshing {len(checks)} checks into {_SCHEMA} (run {run_id})", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, ARGS.workers)) as pool:
        results = list(pool.map(lambda c: refresh_check(spark, c, run_id), checks))

    failed = [r for r in results if r["status"] != "ok"]
    for r in sorted(results, key=lambda r: r["check_id"]):
        detail = (f"{r['records_checked']:>12,} checked {r['issues']:>10,} problems" if r["status"] == "ok"
                  else r["error"][:300])
        print(f"{r['status']:5} {r['check_id']:48} {r['seconds']:7.1f}s  {detail}", flush=True)
    print(f"\n{len(results) - len(failed)} of {len(results)} checks refreshed.", flush=True)
    if failed:
        print("Failed checks keep their previous results. See the error column in " + RUNS, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
