"""
Runs after schema grounding and before SQL generation. Checks whether the
question has ambiguity that would actually change what gets computed
(undefined metric, missing time range, unclear comparison baseline) and,
if so, pauses and asks the developer directly — rather than letting the
SQL generator silently pick an interpretation.

This supersedes ADR-0001's original "assume and proceed" instruction —
see ADR-0002, Decision 4, for why.
"""

import ollama

from src.config import settings
from src.state import AgentState

CLARIFICATION_SYSTEM_PROMPT = """You check whether a business question is
precise enough to translate directly into one correct SQL query against
the given schema.

Respond with EXACTLY one line, nothing else:
- "CLEAR" if the question is unambiguous enough to write one correct query.
- "CLARIFY: <your one clarifying question>" if there is a genuine ambiguity
  that would change *what* is computed — e.g. an undefined metric
  ("popular" could mean enrollment count, revenue, or recency), a missing
  time range ("recently" — how recent?), or an unclear comparison baseline
  ("higher than usual" — usual compared to what?).

Do NOT flag stylistic ambiguity that has an obvious reasonable default
(sort order, rounding, column order). Only flag ambiguity that would
change the actual result if interpreted differently.
"""


def _check_ambiguity(question: str, schema_context: str) -> str:
    """Isolated so it can be mocked in tests without a live Ollama call."""
    response = ollama.chat(
        model=settings.ollama_model,
        messages=[
            {"role": "system", "content": CLARIFICATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Schema:\n{schema_context}\n\nQuestion: {question}",
            },
        ],
    )
    return response["message"]["content"].strip()


def check_clarity(state: AgentState) -> AgentState:
    if not settings.clarification_enabled:
        return {**state, "clarification_needed": False}

    verdict = _check_ambiguity(state["question"], state["schema_context"])

    if not verdict.upper().startswith("CLARIFY"):
        return {**state, "clarification_needed": False}

    clarifying_question = (
        verdict.split(":", 1)[1].strip()
        if ":" in verdict
        else "Can you clarify what you mean?"
    )

    print(f"\n[CLARIFICATION NEEDED] {clarifying_question}")
    answer = input(
        "Your answer (or press Enter to let the agent proceed with its "
        "own best-effort interpretation): "
    ).strip()

    if answer:
        augmented_question = (
            f"{state['question']}\n\n"
            f"Clarification asked: {clarifying_question}\n"
            f"Developer's answer: {answer}"
        )
        return {
            **state,
            "clarification_needed": True,
            "clarification_question": clarifying_question,
            "clarification_answer": answer,
            "question": augmented_question,
        }

    # Developer chose not to answer — proceed, but flag that this run has
    # an unresolved ambiguity, which makes downstream escalation more
    # likely (an unanswered clarification is itself a low-confidence
    # signal, even though we don't yet have the correction-memory-based
    # novelty check from ADR-0001 Decision 2 wired up). Also logged as an
    # assumption so it's visible in the final answer, not just the live
    # trace (ADR-0004).
    assumptions = state.get("assumptions", []) + [
        {
            "step": "clarification",
            "type": "unanswered_clarification",
            "description": (
                f"Developer did not answer the clarifying question "
                f"({clarifying_question!r}); agent proceeded with its own "
                f"best-effort interpretation."
            ),
            "confirmed_by_developer": False,
        }
    ]
    return {
        **state,
        "clarification_needed": True,
        "clarification_question": clarifying_question,
        "clarification_answer": None,
        "assumptions": assumptions,
    }
