"""Package the app as THREE files for workspaces with no Git, no CLI and no Python compute.

Writes dist/app_bundle/:
    app.py            a small launcher with the whole app packed inside it
    app.yaml          Databricks Apps settings (live mode: no refresh job needed)
    requirements.txt  Python packages

Upload the three files into one workspace folder (Import > File) and deploy that folder as a
Databricks App. On start, app.py unpacks the code into a temporary folder on the app's own
compute and runs it, so no cluster or serverless permission is needed.

Usage (from ctp_dq_app):  python tools/build_app_bundle.py
Rebuild after any code change so the bundle matches the code.
"""

from __future__ import annotations

import base64
import hashlib
import io
import shutil
import sys
import textwrap
import zipfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = APP_ROOT / "dist" / "app_bundle"

# Runtime code only. The real entry point is packed as app_main.py; app.py is the launcher.
INCLUDE = ["dq/**/*.py", "ui/*.py", "views/*.py"]

APP_YAML = """\
# Databricks Apps settings for the single-file bundle (no refresh job, no Python compute needed).
# Add a "SQL warehouse" resource to the app with resource key "sql-warehouse" and "Can use".
command:
  - streamlit
  - run
  - app.py
  - --server.fileWatcherType=none
  - --browser.gatherUsageStats=false
  - --client.toolbarMode=minimal
  - --theme.base=light
  - --theme.primaryColor=#2a78d6
  - --theme.backgroundColor=#f4f6fb
  - --theme.secondaryBackgroundColor=#eaeef6
  - --theme.textColor=#15161a

env:
  - name: DATABRICKS_WAREHOUSE_ID
    valueFrom: sql-warehouse
  - name: DQ_CATALOG
    value: ctp_pd_catalog
  # live = run the checks against the source tables when a steward opens the app.
  # Switch to snapshot once a nightly refresh job (jobs/refresh_snapshot.py) can run.
  - name: DQ_SOURCE
    value: live
  # Results are reused for 4 hours, so only the first visit after that waits for the checks.
  - name: DQ_CACHE_MINUTES
    value: "240"
  - name: DQ_AUTH_MODE
    value: app
"""

LAUNCHER = '''"""CTP Data Quality: single-file launcher for Databricks Apps (build {digest}).

The whole app ({n} files) is packed below. On start this file unpacks it into a temporary
folder on the app's own compute and runs it. Built by tools/build_app_bundle.py; do not edit
by hand, rebuild instead.
"""

import base64
import hashlib
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_BUILD = "{digest}"


def _unpack() -> Path:
    """Unpack once per build. A finished folder is renamed into place, so a half-written copy is never used."""
    target = Path(tempfile.gettempdir()) / f"ctp_dq_app_{{_BUILD}}"
    if (target / "app_main.py").is_file():
        return target
    blob = base64.b64decode("".join(_ARCHIVE.split()))
    if hashlib.sha256(blob).hexdigest()[:12] != _BUILD:
        raise RuntimeError("The packed app is damaged. Upload app.py again.")
    staging = target.with_name(f"{{target.name}}_{{os.getpid()}}_{{time.time_ns()}}")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(staging)
    try:
        os.rename(staging, target)
    except OSError:  # another session unpacked it first; use theirs
        shutil.rmtree(staging, ignore_errors=True)
    return target


_ARCHIVE = """
{archive}
"""

_ROOT = _unpack()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_MAIN = _ROOT / "app_main.py"
exec(compile(_MAIN.read_text(encoding="utf-8"), str(_MAIN), "exec"), {{"__name__": "__main__", "__file__": str(_MAIN)}})
'''


def collect() -> dict[str, bytes]:
    files = {"app_main.py": (APP_ROOT / "app.py").read_bytes()}
    for pattern in INCLUDE:
        for p in sorted(APP_ROOT.glob(pattern)):
            if p.is_file() and "__pycache__" not in p.parts:
                files[p.relative_to(APP_ROOT).as_posix()] = p.read_bytes()
    return {k: v.replace(b"\r\n", b"\n") for k, v in files.items()}


def archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, data in sorted(files.items()):
            info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))  # fixed time: same input, same file
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def build() -> Path:
    files = collect()
    blob = archive(files)
    digest = hashlib.sha256(blob).hexdigest()[:12]
    b64 = "\n".join(textwrap.wrap(base64.b64encode(blob).decode(), 100))
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    (OUT_DIR / "app.py").write_text(LAUNCHER.format(digest=digest, n=len(files), archive=b64),
                                    encoding="utf-8", newline="\n")
    (OUT_DIR / "app.yaml").write_text(APP_YAML, encoding="utf-8", newline="\n")
    shutil.copyfile(APP_ROOT / "requirements.txt", OUT_DIR / "requirements.txt")
    return OUT_DIR


if __name__ == "__main__":
    out = build()
    for f in sorted(out.iterdir()):
        print(f"{f.relative_to(APP_ROOT).as_posix()}: {f.stat().st_size / 1024:.0f} KB")
    sys.exit(0)
