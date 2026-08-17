"""
Runs after a step passes validation. Folds the current step's result into
step_results, resets the per-step scratch state (generation_attempt,
sql_query, sql_result, sql_error) for whatever comes next, and marks
whether the whole plan is complete.

Kept as its own node — rather than logic inside the critic's routing
function — because it needs to *mutate* state (append to step_results,
advance the index), and LangGraph conditional-edge functions can only
return a routing string, not modify state.
"""

from src.state import AgentState


def advance_step(state: AgentState) -> AgentState:
    current_step_question = state["plan"][state["current_step_index"]]

    step_record = {
        "sub_question": current_step_question,
        "sql_query": state.get("sql_query"),
        "sql_result": state.get("sql_result"),
    }

    step_results = state.get("step_results", []) + [step_record]
    next_index = state["current_step_index"] + 1
    is_last = next_index >= len(state["plan"])

    return {
        **state,
        "step_results": step_results,
        "current_step_index": next_index,
        "plan_complete": is_last,
        # reset per-step scratch state for the next step (no-op if last)
        "generation_attempt": 0,
        "sql_query": None,
        "sql_result": None,
        "sql_error": None,
        "is_valid": None,
        "validation_reason": None,
        "pending_correction": None,
    }


def route_after_advance(state: AgentState) -> str:
    """Routing function: "respond" if the plan is done, else back to
    sql_generator for the next independent sub-question."""
    return "respond" if state.get("plan_complete") else "sql_generator"
