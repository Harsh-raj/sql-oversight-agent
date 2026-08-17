"""
Runs after a step passes validation, but only when that success came from
correcting a hallucinated table/column name (see sql_generator's
pending_correction). Pauses and asks the developer to confirm the
correction actually reflects what they meant, rather than silently
trusting a structurally-valid-but-possibly-wrong self-correction.

See ADR-0004 for why this is a hard block rather than a note added after
the fact.
"""

from src.state import AgentState


def confirm_correction(state: AgentState) -> AgentState:
    correction = state["pending_correction"]

    print(
        f"\n[ASSUMPTION CHECK] The first attempt at this step referenced "
        f"'{correction['original_identifier']}' (a {correction['error_type']} "
        f"that doesn't exist in the schema).\n"
        f"  Original query:  {correction['original_query']}\n"
        f"  Corrected query: {state['sql_query']}\n"
    )
    answer = input(
        "Does the corrected query look like the right interpretation? [y/n]: "
    ).strip().lower()
    confirmed = answer.startswith("y")

    assumption_record = {
        "step": correction["step_question"],
        "type": "identifier_correction",
        "original_identifier": correction["original_identifier"],
        "original_query": correction["original_query"],
        "corrected_query": state["sql_query"],
        "confirmed_by_developer": confirmed,
    }
    assumptions = state.get("assumptions", []) + [assumption_record]

    if not confirmed:
        # Treated the same as exhausting retries (ADR-0004, Decision 1):
        # we do not attempt a further auto-retry after an explicit human
        # rejection of the model's assumption.
        return {
            **state,
            "assumptions": assumptions,
            "pending_correction": None,
            "is_valid": False,
            "validation_reason": (
                f"Developer rejected the model's assumed correction "
                f"('{correction['original_identifier']}' -> corrected query)."
            ),
        }

    return {
        **state,
        "assumptions": assumptions,
        "pending_correction": None,
    }


def route_after_confirmation(state: AgentState) -> str:
    """Routing function: "advance_step" if confirmed, "escalate" if the
    developer rejected the correction."""
    return "advance_step" if state.get("is_valid") else "escalate"
