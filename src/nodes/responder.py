from src.state import AgentState


def respond(state: AgentState) -> AgentState:
    """
    Formats the final answer across all completed plan steps. Still
    strictly mechanical — no LLM-generated narrative summary layer — each
    step's literal query and result are shown so the answer is fully
    traceable (ADR-0002, Decision 5: keeping the Responder mechanical is
    part of the zero-hallucination-tolerance stance).

    Always surfaces every assumption made during the run (ADR-0004) —
    identifier corrections the developer confirmed, and any clarifying
    question the developer chose not to answer — so the final artifact is
    self-contained even if nobody watched the live trace.
    """
    lines = [f"Question: {state['question']}", ""]

    step_results = state.get("step_results", [])
    for i, step in enumerate(step_results, start=1):
        source_note = " (human-provided query)" if step.get("source") == "human" else ""
        lines.append(f"Step {i}{source_note}: {step['sub_question']}")
        lines.append(f"  Query:  {step['sql_query']}")
        lines.append(f"  Result: {step['sql_result']}")
        lines.append("")

    assumptions = state.get("assumptions", [])
    if assumptions:
        lines.append("Assumptions made during this run:")
        for a in assumptions:
            if a.get("type") == "identifier_correction":
                status = "confirmed" if a["confirmed_by_developer"] else "REJECTED"
                lines.append(
                    f"  - [{status}] Step {a['step']!r}: corrected "
                    f"'{a['original_identifier']}' -> used a different "
                    f"identifier instead."
                )
            else:
                lines.append(f"  - {a.get('description', a)}")
        lines.append("")

    return {**state, "final_answer": "\n".join(lines).rstrip()}
