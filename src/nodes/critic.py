from src.config import settings
from src.state import AgentState


def validate_result(state: AgentState) -> AgentState:
    """
    v1 validation is intentionally mechanical, not model-based:
    - Did the query error?
    - Did it return zero rows (often a sign of a wrong filter/join)?

    This is deliberately cheap. A model-based "does this actually answer
    the question" critic is a natural v2 addition, but the mechanical
    checks catch the most common failure modes for a 7B-class model
    (bad column name, empty result from an overly strict WHERE) without
    spending another generation call on every single attempt.
    """
    if state.get("sql_error"):
        return {
            **state,
            "is_valid": False,
            "validation_reason": f"Query error: {state['sql_error']}",
        }

    result = state.get("sql_result")
    if result is not None and len(result) == 0:
        return {
            **state,
            "is_valid": False,
            "validation_reason": "Query executed but returned zero rows.",
        }

    return {**state, "is_valid": True, "validation_reason": None}


def should_retry_or_escalate(state: AgentState) -> str:
    """
    Routing function (used as a LangGraph conditional edge).
    Returns one of: "retry", "escalate", "confirm", "advance".

    "confirm" (ADR-0004): a valid result from correcting a hallucinated
    table/column name goes to confirm_correction before being trusted.

    A human-provided query (ADR-0006) that fails validation goes back to
    "escalate" rather than "retry" — the model's retry budget is a
    separate concept from the human's own bounded attempts, tracked by
    escalate_to_human itself via human_attempts/MAX_HUMAN_ATTEMPTS.
    """
    if state.get("is_valid"):
        if state.get("pending_correction"):
            return "confirm"
        return "advance"

    if state.get("human_provided"):
        return "escalate"

    attempt = state.get("generation_attempt", 0)
    if attempt < settings.max_sql_retries:
        return "retry"

    return "escalate"
