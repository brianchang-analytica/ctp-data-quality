"""Write the rule file: every check's wording and limits as JSON, ready to import into the workspace.

Writes dist/ctp_dq_rules.json. Import it into a workspace folder, share it with the app's service
principal (Can read) and set DQ_RULES_PATH in app.yaml. See dq/rules.py for what the file controls.
The app's Rule list page offers the same file as a download.

Usage (from ctp_dq_app):  python tools/export_rules.py [output path]
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from dq import rules  # noqa: E402
from dq.checks import ALL_CHECKS  # noqa: E402
from dq.checks.core import active_checks  # noqa: E402

if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else APP_ROOT / "dist" / rules.FILE_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    shown = {c.id for c in active_checks()}
    out.write_text(rules.to_json(ALL_CHECKS, shown), encoding="utf-8", newline="\n")
    print(f"{out}: {len(ALL_CHECKS)} rules, {len(shown)} shown in the app")
