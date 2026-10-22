"""Shared SQL macros so every check normalizes keys, names and flags identically.

Write a macro call in any check or trace query SQL, e.g. `STN_KEY(s.stn)`; it is
expanded to plain Databricks SQL when the query is rendered (Check.render_sql /
TraceQuery.render_sql), so what runs on the warehouse — and what "technical
details" shows — never contains macros.

    STN_KEY(x)  STN upper-cased with all whitespace (incl. non-breaking) removed; blank -> NULL
    TXT(x)      text upper-cased, trimmed, inner whitespace collapsed; blank -> NULL
    CO_NAME(x)  company name for matching: & as AND, R.J. as RJ, punctuation and INC/LLC/LTD/CO/CORP removed
    BLANK(x)    trimmed text, original case (for display); blank -> NULL
    NUM_KEY(x)  numeric ID (INT/BIGINT/DECIMAL) as a string, so 123.000000 matches 123
    ANY_KEY(x)  ID of unknown type: numeric form when it parses as a number, else upper/trim
    FLAG_ON(x)  TRUE when a Y/N-style flag is set (Y, YES, 1, T, TRUE in any case); never NULL

Arguments may contain nested parentheses and quoted strings. Macro names are
upper-case and must be followed directly by "(".
"""

from __future__ import annotations

import re

MACROS: dict[str, str] = {
    "STN_KEY": r"nullif(upper(regexp_replace(CAST({x} AS STRING), '[\\s\\p{Z}]+', '')), '')",
    "TXT": r"nullif(upper(regexp_replace(trim(CAST({x} AS STRING)), '\\s+', ' ')), '')",
    # & -> AND; periods dropped without a space (R.J. -> RJ); other punctuation -> space;
    # legal suffixes and a leading THE removed; spaces collapsed.
    "CO_NAME": (
        r"nullif(trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace("
        r"upper(CAST({x} AS STRING)), '&', ' AND '), '[.]', ''), "
        r"'[^A-Z0-9 ]', ' '), "
        r"'\\b(THE|INCORPORATED|INC|LLC|L L C|LIMITED|LTD|CORPORATION|CORP|COMPANY|CO|LP|LLP|PLC)\\b', ' '), "
        r"'\\s+', ' ')), '')"
    ),
    "BLANK": r"nullif(trim(CAST({x} AS STRING)), '')",
    "NUM_KEY": r"CAST(try_cast({x} AS BIGINT) AS STRING)",
    "ANY_KEY": (
        r"coalesce(CAST(try_cast(try_cast(trim(CAST({x} AS STRING)) AS DECIMAL(38,6)) AS BIGINT) AS STRING), "
        r"nullif(upper(trim(CAST({x} AS STRING))), ''))"
    ),
    "FLAG_ON": r"coalesce(upper(trim(CAST({x} AS STRING))) IN ('Y', 'YES', '1', 'T', 'TRUE'), false)",
}

_CALL = re.compile(r"\b(" + "|".join(sorted(MACROS, key=len, reverse=True)) + r")\(")


def expand(sql: str) -> str:
    """Replace every MACRO(arg) call with its SQL, innermost-safe and quote-aware."""
    while True:
        m = _CALL.search(sql)
        if not m:
            return sql
        i, depth, in_str = m.end(), 1, False
        while depth:
            if i >= len(sql):
                raise ValueError(f"Unbalanced parentheses after {m.group(1)}(")
            ch = sql[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str and ch == "(":
                depth += 1
            elif not in_str and ch == ")":
                depth -= 1
            i += 1
        arg = sql[m.end(): i - 1].strip()
        sql = sql[: m.start()] + "(" + MACROS[m.group(1)].replace("{x}", arg) + ")" + sql[i:]
