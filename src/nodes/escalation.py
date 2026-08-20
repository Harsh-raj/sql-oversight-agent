"""
Real human-in-the-loop escalation (ADR-0006). Previously this node just
printed a message and ended the run — informing the human, not consulting
them. Now:

1. Asks the developer directly whether they know the correct SQL for the
   failing step.
2. If yes, their query is routed through the SAME identifier_validator ->
   sql_executor -> critic pipeline as any model-generated query — no
   special trust just because a human typed it.
3. Bounded to MAX_HUMAN_ATTEMPTS, same principle as the model's own
   bounded retries (ADR-0002).
4. Anything that doesn't get resolved live is persisted to
   data/escalation_queue.jsonl with full context, rather than only
   existing as scrolled-past console output.
"""

import json
import time
from pathlib import Path

from src.state import AgentState

ESCALATION_QUEUE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "escalation_queue.jsonl"
)
MAX_HUMAN_ATTEMPTS = 2


def _persist_to_review_queue(state: AgentState, reason: str) -> dict:
    ESCALATION_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)

    plan = state.get("plan") or [state.get("question", "")]
    step_index = state.get("current_step_index", 0)
    completed_steps = state.get("step_results", [])

    # ADR-0009: aggregate attempts across completed steps plus the
    # failing step's own attempt count, for trajectory metrics.
    total_attempts = sum(s.get("attempts_used", 1) for s in completed_steps) + state.get(
        "generation_attempt", 0
    )
    human_intervention = any(
        s.get("source") == "human" for s in completed_steps
    ) or state.get("human_provided", False)

    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": state.get("question"),
        "step": plan[step_index] if step_index < len(plan) else None,
        "step_index": step_index,
        "total_steps": len(plan),
        "reason": reason,
        "last_query_tried": state.get("sql_query"),
        "steps_completed": completed_steps,
        "total_attempts": total_attempts,
        "human_intervention": human_intervention,
        "resolved": False,
    }

    with open(ESCALATION_QUEUE_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")

    return record


def _finalize_escalation(state: AgentState, reason: str) -> AgentState:
    plan = state.get("plan") or [state.get("question", "")]
    step_index = state.get("current_step_index", 0)
    completed_steps = state.get("step_results", [])
    current_step_question = (
        plan[step_index] if step_index < len(plan) else "(unknown)"
    )

    completed_summary = ""
    if completed_steps:
        lines = [
            f"  - {s['sub_question']} -> {s['sql_result']}" for s in completed_steps
        ]
        completed_summary = "\n\nSteps already completed successfully:\n" + "\n".join(
            lines
        )

    _persist_to_review_queue(state, reason)

    return {
        **state,
        "escalated": True,
        "escalation_reason": reason,
        "human_wants_to_retry": False,
        "final_answer": (
            f"I wasn't able to confidently answer step {step_index + 1} of "
            f"{len(plan)} ({current_step_question!r}) — {reason}. This has "
            f"been logged to the review queue for follow-up, and no partial "
            f"answer is being returned." + completed_summary
        ),
    }


def escalate_to_human(state: AgentState) -> AgentState:
    reason = state.get("validation_reason", "unknown reason")
    attempts = state.get("generation_attempt", 0)
    plan = state.get("plan") or [state.get("question", "")]
    step_index = state.get("current_step_index", 0)
    completed_steps = state.get("step_results", [])
    current_step_question = (
        plan[step_index] if step_index < len(plan) else "(unknown)"
    )
    human_attempts = state.get("human_attempts", 0)

    print(
        f"\n[ESCALATION] Step {step_index + 1}/{len(plan)}: "
        f"{current_step_question!r}"
    )
    print(f"Reason: {reason}")
    print(f"Last query tried: {state.get('sql_query')}")
    print(f"Steps completed before failure: {len(completed_steps)}/{len(plan)}")
    if attempts:
        print(f"(Model attempts on this step: {attempts})")

    if human_attempts >= MAX_HUMAN_ATTEMPTS:
        print(
            f"\nAlready tried {human_attempts} human-provided "
            f"quer{'y' if human_attempts == 1 else 'ies'} without success. "
            "Ending here rather than looping indefinitely."
        )
        return _finalize_escalation(state, reason)

    answer = input(
        "\nDo you know the correct SQL for this step? [y/n]: "
    ).strip().lower()

    if not answer.startswith("y"):
        return _finalize_escalation(state, reason)

    human_sql = input("Enter the SQL query: ").strip()

    # Route back through the full validation pipeline — human input gets
    # no special trust (ADR-0006, Decision 1).
    return {
        **state,
        "sql_query": human_sql,
        "human_provided": True,
        "human_attempts": human_attempts + 1,
        "human_wants_to_retry": True,
        "sql_error": None,
        "is_valid": None,
        "validation_reason": None,
        "pending_correction": None,
    }


def route_after_escalation(state: AgentState) -> str:
    """Routing function: back through validation if the developer wants
    to try their own query, otherwise the run ends here."""
    return "identifier_validator" if state.get("human_wants_to_retry") else "end"
