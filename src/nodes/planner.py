"""
Runs after clarification and before SQL generation. Decomposes the
(already-clarified) question into an ordered list of independent
sub-questions — each answerable by exactly one SQL query with no
dependency on another step's result.

See ADR-0003 for why cross-step chaining is explicitly out of scope for
this version, and why malformed planner output falls back to a safe
single-step plan instead of retrying or crashing.
"""

import re

import ollama

from src.config import settings
from src.state import AgentState

PLANNER_SYSTEM_PROMPT = """You break a business question into the
smallest set of independent sub-questions needed to answer it, where each
sub-question can be answered by exactly ONE SQL query on its own — with
NO dependency on any other sub-question's result.

Rules:
- If the question is already answerable by a single query, output exactly
  one line.
- Each line must start with "- " and be a complete, self-contained
  question (repeat any shared context — don't say "and the previous
  category" or similar).
- Do NOT decompose into steps where one step needs another step's result
  as input (e.g. "first find the top category, then find its average
  price"). If the question genuinely requires that kind of chaining,
  output it as a single line instead — chaining across steps is not
  supported yet.
- Output nothing except the "- " prefixed lines. No headers, no numbering,
  no explanation.
"""


def _generate_plan_text(question: str, schema_context: str) -> str:
    """Isolated so it can be mocked in tests without a live Ollama call."""
    response = ollama.chat(
        model=settings.ollama_model,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Schema:\n{schema_context}\n\nQuestion: {question}",
            },
        ],
    )
    return response["message"]["content"]


_SQL_LEADING_PATTERN = re.compile(
    r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def _looks_like_sql(line: str) -> bool:
    """
    Detects a step that's actually a SQL statement instead of a
    natural-language question — a real bug found in live testing: the
    model sometimes leaks the query itself into the plan output instead
    of describing what it answers. Treated the same as any other
    malformed planner output (ADR-0003, Decision 3) — degrade to a safe
    single-step plan rather than trusting it.
    """
    return bool(_SQL_LEADING_PATTERN.match(line))


def _parse_plan(raw_text: str, fallback_question: str) -> list:
    steps = [
        line.strip()[2:].strip()
        for line in raw_text.splitlines()
        if line.strip().startswith("- ")
    ]
    if not steps or any(_looks_like_sql(step) for step in steps):
        # Malformed planner output — either no valid lines, or a step
        # that's actually SQL rather than a question — degrade
        # gracefully to a safe single-step plan rather than crashing or
        # trusting output that would make the "Step N" display in the
        # final answer show a query instead of a question (ADR-0003,
        # Decision 3).
        return [fallback_question]
    return steps


def create_plan(state: AgentState) -> AgentState:
    raw_text = _generate_plan_text(state["question"], state["schema_context"])
    plan = _parse_plan(raw_text, fallback_question=state["question"])

    return {
        **state,
        "plan": plan,
        "current_step_index": 0,
        "step_results": [],
        "plan_complete": False,
    }
