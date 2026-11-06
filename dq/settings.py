"""Runtime settings, read from environment variables (set in app.yaml).

Everything that differs between workspaces (catalog name, schema names,
warehouse) lives here so the check SQL never hard-codes it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def _ident(env_name: str, default: str) -> str:
    """Read a catalog/schema identifier and refuse anything that is not a plain name."""
    value = os.getenv(env_name, default).strip()
    if not _IDENT.match(value):
        raise ValueError(f"{env_name}={value!r} is not a valid Unity Catalog identifier")
    return value


def _flag(env_name: str, default: bool = False) -> bool:
    return os.getenv(env_name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    catalog: str = field(default_factory=lambda: _ident("DQ_CATALOG", "ctp_pd_catalog"))

    # Schema names. The keys are the placeholders check SQL uses: ${core}, ${edw}, ...
    schemas: dict[str, str] = field(
        default_factory=lambda: {
            "core": _ident("DQ_SCHEMA_CORE", "slvr_ctp_core"),
            "dq": _ident("DQ_SCHEMA_DQ", "slvr_ctp_dq"),
            "edw": _ident("DQ_SCHEMA_EDW", "gold_ctp_edw"),
            "reporting": _ident("DQ_SCHEMA_REPORTING", "gold_ctp_reporting"),
            "brnz_esub": _ident("DQ_SCHEMA_BRNZ_ESUB", "brnz_esub_itrac"),
            "brnz_rhap": _ident("DQ_SCHEMA_BRNZ_RHAPSODY", "brnz_rhapsody_public"),
            "brnz_pms": _ident("DQ_SCHEMA_BRNZ_PMS", "brnz_pms_product"),
            "brnz_pms_ref": _ident("DQ_SCHEMA_BRNZ_PMS_REF", "brnz_pms_reference"),
            "brnz_cdr": _ident("DQ_SCHEMA_BRNZ_CDR", "brnz_cdr_stage_archive"),
        }
    )

    warehouse_id: str = field(default_factory=lambda: os.getenv("DATABRICKS_WAREHOUSE_ID", "").strip())

    # Where results come from:
    #   "snapshot" -> read the tables written by the scheduled refresh job (jobs/refresh_snapshot.py).
    #                 Fast, and the app only needs SELECT on the results schema.
    #   "live"     -> run every check against the source tables on demand.
    source: str = field(default_factory=lambda: os.getenv("DQ_SOURCE", "snapshot").strip().lower())
    results_schema: str = field(default_factory=lambda: _ident("DQ_RESULTS_SCHEMA", "slvr_ctp_dq"))
    rows_table_name: str = field(default_factory=lambda: _ident("DQ_ROWS_TABLE", "dq_app_check_rows"))
    counts_table_name: str = field(default_factory=lambda: _ident("DQ_COUNTS_TABLE", "dq_app_check_counts"))
    runs_table_name: str = field(default_factory=lambda: _ident("DQ_RUNS_TABLE", "dq_app_check_runs"))

    # "app"  -> queries run as the app's service principal (simplest to set up)
    # "user" -> queries run as the signed-in steward (on-behalf-of-user), so
    #           Unity Catalog permissions are enforced per person.
    auth_mode: str = field(default_factory=lambda: os.getenv("DQ_AUTH_MODE", "app").strip().lower())

    # Demo mode shows synthetic results so the app can be previewed without a warehouse.
    force_demo: bool = field(default_factory=lambda: _flag("DQ_DEMO_MODE"))

    cache_minutes: int = field(default_factory=lambda: int(os.getenv("DQ_CACHE_MINUTES", "30")))
    detail_row_limit: int = field(default_factory=lambda: int(os.getenv("DQ_DETAIL_ROW_LIMIT", "5000")))
    max_parallel_queries: int = field(default_factory=lambda: int(os.getenv("DQ_MAX_PARALLEL_QUERIES", "6")))

    # Optional workspace file with the rules' wording and limits (see dq/rules.py), for example
    # /Workspace/Shared/ctp_dq/ctp_dq_rules.json. Empty = use the wording built into the app.
    rules_path: str = field(default_factory=lambda: os.getenv("DQ_RULES_PATH", "").strip())

    @property
    def demo_mode(self) -> bool:
        return self.force_demo or not self.warehouse_id

    @property
    def snapshot_mode(self) -> bool:
        return not self.demo_mode and self.source == "snapshot"

    @property
    def rows_table(self) -> str:
        return f"`{self.catalog}`.`{self.results_schema}`.`{self.rows_table_name}`"

    @property
    def counts_table(self) -> str:
        return f"`{self.catalog}`.`{self.results_schema}`.`{self.counts_table_name}`"

    @property
    def runs_table(self) -> str:
        return f"`{self.catalog}`.`{self.results_schema}`.`{self.runs_table_name}`"

    def placeholders(self) -> dict[str, str]:
        """Map of ${name} -> fully qualified `catalog`.`schema` used to render check SQL."""
        out = {name: f"`{self.catalog}`.`{schema}`" for name, schema in self.schemas.items()}
        out["catalog"] = f"`{self.catalog}`"
        return out


SETTINGS = Settings()
