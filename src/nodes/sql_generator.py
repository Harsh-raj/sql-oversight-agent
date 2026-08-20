import re

import ollama

from src.config import settings
from src.memory.correction_store import CorrectionStore
from src.schema_utils import extract_table_names
from src.state import AgentState

# Alias kept so existing tests / callers referencing the old private name
# keep working — the implementation now lives in schema_utils, shared
# with identifier_validator (ADR-0005).
_extract_table_names = extract_table_names

SYSTEM_PROMPT = """You are a SQL generation assistant for a SQLite database.
Given a schema and a business question, write ONE valid SQLite query that
answers it. Rules:
- Use ONLY the tables and columns given in the schema. NEVER invent a
  table name or a column name — not even one that sounds plausible from
  the wording of the question itself. If the question's phrasing suggests
  a name that isn't in the schema (e.g. the question says "courses" but
  the schema's table is named something else), use the ACTUAL schema name
  instead of the question's wording.
- Return ONLY the SQL query, no explanation, no markdown fences.
- Prefer explicit column lists over SELECT *.
- Any ambiguity in the question that would change what gets computed has
  already been resolved upstream (a clarification step runs before you —
  if the question includes a "Clarification asked" / "Developer's answer"
  section, treat that answer as authoritative). Do not introduce new
  assumptions about what the question means; translate it as given.
"""


def _extract_sql(raw_text: str) -> str:
    """Strip markdown fences if the model adds them despite instructions."""
    match = re.search(r"```(?:sql)?\s*(.*?)```", raw_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw_text.strip()


def _detect_bad_identifier(error: str):
    """Returns (error_type, bad_identifier) for a table/column-not-found
    error, or (None, None) if the error doesn't match either pattern.
    Shared by _build_retry_feedback (the prompt text) and generate_sql
    (which needs to know a correction is being attempted, so it can be
    flagged for developer confirmation — ADR-0004)."""
    error_lower = (error or "").lower()

    if "no such table" in error_lower:
        match = re.search(r"no such table:\s*(\S+)", error, re.IGNORECASE)
        bad_table = match.group(1).strip(
            ";") if match else "the table you used"
        return "table", bad_table

    if "no such column" in error_lower:
        match = re.search(r"no such column:\s*(\S+)", error, re.IGNORECASE)
        bad_col = match.group(1).strip(";") if match else "the column you used"
        return "column", bad_col

    return None, None


def _build_retry_feedback(error: str, previous_query: str) -> str:
    """
    Builds the retry correction message. Detects specific error patterns
    ("no such table" / "no such column") and gives a targeted correction
    naming the exact bad identifier, rather than a generic "it failed, try
    again" — generic feedback was observed (in live testing) to sometimes
    produce a near-identical repeat of the same wrong query.
    """
    error_type, bad_identifier = _detect_bad_identifier(error)

    if error_type == "table":
        return f"""
Your previous query used the table '{bad_identifier}', which does NOT exist in this database.

Previous query:
{previous_query}

Look again at the "Valid table names" line above and rewrite the query
using the correct table name. Do not reuse '{bad_identifier}' — it is
wrong regardless of how well it seemed to match the question's wording.
"""

    if error_type == "column":
        return f"""
Your previous query used the column '{bad_identifier}', which does NOT
exist in this schema.

Previous query:
{previous_query}

Check the exact column names in the schema above and rewrite the query
using a real column name. Do not reuse '{bad_identifier}'.
"""

    return f"""
Your previous query failed with this error:
{error}

Previous query:
{previous_query}

Write a corrected query.
"""


def _format_memory_context(similar_corrections: list) -> str:
    """
    Formats retrieved past corrections as few-shot context. Only
    corrections above the configured similarity threshold are included —
    a low-similarity "match" is worse than no match, since it primes the
    model with an irrelevant example (ADR-0008, Decision 4/consequence).
    """
    relevant = [
        c
        for c in similar_corrections
        if c["score"] >= settings.retrieval_similarity_threshold
    ]
    if not relevant:
        return ""

    lines = [
        "\nSimilar past question(s) where a human corrected the agent's "
        "answer — use these as guidance if relevant:"
    ]
    for c in relevant:
        lines.append(f'- Question: "{c["question"]}"')
        if c.get("attempted_sql"):
            lines.append(f"  Agent's incorrect attempt: {c['attempted_sql']}")
        lines.append(f"  Correct approach: {c['human_correction']}")
    return "\n".join(lines) + "\n"


def generate_sql(state: AgentState) -> AgentState:
    """
    Generates a SQL query for the CURRENT plan step (ADR-0003) — not
    necessarily the whole original question, which may have been broken
    into multiple independent sub-questions by the Planner. The overall
    question is still included as context.

    On retry (generation_attempt > 0):
    - the prompt includes a targeted correction (see _build_retry_feedback)
      instead of generic error text
    - the sampling temperature is increased, so the model has an actual
      chance to produce something different rather than confidently
      repeating the same wrong query (observed in live testing: a retry
      with temperature 0 regenerated a near-identical bad query).
    """
    attempt = state.get("generation_attempt", 0)
    current_step = state["plan"][state["current_step_index"]]
    schema_context = state["schema_context"]

    table_names = _extract_table_names(schema_context)
    valid_tables_line = (
        f"Valid table names (use EXACTLY these — no others): {', '.join(table_names)}\n\n"
        if table_names
        else ""
    )

    user_prompt = f"""{valid_tables_line}Schema:
{schema_context}

Overall question: {state['question']}
Current sub-question to answer with one SQL query: {current_step}
"""

    # Only query memory on the first attempt per step — the question
    # doesn't change between retries, only the error feedback does
    # (ADR-0008, Decision 4).
    if attempt == 0:
        similar = CorrectionStore().retrieve_similar(current_step)
        user_prompt += _format_memory_context(similar)

    is_identifier_correction = False
    if attempt > 0 and state.get("sql_error"):
        error_type, bad_identifier = _detect_bad_identifier(state["sql_error"])
        user_prompt += _build_retry_feedback(
            state["sql_error"], state.get("sql_query", "")
        )
        if error_type is not None:
            is_identifier_correction = True

    # Base temperature kept low for consistency; bumped on each retry so a
    # confidently-wrong first attempt doesn't just get regenerated as-is.
    temperature = min(0.2 + attempt * 0.3, 0.8)

    response = ollama.chat(
        model=settings.ollama_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        options={"temperature": temperature},
    )

    sql = _extract_sql(response["message"]["content"])

    result_state = {
        **state,
        "sql_query": sql,
        "generation_attempt": attempt + 1,
        "sql_error": None,
    }

    if is_identifier_correction:
        error_type, bad_identifier = _detect_bad_identifier(state["sql_error"])
        result_state["pending_correction"] = {
            "error_type": error_type,
            "original_identifier": bad_identifier,
            "original_query": state.get("sql_query"),
            "step_question": current_step,
        }

    return result_state
