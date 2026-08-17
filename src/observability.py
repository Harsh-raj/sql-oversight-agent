"""
Wraps every graph node with:
- a hard timeout (only meaningful for nodes that call the local model,
  where a hang is actually possible)
- a real-time, human-readable trace of what happened and how long it took

This is deliberately console-only and synchronous — it is a strict
subset of what the deferred observability stage (Langfuse tracing) will
later persist and visualize. See ADR-0002, Decision 3.
"""

import functools
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Optional


class NodeTimeoutError(Exception):
    """Raised when a single node exceeds its allotted time budget."""


def _summarize(name: str, state: dict) -> str:
    """Short, node-specific description of what just happened, for the
    live trace. Falls back to a generic message for unknown nodes."""
    if name == "schema_tool":
        return "schema loaded"
    if name == "clarification":
        if state.get("clarification_needed"):
            return f"asked developer: {state.get('clarification_question')!r}"
        return "no clarification needed"
    if name == "planner":
        plan = state.get("plan", [])
        return f"decomposed into {len(plan)} step(s)"
    if name == "sql_generator":
        return f"generated SQL (attempt {state.get('generation_attempt')})"
    if name == "identifier_validator":
        correction = state.get("pending_correction")
        if correction:
            return (
                f"corrected {correction['error_type']} "
                f"'{correction['original_identifier']}' -> pending confirmation"
            )
        return "no correction needed"
    if name == "sql_executor":
        if state.get("sql_error"):
            return f"error: {state['sql_error']}"
        return f"{len(state.get('sql_result') or [])} row(s) returned"
    if name == "critic":
        return "VALID" if state.get("is_valid") else f"INVALID — {state.get('validation_reason')}"
    if name == "confirm_correction":
        assumptions = state.get("assumptions", [])
        last = assumptions[-1] if assumptions else {}
        return "confirmed" if last.get("confirmed_by_developer") else "REJECTED by developer"
    if name == "advance_step":
        idx = state.get("current_step_index", 0)
        total = len(state.get("plan", []))
        status = "plan complete" if state.get("plan_complete") else f"moving to step {idx + 1}/{total}"
        return status
    if name == "escalate":
        return f"escalated — {state.get('escalation_reason')}"
    if name == "respond":
        return "final answer ready"
    return "done"


def timed_node(name: str, timeout_seconds: Optional[float] = None):
    """
    Decorator for a graph node function. Prints a live start/end trace
    line and, if `timeout_seconds` is given, enforces a hard timeout by
    running the node in a worker thread.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state):
            start = time.time()
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] -> {name} starting...", flush=True)

            if timeout_seconds:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(fn, state)
                    try:
                        result = future.result(timeout=timeout_seconds)
                    except FutureTimeoutError:
                        duration = time.time() - start
                        ts_end = time.strftime("%H:%M:%S")
                        print(
                            f"[{ts_end}] x  {name} TIMED OUT after "
                            f"{duration:.1f}s (limit {timeout_seconds}s)",
                            flush=True,
                        )
                        raise NodeTimeoutError(
                            f"Node '{name}' exceeded its {timeout_seconds}s timeout"
                        )
            else:
                result = fn(state)

            duration = time.time() - start
            ts_end = time.strftime("%H:%M:%S")
            print(
                f"[{ts_end}] <- {name} done in {duration:.2f}s -> {_summarize(name, result)}",
                flush=True,
            )
            return result

        return wrapper

    return decorator
