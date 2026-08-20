from langgraph.graph import END, StateGraph

from src.config import settings
from src.nodes.advance_step import advance_step, route_after_advance
from src.nodes.clarification import check_clarity
from src.nodes.confirm_correction import confirm_correction, route_after_confirmation
from src.nodes.critic import should_retry_or_escalate, validate_result
from src.nodes.escalation import escalate_to_human, route_after_escalation
from src.nodes.fast_path import check_fast_path, route_after_fast_path_check
from src.nodes.identifier_validator import validate_identifiers
from src.nodes.planner import create_plan
from src.nodes.responder import respond
from src.nodes.schema_tool import get_schema_context
from src.nodes.semantic_critic import check_semantic_validity
from src.nodes.sql_executor import execute_sql
from src.nodes.sql_generator import generate_sql
from src.observability import timed_node
from src.state import AgentState


def build_graph():
    """
    Graph shape (ADR-0011 adds `semantic_critic`; timeouts apply only to
    nodes that call the local model):

        schema_tool -> fast_path_check --(simple)--> sql_generator -> identifier_validator -> sql_executor -> critic
                             |                              ^                                                       |
                             (not simple)                   |----------------------- retry (bounded) ----------------|
                             v                                                                                       |
                        clarification -> planner --------------------------------------------------------------------+
                                                                                                                   |
                                              escalate <---- (rejected) --- confirm_correction <---- (valid + pending_correction)
                                                  ^  |                          |
                                                  |  | (human provides SQL,     (confirmed / no correction needed)
                                                  |  |  loops back through            v
                                                  |  |  validation)          semantic_critic
                                                  |  v                                |
                                                 END                    (routes via should_retry_or_escalate:
                                                                         retry / escalate / advance)
                                                                                       |
                                                                                 advance_step
                                                                                       |
                                                                 more steps? ---------+--> respond
                                                                 (loops back to sql_generator)

    semantic_critic (ADR-0011 / Stage 3) runs only after the mechanical
    critic confirms is_valid=True (and after any identifier correction
    is confirmed) — it checks whether the result actually answers the
    question (wrong aggregation, wrong sort direction, wrong grouping),
    not just whether the query ran cleanly. It doesn't introduce new
    routing: it refines is_valid/validation_reason and reuses
    critic.should_retry_or_escalate for what happens next, so a semantic
    failure draws from the same retry budget as a mechanical one.

    fast_path_check (ADR-0007) is a cheap, non-LLM heuristic gate that
    skips clarification and planning entirely for questions that are
    obviously simple (short, no comparison/multi-part language) —
    addressing the ~35-50s of latency those two LLM calls added to every
    question regardless of complexity. Every downstream safety net
    (identifier_validator, critic, semantic_critic, confirm_correction,
    escalation) still applies unchanged; only the upfront ambiguity
    check is skipped, and only when there's essentially nothing to be
    ambiguous about.

    escalate_to_human (ADR-0006) is no longer a dead end: it asks the
    developer if they know the correct SQL, and if so, routes their query
    back through identifier_validator -> sql_executor -> critic — the
    SAME validation pipeline as any model-generated query. A human-
    provided query that also fails validation routes back to escalate
    (bounded by MAX_HUMAN_ATTEMPTS) rather than the model's retry loop.
    Anything not resolved live is persisted to
    data/escalation_queue.jsonl before the run ends.
    """
    graph = StateGraph(AgentState)

    graph.add_node("schema_tool", timed_node("schema_tool")(get_schema_context))
    graph.add_node("fast_path_check", timed_node("fast_path_check")(check_fast_path))
    graph.add_node(
        "clarification",
        timed_node("clarification", settings.node_timeout_seconds)(check_clarity),
    )
    graph.add_node(
        "planner",
        timed_node("planner", settings.node_timeout_seconds)(create_plan),
    )
    graph.add_node(
        "sql_generator",
        timed_node("sql_generator", settings.node_timeout_seconds)(generate_sql),
    )
    graph.add_node(
        "identifier_validator",
        timed_node("identifier_validator")(validate_identifiers),
    )
    graph.add_node("sql_executor", timed_node("sql_executor")(execute_sql))
    graph.add_node("critic", timed_node("critic")(validate_result))
    graph.add_node(
        "semantic_critic",
        timed_node("semantic_critic", settings.node_timeout_seconds)(
            check_semantic_validity
        ),
    )
    graph.add_node(
        "confirm_correction", timed_node("confirm_correction")(confirm_correction)
    )
    graph.add_node("advance_step", timed_node("advance_step")(advance_step))
    graph.add_node("escalate", timed_node("escalate")(escalate_to_human))
    graph.add_node("respond", timed_node("respond")(respond))

    graph.set_entry_point("schema_tool")
    graph.add_edge("schema_tool", "fast_path_check")
    graph.add_conditional_edges(
        "fast_path_check",
        route_after_fast_path_check,
        {
            "sql_generator": "sql_generator",
            "clarification": "clarification",
        },
    )
    graph.add_edge("clarification", "planner")
    graph.add_edge("planner", "sql_generator")
    graph.add_edge("sql_generator", "identifier_validator")
    graph.add_edge("identifier_validator", "sql_executor")
    graph.add_edge("sql_executor", "critic")

    graph.add_conditional_edges(
        "critic",
        should_retry_or_escalate,
        {
            "retry": "sql_generator",
            "escalate": "escalate",
            "confirm": "confirm_correction",
            "advance": "semantic_critic",
        },
    )

    graph.add_conditional_edges(
        "semantic_critic",
        should_retry_or_escalate,
        {
            "retry": "sql_generator",
            "escalate": "escalate",
            "confirm": "confirm_correction",
            "advance": "advance_step",
        },
    )

    graph.add_conditional_edges(
        "confirm_correction",
        route_after_confirmation,
        {
            "semantic_critic": "semantic_critic",
            "escalate": "escalate",
        },
    )

    graph.add_conditional_edges(
        "advance_step",
        route_after_advance,
        {
            "sql_generator": "sql_generator",
            "respond": "respond",
        },
    )

    graph.add_conditional_edges(
        "escalate",
        route_after_escalation,
        {
            "identifier_validator": "identifier_validator",
            "end": END,
        },
    )
    graph.add_edge("respond", END)

    return graph.compile()
