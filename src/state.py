from typing import Optional, TypedDict


class AgentState(TypedDict, total=False):
    """
    Shared state passed between every node in the graph.

    Only `question` is required on input. Every other field is populated
    as the question moves through the graph. Keeping this flat and typed
    makes each node's contract explicit and easy to unit test in isolation.
    """

    # input
    question: str

    # schema_tool output
    schema_context: str

    # clarification output (ADR-0002)
    clarification_needed: bool
    clarification_question: Optional[str]
    clarification_answer: Optional[str]

    # planner output (ADR-0003) — plan is the ordered list of independent
    # sub-questions; current_step_index tracks which one is "in flight";
    # step_results accumulates completed steps as the plan progresses.
    plan: list
    current_step_index: int
    step_results: list
    plan_complete: bool

    # sql_generator output — represents the CURRENT step's scratch state;
    # gets folded into step_results and reset by advance_step between steps
    sql_query: Optional[str]
    generation_attempt: int  # increments on each retry, resets per step

    # set by sql_generator when a retry corrects a hallucinated table/
    # column name; consumed and cleared by confirm_correction (ADR-0004)
    pending_correction: Optional[dict]

    # every assumption made during the run (unconfirmed clarification
    # skips, confirmed/rejected identifier corrections) — always shown
    # in the final answer, not just the live trace (ADR-0004)
    assumptions: list

    # sql_executor output
    sql_result: Optional[list]
    sql_error: Optional[str]

    # critic output
    is_valid: Optional[bool]
    validation_reason: Optional[str]

    # escalation
    escalated: bool
    escalation_reason: Optional[str]

    # responder output
    final_answer: Optional[str]
