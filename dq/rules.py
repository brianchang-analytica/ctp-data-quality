"""Rule wording and limits, read from a file in the Databricks workspace.

Each rule is one check. The SQL behind a rule stays in the app (dq/checks), so nobody can change
what runs by editing a file. The rule file holds what stewards read and tune:

    title, what_it_checks, why_it_matters, how_to_fix, impact,
    needs_attention_below_pct, action_needed_below_pct, assumptions

The other fields in the file (id, systems, problem_type, counts, date_used, shown_in_app) are
there for reference; the app ignores changes to them.

Set DQ_RULES_PATH to the file (for example /Workspace/Shared/ctp_dq/ctp_dq_rules.json) and give
the app's service principal "Can read" on it. The app reads it through the Workspace API, so edits
show up without redeploying. A local path also works, which is handy for testing. Without
DQ_RULES_PATH, or when the file cannot be read, the app uses the wording built into the code.

Get a starting file from the app's Rule list page, or run:  python tools/export_rules.py
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from dq.checks.core import KIND_WORDS
from dq.model import FILTER_DATE, SEVERITIES, SYSTEMS, Check

FORMAT = "ctp-dq-rules/1"
FILE_NAME = "ctp_dq_rules.json"

ABOUT = (
    "Wording and limits for the CTP Data Quality app. Each rule is one check. You can change "
    "title, what_it_checks, why_it_matters, how_to_fix, impact (High, Medium or Low), "
    "needs_attention_below_pct, action_needed_below_pct and assumptions. The app ignores changes "
    "to the other fields. The logic of each check stays in the app."
)

# File key -> Check field, for the fields the file may change.
_TEXT = {"title": "title", "what_it_checks": "question", "why_it_matters": "why", "how_to_fix": "how_to_fix"}
_TEXT_MAX = {"title": 90}
_TEXT_DEFAULT_MAX = 2000

# A slow or unreachable workspace must never hold up a page, so a read that takes longer gives up.
READ_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class RuleSource:
    """Where the rule wording came from, plus the changes the file makes to each check."""

    path: str = ""  # empty = built into the app
    read_ok: bool = False
    changed_at: datetime | None = None
    read_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    changes: dict[str, dict] = field(default_factory=dict)  # check id -> {Check field: value}
    problems: tuple[str, ...] = ()

    @property
    def from_file(self) -> bool:
        return bool(self.path) and self.read_ok


# --------------------------------------------------------------------------- #
# Export: the built-in rules as a file
# --------------------------------------------------------------------------- #


def as_pct(rate: float) -> float | int:
    v = round(rate * 100, 2)
    return int(v) if v == int(v) else v


def rule_entry(check: Check, shown_in_app: bool = True) -> dict:
    return {
        "id": check.id,
        "title": check.title,
        "systems": [SYSTEMS[s].name for s in check.systems],
        "problem_type": KIND_WORDS[check.dimension],
        "counts": check.record_label,
        "date_used": check.date_label if FILTER_DATE in check.filters else "Latest data",
        "shown_in_app": shown_in_app,
        "impact": check.severity,
        "what_it_checks": check.question,
        "why_it_matters": check.why,
        "how_to_fix": check.how_to_fix,
        "needs_attention_below_pct": as_pct(check.warn_below),
        "action_needed_below_pct": as_pct(check.fail_below),
        "assumptions": list(check.assumptions),
    }


def to_document(checks: list[Check], shown_ids: set[str] | None = None) -> dict:
    return {
        "format": FORMAT,
        "about": ABOUT,
        "rules": [rule_entry(c, shown_ids is None or c.id in shown_ids) for c in checks],
    }


def to_json(checks: list[Check], shown_ids: set[str] | None = None) -> str:
    return json.dumps(to_document(checks, shown_ids), indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Import: read and check the file
# --------------------------------------------------------------------------- #


def _read(path: str) -> tuple[str, datetime | None]:
    local = Path(path)
    if local.is_file():
        return local.read_text(encoding="utf-8"), datetime.fromtimestamp(local.stat().st_mtime, timezone.utc)
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()  # the app's service principal, from the variables Databricks Apps sets
    info = w.workspace.get_status(path)
    text = w.workspace.download(path).read().decode("utf-8-sig")
    changed = datetime.fromtimestamp(info.modified_at / 1000, timezone.utc) if info.modified_at else None
    return text, changed


def _read_with_timeout(path: str) -> tuple[str, datetime | None]:
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = _read(path)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, name="dq-rule-file", daemon=True)
    worker.start()
    worker.join(READ_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise TimeoutError(f"no answer from the workspace within {READ_TIMEOUT_SECONDS} seconds")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _changes_for(entry: dict, check: Check) -> tuple[dict, list[str]]:
    """The valid changes one rule makes, and a note for each value that was refused."""
    rid, out, notes = check.id, {}, []
    for key, attr in _TEXT.items():
        if key not in entry:
            continue
        value = entry[key]
        limit = _TEXT_MAX.get(key, _TEXT_DEFAULT_MAX)
        if not isinstance(value, str) or not value.strip():
            notes.append(f"{rid}: {key} must be text. Kept the built-in wording.")
        elif len(value) > limit:
            notes.append(f"{rid}: {key} is longer than {limit} characters. Kept the built-in wording.")
        else:
            out[attr] = " ".join(value.split())
    if "impact" in entry:
        if entry["impact"] in SEVERITIES:
            out["severity"] = entry["impact"]
        else:
            notes.append(f"{rid}: impact must be High, Medium or Low. Kept {check.severity}.")
    limits = {}
    for key, attr in (("needs_attention_below_pct", "warn_below"), ("action_needed_below_pct", "fail_below")):
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 100:
            limits[attr] = round(value / 100, 6)
        else:
            notes.append(f"{rid}: {key} must be a number from 1 to 100. Kept the built-in limit.")
    if limits:
        warn, fail = limits.get("warn_below", check.warn_below), limits.get("fail_below", check.fail_below)
        if fail <= warn:
            out.update(limits)
        else:
            notes.append(f"{rid}: action_needed_below_pct is higher than needs_attention_below_pct. Kept the built-in limits.")
    if "assumptions" in entry:
        value = entry["assumptions"]
        if isinstance(value, list) and all(isinstance(a, str) for a in value):
            out["assumptions"] = tuple(" ".join(a.split()) for a in value if a.strip())
        else:
            notes.append(f"{rid}: assumptions must be a list of text. Kept the built-in list.")
    return {k: v for k, v in out.items() if getattr(check, k) != v}, notes


def parse(text: str, checks_by_id: dict[str, Check]) -> tuple[dict[str, dict], list[str]]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [f"The rule file is not valid JSON (line {exc.lineno}, column {exc.colno}). Using the built-in rules."]
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list):
        return {}, ['The rule file has no "rules" list. Using the built-in rules.']
    changes, notes = {}, []
    if doc.get("format") != FORMAT:
        notes.append(f'The rule file should start with "format": "{FORMAT}". Read it anyway.')
    for entry in rules:
        rid = entry.get("id") if isinstance(entry, dict) else None
        if rid not in checks_by_id:
            notes.append(f"Rule {rid or '(no id)'} is not a check in this app. Skipped it.")
            continue
        found, refused = _changes_for(entry, checks_by_id[rid])
        notes.extend(refused)
        if found:
            changes[rid] = found
    return changes, notes


def load(path: str) -> RuleSource:
    """Read the rule file at `path`. Never raises: problems are returned for the Rule list page to show."""
    if not path:
        return RuleSource()
    from dq.checks import CHECKS_BY_ID

    try:
        text, changed = _read_with_timeout(path)
    except Exception as exc:  # missing file, no permission, no network: fall back to the built-in rules
        return RuleSource(path=path, problems=(f"Could not read the rule file. Using the built-in rules. ({str(exc)[:300]})",))
    changes, notes = parse(text, CHECKS_BY_ID)
    return RuleSource(path=path, read_ok=True, changed_at=changed, changes=changes, problems=tuple(notes))


def apply(check: Check, source: RuleSource) -> Check:
    found = source.changes.get(check.id)
    return replace(check, **found) if found else check
