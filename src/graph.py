from langgraph.graph import END, StateGraph

from src.config import settings
from src.nodes.advance_step import advance_step, route_after_advance
from src.nodes.clarification import check_clarity
from src.nodes.confirm_correction import confirm_correction, route_after_confirmation
from src.nodes.critic import should_retry_or_escalate, validate_result
from src.nodes.escalation import escalate_to_human
from src.nodes.identifier_validator import validate_identifiers
from src.nodes.planner import create_plan
from src.nodes.responder import respond
from src.nodes.schema_tool import get_schema_context
from src.nodes.sql_executor import execute_sql
from src.nodes.sql_generator import generate_sql
from src.observability import timed_node
from src.state import AgentState


def build_graph():
    """
    Graph shape (ADR-0005 adds `identifier_validator`; timeouts apply
    only to nodes that call the local model):

        schema_tool -> clarification -> planner -> sql_generator -> identifier_validator -> sql_executor -> critic
                                                          ^                                                       |
                                                          |----------------------- retry (bounded) ----------------|
                                                                                                                   |
                                              escalate <---- (rejected) --- confirm_correction <---- (valid + pending_correction)
                                                  ^                              |
                                                  |                       (confirmed / no correction needed)
                                                  |                              v
                                                  +------------------------ advance_step
                                                                                  |
                                                            more steps? ----------+--> respond
                                                            (loops back to sql_generator)

    identifier_validator (ADR-0005) mechanically checks the generated
    SQL's table/column references against the real schema on every
    attempt (not just retries) and fuzzy-match-substitutes a confident
    correction if one exists — flagged through the same
    pending_correction/confirm_correction gate used for LLM-retry-based
    corrections. If it can't confidently resolve a bad identifier, the
    query proceeds unchanged to execution, where the existing
    execution-error + retry path (ADR-0002/0004) still applies as a
    fallback.
    """
    graph = StateGraph(AgentState)

    graph.add_node("schema_tool", timed_node("schema_tool")(get_schema_context))
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
        "confirm_correction", timed_node("confirm_correction")(confirm_correction)
    )
    graph.add_node("advance_step", timed_node("advance_step")(advance_step))
    graph.add_node("escalate", timed_node("escalate")(escalate_to_human))
    graph.add_node("respond", timed_node("respond")(respond))

    graph.set_entry_point("schema_tool")
    graph.add_edge("schema_tool", "clarification")
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
            "advance": "advance_step",
        },
    )

    graph.add_conditional_edges(
        "confirm_correction",
        route_after_confirmation,
        {
            "advance_step": "advance_step",
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

    graph.add_edge("escalate", END)
    graph.add_edge("respond", END)

    return graph.compile()
