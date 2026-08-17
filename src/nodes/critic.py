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

    "confirm" (ADR-0004) is new: a valid result that came from correcting
    a hallucinated table/column name doesn't go straight to advance_step
    — it goes to confirm_correction first, so the developer can catch a
    structurally-valid-but-semantically-wrong self-correction before it's
    trusted.
    """
    if state.get("is_valid"):
        if state.get("pending_correction"):
            return "confirm"
        return "advance"

    attempt = state.get("generation_attempt", 0)
    if attempt < settings.max_sql_retries:
        return "retry"

    return "escalate"
