"""
Runs between sql_generator and sql_executor, on every attempt. Parses the
generated SQL (via sqlparse) to find referenced table and column names,
checks them against the real schema, and substitutes a confident
fuzzy-matched correction if one exists — flagged through the same
pending_correction / confirm_correction gate already built for
LLM-retry-based corrections (ADR-0004).

This does not replace that retry-based path — it runs first, and the
retry path remains the fallback for anything this validator can't
confidently resolve (no close match, or an ambiguous one). See ADR-0005
for the full reasoning, including why this is a heuristic parser rather
than a full SQL semantic analysis.
"""

import difflib
import re

import sqlparse
from sqlparse.tokens import Keyword, Name, Punctuation

from src.schema_utils import extract_all_column_names, extract_table_names
from src.state import AgentState

FUZZY_MATCH_THRESHOLD = 0.6  # minimum similarity to consider a candidate at all
AMBIGUITY_GAP = 0.1  # minimum gap over the second-best candidate to trust the top one


def _ci_contains(name: str, candidates: list) -> bool:
    """Case-insensitive membership check — SQLite identifiers are
    case-insensitive, so 'Category' matching schema column 'category' is
    not an error and must not be treated as one (a real bug found during
    testing: without this, harmless casing differences triggered
    unnecessary confirmation prompts)."""
    lowered = {c.lower() for c in candidates}
    return name.lower() in lowered


def _extract_referenced_tables(tokens: list) -> list:
    """Table names immediately following FROM/JOIN keywords."""
    tables = []
    for i, token in enumerate(tokens):
        if token.ttype is Keyword and token.value.upper() in ("FROM", "JOIN"):
            j = i + 1
            while j < len(tokens) and tokens[j].is_whitespace:
                j += 1
            if j < len(tokens) and tokens[j].ttype is Name:
                tables.append(tokens[j].value)
    return tables


def _extract_referenced_columns(tokens: list, known_tables: list) -> list:
    """
    Name tokens that aren't table references (i.e. not immediately after
    FROM/JOIN), aren't a known table name themselves, and aren't function
    calls (a Name immediately followed by '(' — sqlparse tokenizes
    function names like COUNT as plain Name tokens, indistinguishable
    from a column reference by type alone; verified directly against
    sqlparse's actual token output before relying on this). Heuristic,
    not a full parse — see ADR-0005, Decision 4 for the documented
    limitation on aliases/joins/qualified references.
    """
    columns = []
    for i, token in enumerate(tokens):
        if token.is_whitespace or token.ttype is Punctuation:
            continue
        if token.ttype is Name:
            j = i - 1
            while j >= 0 and tokens[j].is_whitespace:
                j -= 1
            if (
                j >= 0
                and tokens[j].ttype is Keyword
                and tokens[j].value.upper() in ("FROM", "JOIN")
            ):
                continue  # this is a table reference, not a column
            if _ci_contains(token.value, known_tables):
                continue  # this is a table reference, not a column (case-insensitive)

            k = i + 1
            while k < len(tokens) and tokens[k].is_whitespace:
                k += 1
            if k < len(tokens) and tokens[k].ttype is Punctuation and tokens[k].value == "(":
                continue  # this is a function call (e.g. COUNT), not a column

            columns.append(token.value)
    return columns


def _find_best_match(identifier: str, valid_candidates: list):
    """
    Returns the best matching candidate name, or None if no candidate is
    both similar enough (FUZZY_MATCH_THRESHOLD) and clearly better than
    the next-best option (AMBIGUITY_GAP) — see ADR-0005, Decision 3.
    """
    if not valid_candidates:
        return None

    scored = sorted(
        (
            (c, difflib.SequenceMatcher(None, identifier.lower(), c.lower()).ratio())
            for c in valid_candidates
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )

    best_name, best_score = scored[0]
    if best_score < FUZZY_MATCH_THRESHOLD:
        return None

    if len(scored) > 1:
        _, second_score = scored[1]
        if best_score - second_score < AMBIGUITY_GAP:
            return None  # too ambiguous to guess confidently

    return best_name


def validate_identifiers(state: AgentState) -> AgentState:
    sql = state.get("sql_query") or ""
    schema_context = state["schema_context"]

    valid_tables = extract_table_names(schema_context)
    valid_columns = extract_all_column_names(schema_context)

    parsed = sqlparse.parse(sql)
    if not parsed:
        return state
    tokens = list(parsed[0].flatten())

    current_step = state["plan"][state["current_step_index"]]
    corrected_sql = sql
    correction = None

    # Tables first — a wrong table makes column-checking meaningless
    for table in _extract_referenced_tables(tokens):
        if _ci_contains(table, valid_tables):
            continue
        match = _find_best_match(table, valid_tables)
        if match:
            corrected_sql = re.sub(rf"\b{re.escape(table)}\b", match, corrected_sql)
            correction = {
                "error_type": "table",
                "original_identifier": table,
                "original_query": sql,
                "step_question": current_step,
            }
            break  # one correction at a time (same simplification as ADR-0004)

    if correction is None:
        for column in _extract_referenced_columns(tokens, valid_tables):
            if column == "*" or _ci_contains(column, valid_columns):
                continue
            match = _find_best_match(column, valid_columns)
            if match:
                corrected_sql = re.sub(
                    rf"\b{re.escape(column)}\b", match, corrected_sql
                )
                correction = {
                    "error_type": "column",
                    "original_identifier": column,
                    "original_query": sql,
                    "step_question": current_step,
                }
                break

    if correction is None:
        return state  # nothing wrong, or nothing confidently fixable

    return {
        **state,
        "sql_query": corrected_sql,
        "pending_correction": correction,
    }
