"""Package the app into ONE Databricks notebook that recreates the app folder in a workspace.

For workspaces where Git folders are disabled and the CLI is not available: import
dist/ctp_dq_installer.py (Workspace > Import > File), open it, and Run all. It writes
every app file to /Workspace/Users/<you>/ctp_dq_app (or a folder you choose).

Usage (from ctp_dq_app):  python tools/build_installer.py
Rebuild after any code change so the installer matches the code.
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import textwrap
import zipfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
OUT = APP_ROOT / "dist" / "ctp_dq_installer.py"

# What the workspace needs to run the app and the refresh job, plus the docs and SQL scripts.
# Local-only development tools (validator, smoke tests) and the 850 KB schema inventory they use
# are left out to keep the notebook small.
INCLUDE = [
    "app.py", "app_v2.py", "app.yaml", "databricks.yml", "requirements.txt", "README.md",
    ".streamlit/config.toml",
    "dq/**/*.py", "dq/checks/AUTHORING.md",
    "ui/*.py", "views/*.py", "v2/views/*.py",
    "jobs/*.py",
    "reference/confirmed_facts.md",
    "tools/sql/*.sql",
]


def collect() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for pattern in INCLUDE:
        for p in sorted(APP_ROOT.glob(pattern)):
            if p.is_file() and "__pycache__" not in p.parts:
                # Normalize line endings so the workspace copy is identical on every OS.
                files[p.relative_to(APP_ROOT).as_posix()] = p.read_bytes().replace(b"\r\n", b"\n")
    return files


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
    nb = f'''# Databricks notebook source
# MAGIC %md
# MAGIC # CTP Data Quality: installer
# MAGIC
# MAGIC This notebook writes the CTP Data Quality app ({len(files)} files, build `{digest}`) into a workspace folder.
# MAGIC
# MAGIC 1. Attach it to **Serverless** compute (or any cluster on Databricks Runtime 13.3 LTS or later).
# MAGIC 2. Optional: change the **target_folder** box at the top. The default is your own user folder.
# MAGIC 3. Click **Run all**. The last cell lists every file written.
# MAGIC
# MAGIC Running it again overwrites the app files with this version. Other files in the folder are left alone.

# COMMAND ----------

me = spark.sql("SELECT current_user()").first()[0]
dbutils.widgets.text("target_folder", f"/Workspace/Users/{{me}}/ctp_dq_app", "Target folder")
TARGET = dbutils.widgets.get("target_folder").rstrip("/")
assert TARGET.startswith("/Workspace/"), "The target folder must start with /Workspace/"
print("Installing into", TARGET)

# COMMAND ----------

# The app files as one compressed archive ({len(blob) // 1024} KB, build {digest}).
ARCHIVE = """
{b64}
"""

# COMMAND ----------

import base64, hashlib, io, os, zipfile

blob = base64.b64decode("".join(ARCHIVE.split()))
assert hashlib.sha256(blob).hexdigest()[:12] == "{digest}", "The archive is damaged. Import the installer again."
written = []
with zipfile.ZipFile(io.BytesIO(blob)) as zf:
    for rel in zf.namelist():
        path = os.path.join(TARGET, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(zf.read(rel))
        written.append(rel)

print(f"Wrote {{len(written)}} files to {{TARGET}} (build {digest}):")
for rel in sorted(written):
    print("  ", rel)
print()
print("Next: create the refresh job from", TARGET + "/jobs/refresh_snapshot.py", "and deploy the app from", TARGET)
'''
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(nb, encoding="utf-8", newline="\n")
    return OUT


if __name__ == "__main__":
    out = build()
    print(f"{out.relative_to(APP_ROOT).as_posix()}: {out.stat().st_size / 1024:.0f} KB")
    sys.exit(0)
