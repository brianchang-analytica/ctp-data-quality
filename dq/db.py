"""SQL warehouse access for the Databricks App.

Inside Databricks Apps the platform injects DATABRICKS_HOST plus the app's
service-principal credentials, and `databricks.sdk.core.Config()` picks them
up. With DQ_AUTH_MODE=user the steward's own token (forwarded by the Apps
proxy) is used instead, so Unity Catalog permissions apply per person.
"""

from __future__ import annotations

import hashlib
import re
import threading

import pandas as pd

from dq.settings import SETTINGS

_local = threading.local()


def _http_path() -> str:
    return f"/sql/1.0/warehouses/{SETTINGS.warehouse_id}"


def _connect(user_token: str | None):
    from databricks import sql
    from databricks.sdk.core import Config

    cfg = Config()
    if user_token:
        return sql.connect(server_hostname=cfg.host, http_path=_http_path(), access_token=user_token)
    return sql.connect(server_hostname=cfg.host, http_path=_http_path(), credentials_provider=lambda: cfg.authenticate)


def _connection(user_token: str | None):
    """One connection per worker thread and identity, reused across queries."""
    ident = hashlib.sha256(user_token.encode()).hexdigest() if user_token else "app"
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    if ident not in conns:
        conns[ident] = _connect(user_token)
    return ident, conns[ident]


def _drop(ident: str) -> None:
    conn = getattr(_local, "conns", {}).pop(ident, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def query(sql: str, params: dict | None = None, user_token: str | None = None) -> pd.DataFrame:
    """Run one statement and return a DataFrame. Retries once on a dropped connection."""
    for attempt in (1, 2):
        ident, conn = _connection(user_token)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
            return pd.DataFrame.from_records([tuple(r) for r in rows], columns=cols)
        except Exception as exc:
            _drop(ident)
            if attempt == 2 or not _looks_like_connection_error(exc):
                raise
    raise RuntimeError("unreachable")


def _looks_like_connection_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in ("closed", "session", "connection", "timeout", "invalid sessionhandle", "401"))


_ERROR_HINTS = [
    (r"TABLE_OR_VIEW_NOT_FOUND|SCHEMA_NOT_FOUND|does not exist", "A table this check reads was not found. It may have been renamed, or you may not have access to it."),
    (r"PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|User does not have", "You (or the app) do not have permission to read one of the tables this check uses."),
    (r"UNRESOLVED_COLUMN|cannot be resolved", "A column this check expects is missing. The source table's layout may have changed."),
    (r"CAST_INVALID_INPUT|DATETIME_OVERFLOW|cannot be cast", "Some values could not be converted (for example, a date stored as text in an unexpected format)."),
    (r"timed? ?out|TIMEOUT", "The query took too long. Try a shorter date range."),
]


def friendly_error(exc: Exception) -> str:
    text = str(exc)
    for pattern, hint in _ERROR_HINTS:
        if re.search(pattern, text, re.IGNORECASE):
            return f"{hint}\n\nTechnical message: {text[:600]}"
    return f"The check could not run.\n\nTechnical message: {text[:600]}"
