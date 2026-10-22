"""Static validation of every data-quality check, without a Databricks connection.

For each check this:
  1. renders the SQL and parses it as Databricks SQL (sqlglot),
  2. verifies the output-column contract (names and order),
  3. verifies every table and column exists in reference/table_inventory.csv,
  4. compiles the app's summary/detail/trend/breakdown wrappers,
  5. binds the query in DuckDB against empty, correctly typed copies of the
     tables and checks the contract column types (catches string-vs-number
     comparisons that fail at runtime on an ANSI-mode SQL warehouse),
  6. lints the steward-facing text for technical jargon.

Usage (from the ctp_dq_app folder):
    python tools/validate_checks.py                 # all checks
    python tools/validate_checks.py --module esub   # one module
    python tools/validate_checks.py --id ESUB_X     # one check
Exit code is 1 if any ERROR is found. WARNs should be read and resolved or justified.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from importlib import import_module
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
os.environ["DQ_SOURCE"] = "live"  # validate the checks' own SQL, not the saved-results reader
sys.path.insert(0, str(APP_ROOT))

import pandas as pd  # noqa: E402
import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402
from sqlglot.optimizer.qualify import qualify  # noqa: E402
from sqlglot.schema import MappingSchema  # noqa: E402

from dq import engine  # noqa: E402
from dq.model import CONTRACT_COLUMNS, Check  # noqa: E402
from dq.settings import SETTINGS  # noqa: E402

INVENTORY = APP_ROOT / "reference" / "table_inventory.csv"

DUCK_TYPES = {
    "STRING": "VARCHAR", "BOOLEAN": "BOOLEAN", "TIMESTAMP": "TIMESTAMP", "LONG": "BIGINT",
    "DECIMAL": "DECIMAL(38,6)", "INT": "INTEGER", "BINARY": "BLOB", "SHORT": "SMALLINT",
    "DATE": "DATE", "BYTE": "TINYINT",
}
SPARK_TYPES = {"LONG": "BIGINT", "SHORT": "SMALLINT", "BYTE": "TINYINT", "DECIMAL": "DECIMAL(38,6)"}

EXPECTED_DUCK_TYPES = {
    "record_key": {"VARCHAR"},
    "record_date": {"DATE"},
    "submission_type": {"VARCHAR", '"NULL"', "NULL"},
    "office": {"VARCHAR", '"NULL"', "NULL"},
    "is_issue": {"BOOLEAN"},
    "issue_category": {"VARCHAR", '"NULL"', "NULL"},
    "issue_reason": {"VARCHAR", '"NULL"', "NULL"},
}

JARGON = re.compile(
    r"\b(null|nulls|join|joins|joined|table|tables|schema|schemas|column|columns|halo|brnz|slvr|gold_|sql|"
    r"boolean|varchar|bigint|cte|row count|primary key|foreign key|_id|_key|_ind)\b",
    re.IGNORECASE,
)


def load_inventory():
    df = pd.read_csv(INVENTORY)
    tables: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for r in df.itertuples():
        tables[(r.table_schema.lower(), r.table_name.lower())][r.column_name.lower()] = r.data_type
    return tables


def mapping_schema(tables) -> MappingSchema:
    nested: dict = {SETTINGS.catalog: {}}
    for (schema, table), cols in tables.items():
        nested[SETTINGS.catalog].setdefault(schema, {})[table] = {c: SPARK_TYPES.get(t, t) for c, t in cols.items()}
    return MappingSchema(nested, dialect="databricks")


def plain_placeholders() -> dict[str, str]:
    return {k: v.replace("`", "") for k, v in SETTINGS.placeholders().items()}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, cid: str, msg: str) -> None:
        self.errors.append(f"ERROR [{cid}] {msg}")

    def warn(self, cid: str, msg: str) -> None:
        self.warnings.append(f"WARN  [{cid}] {msg}")


def referenced_tables(tree: exp.Expression) -> list[exp.Table]:
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out = []
    for t in tree.find_all(exp.Table):
        if not t.name:
            continue
        if not t.db and t.name.lower() in cte_names:
            continue
        out.append(t)
    return out


TITLE_MAX = 55
TRAILING_PREPOSITIONS = {
    "in", "on", "at", "to", "for", "from", "with", "of", "by", "into", "onto", "about", "up",
    "off", "out", "over", "under", "through", "as", "after", "before", "against", "between",
}
FILLER = re.compile(
    r"\b(seamless(ly)?|leverag\w*|robust|delve|crucial|comprehensive|streamlin\w*|empower\w*|holistic|"
    r"at a glance|in order to|it'?s important to note|worth a closer look|simply|effortless\w*|"
    r"ensure that|utiliz\w*|facilitat\w*|a variety of|plays? a (key|vital|critical) role)\b",
    re.IGNORECASE,
)
_CONJ_START = re.compile(r"^(and|or)\b", re.IGNORECASE)
_CONJ_INSIDE = re.compile(r"\s(and|or)\s", re.IGNORECASE)


def _is_triad(clause: str) -> bool:
    """Heuristic for "A, B and C" / "A, B, or C": a comma run that ends in and/or after exactly 3 items."""
    parts = [p.strip() for p in clause.split(",")]
    run = 1  # items seen in the current comma run
    for p in parts[1:]:
        if _CONJ_START.match(p):
            if run + 1 == 3:
                return True
            run = 1
        elif _CONJ_INSIDE.search(p):
            if run + 2 == 3:
                return True
            run = 1
        else:
            run += 1
    return False


def style_issues(text: str) -> list[str]:
    """Plain-language rules for steward-facing text."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = re.findall(r"[A-Za-z']+", sentence)
        if words and words[-1].lower() in TRAILING_PREPOSITIONS:
            out.append(f"ends with the preposition '{words[-1]}': {sentence[:70]!r}")
        for clause in re.split(r"[;:()]", sentence):
            if _is_triad(clause):
                out.append(f"possible three-item list: {clause.strip()[:80]!r}")
    if "—" in text:
        out.append("uses an em dash; use a period or comma instead")
    hits = sorted({m.group(0).lower() for m in FILLER.finditer(text)})
    if hits:
        out.append(f"filler words {hits}")
    return out


def check_text(check: Check, rep: Report) -> None:
    for field in ("title", "question", "why", "how_to_fix"):
        text = getattr(check, field)
        if not text or len(text.strip()) < 12:
            rep.err(check.id, f"{field} is empty or too short")
            continue
        hits = sorted({m.group(0) for m in JARGON.finditer(text)})
        if hits:
            rep.warn(check.id, f"{field} uses technical words a steward may not know: {hits}")
        for issue in style_issues(text):
            rep.warn(check.id, f"{field} {issue}")
    for i, text in enumerate(check.assumptions):
        for issue in style_issues(text):
            rep.warn(check.id, f"assumption {i + 1} {issue}")
    if len(check.title) > TITLE_MAX:
        rep.warn(check.id, f"title is long ({len(check.title)} chars); keep it to {TITLE_MAX} or fewer")


def validate(check: Check, tables, schema: MappingSchema, duck, rep: Report) -> None:
    check_text(check, rep)
    try:
        sql = check.render_sql(plain_placeholders())
    except KeyError as e:
        rep.err(check.id, str(e))
        return

    try:
        statements = [s for s in sqlglot.parse(sql, read="databricks") if s is not None]
    except Exception as e:
        rep.err(check.id, f"does not parse as Databricks SQL: {e}")
        return
    if len(statements) != 1:
        rep.err(check.id, f"must be exactly one statement, found {len(statements)}")
        return
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        rep.err(check.id, f"must be a SELECT query, found {type(tree).__name__}")
        return

    outer = tree if isinstance(tree, exp.Select) else tree.left
    while isinstance(outer, exp.Union):
        outer = outer.left
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in outer.expressions):
        rep.err(check.id, "outer SELECT must list columns explicitly (no *)")
    names = list(tree.named_selects)
    if tuple(n.lower() for n in names[: len(CONTRACT_COLUMNS)]) != CONTRACT_COLUMNS:
        rep.err(check.id, f"first output columns must be {list(CONTRACT_COLUMNS)}, got {names[:len(CONTRACT_COLUMNS)]}")
    extra = names[len(CONTRACT_COLUMNS):]
    if not extra:
        rep.warn(check.id, "no detail columns after the contract columns; stewards need context to act")
    for n in extra:
        if re.fullmatch(r"[a-z0-9_]+", n) and "_" in n:
            rep.warn(check.id, f"detail column '{n}' looks like a raw field name; give it a friendly alias like `Received date`")
    if len(extra) > 12:
        rep.warn(check.id, f"{len(extra)} detail columns; keep to the ~10 a steward needs")
    if len(set(n.lower() for n in names)) != len(names):
        rep.err(check.id, "duplicate output column names")

    # Tables exist
    ok_tables = True
    for t in referenced_tables(tree):
        key = (t.db.lower(), t.name.lower())
        if t.catalog and t.catalog.lower() != SETTINGS.catalog.lower():
            rep.err(check.id, f"table {t.sql()} is not in catalog {SETTINGS.catalog}; use a ${{...}} placeholder")
            ok_tables = False
        elif key not in tables:
            rep.err(check.id, f"table {t.db}.{t.name} is not in the inventory")
            ok_tables = False

    # Columns exist
    if ok_tables:
        try:
            qualify(tree.copy(), schema=schema, dialect="databricks", validate_qualify_columns=True,
                    quote_identifiers=False)
        except sqlglot.errors.OptimizeError as e:
            rep.err(check.id, f"column check failed: {e}")
        except Exception as e:  # sqlglot limitation, not necessarily a bug
            rep.warn(check.id, f"column check could not complete ({type(e).__name__}: {e}); verify manually")

    # App wrappers compile
    f = engine.Filters(start=pd.Timestamp("2024-01-01").date(), end=pd.Timestamp("2024-12-31").date(),
                       submission_types=("PMTA",), offices=("OS",))
    wrappers = [engine.summary_query(check, f), engine.detail_query(check, f), engine.trend_query(check, f)]
    wrappers += [engine.breakdown_query(check, f, by) for by in engine.BREAKDOWNS]
    for q in wrappers:
        try:
            sqlglot.parse_one(q.sql, read="databricks")
        except Exception as e:
            rep.err(check.id, f"app wrapper query does not parse: {e}")
            break

    if duck is not None and ok_tables:
        bind_in_duckdb(check, sql, duck, rep)


def make_duck(tables):
    try:
        import duckdb
    except ImportError:
        return None
    con = duckdb.connect()
    con.execute(f"ATTACH ':memory:' AS {SETTINGS.catalog}")
    for schema in sorted({s for s, _ in tables}):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {SETTINGS.catalog}.{schema}")
    for (schema, table), cols in tables.items():
        coldefs = ", ".join(f'"{c}" {DUCK_TYPES.get(t, "VARCHAR")}' for c, t in cols.items())
        con.execute(f"CREATE TABLE {SETTINGS.catalog}.{schema}.{table} ({coldefs})")
    return con


def bind_in_duckdb(check: Check, sql: str, con, rep: Report) -> None:
    try:
        duck_sql = sqlglot.transpile(sql, read="databricks", write="duckdb")[0]
    except Exception as e:
        rep.warn(check.id, f"could not translate for the DuckDB bind test ({e})")
        return
    try:
        desc = con.execute(f"DESCRIBE {duck_sql}").fetchall()
    except Exception as e:
        msg = str(e).splitlines()[0][:400]
        rep.warn(check.id, f"DuckDB bind test failed (often a real type mismatch; sometimes a dialect gap): {msg}")
        return
    types = {row[0].lower(): str(row[1]).upper() for row in desc}
    for col, allowed in EXPECTED_DUCK_TYPES.items():
        got = types.get(col)
        if got is not None and got not in allowed:
            rep.err(check.id, f"contract column {col} has type {got}; expected {sorted(allowed)[0]}")


def validate_lookups(tables, schema: MappingSchema, duck, rep: Report) -> int:
    from dq.lookup import TRACE_QUERIES

    for q in TRACE_QUERIES:
        sql = q.render_sql(plain_placeholders()).replace(":stn", "'PM0000001'")
        try:
            tree = sqlglot.parse_one(sql, read="databricks")
        except Exception as e:
            rep.err(q.id, f"does not parse as Databricks SQL: {e}")
            continue
        outer = tree if isinstance(tree, exp.Select) else getattr(tree, "left", tree)
        if isinstance(outer, exp.Select) and any(isinstance(e, exp.Star) for e in outer.expressions):
            rep.err(q.id, "outer SELECT must list columns explicitly (no *)")
        names = list(tree.named_selects)
        for n in names:
            if re.fullmatch(r"[a-z0-9_]+", n) and "_" in n:
                rep.warn(q.id, f"column '{n}' looks like a raw field name; give it a friendly alias")
        for label, alias in q.compare:
            if alias not in names:
                rep.err(q.id, f"compare entry {label!r} points at {alias!r}, which is not an output column")
        ok = True
        for t in referenced_tables(tree):
            if (t.db.lower(), t.name.lower()) not in tables:
                rep.err(q.id, f"table {t.db}.{t.name} is not in the inventory")
                ok = False
        if ok:
            try:
                qualify(tree.copy(), schema=schema, dialect="databricks", validate_qualify_columns=True,
                        quote_identifiers=False)
            except sqlglot.errors.OptimizeError as e:
                rep.err(q.id, f"column check failed: {e}")
            except Exception as e:
                rep.warn(q.id, f"column check could not complete ({type(e).__name__}: {e}); verify manually")
            if duck is not None:
                try:
                    duck.execute("DESCRIBE " + sqlglot.transpile(sql, read="databricks", write="duckdb")[0])
                except Exception as e:
                    rep.warn(q.id, f"DuckDB bind test failed: {str(e).splitlines()[0][:400]}")
    return len(TRACE_QUERIES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", help="module name under dq.checks, e.g. esub")
    ap.add_argument("--id", help="single check id")
    ap.add_argument("--lookup", action="store_true", help="validate the trace-a-submission queries instead")
    ap.add_argument("--no-duckdb", action="store_true")
    args = ap.parse_args()

    if args.lookup:
        tables = load_inventory()
        rep = Report()
        n = validate_lookups(tables, mapping_schema(tables), None if args.no_duckdb else make_duck(tables), rep)
        for line in rep.errors + rep.warnings:
            print(line)
        print(f"\n{n} trace queries validated: {len(rep.errors)} errors, {len(rep.warnings)} warnings")
        return 1 if rep.errors else 0

    if args.module:
        checks = list(import_module(f"dq.checks.{args.module}").CHECKS)
    else:
        from dq.checks import ALL_CHECKS
        checks = list(ALL_CHECKS)
    if args.id:
        checks = [c for c in checks if c.id == args.id]

    ids = [c.id for c in checks]
    dupes = {i for i in ids if ids.count(i) > 1}
    tables = load_inventory()
    schema = mapping_schema(tables)
    duck = None if args.no_duckdb else make_duck(tables)
    rep = Report()
    for d in sorted(dupes):
        rep.err(d, "duplicate check id")
    for c in checks:
        validate(c, tables, schema, duck, rep)

    for line in rep.errors + rep.warnings:
        print(line)
    print(f"\n{len(checks)} checks validated: {len(rep.errors)} errors, {len(rep.warnings)} warnings")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
