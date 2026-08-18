from src.state import AgentState


def escalate_to_human(state: AgentState) -> AgentState:
    """
    Reports which step in the plan failed (ADR-0003, Decision 2: any
    single step failing escalates the WHOLE run — no partial answers),
    plus whatever steps completed successfully before it, so a human has
    full context to act on rather than just the last query tried.

    Real version (stage 4+) will:
    - Persist the full state to a review queue.
    - Block on human input rather than returning immediately.
    - On resolution, hand the (question, correction) pair to the
      correction-memory store (src/memory/correction_store.py).
    """
    reason = state.get("validation_reason", "unknown reason")
    attempts = state.get("generation_attempt", 0)
    plan = state.get("plan", [state.get("question", "")])
    step_index = state.get("current_step_index", 0)
    completed_steps = state.get("step_results", [])
    current_step_question = plan[step_index] if step_index < len(plan) else "(unknown)"

    message = (
        f"Escalating to human review after {attempts} attempt(s) on "
        f"step {step_index + 1}/{len(plan)}: {current_step_question!r}\n"
        f"Reason: {reason}\n"
        f"Last query tried: {state.get('sql_query')}\n"
        f"Steps completed before failure: {len(completed_steps)}/{len(plan)}"
    )

    print(f"[ESCALATION] {message}")

    completed_summary = ""
    if completed_steps:
        lines = [
            f"  - {s['sub_question']} -> {s['sql_result']}"
            for s in completed_steps
        ]
        completed_summary = "\n\nSteps already completed successfully:\n" + "\n".join(
            lines
        )

    return {
        **state,
        "escalated": True,
        "escalation_reason": reason,
        "final_answer": (
            f"I wasn't able to confidently answer step {step_index + 1} of "
            f"{len(plan)} ({current_step_question!r}) after {attempts} "
            f"attempt(s) — {reason}. This has been flagged for human "
            "review rather than guessing, and no partial answer is being "
            "returned." + completed_summary
        ),
    }
