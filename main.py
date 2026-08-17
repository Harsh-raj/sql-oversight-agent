"""
Run a question through the agent.

Usage:
    python main.py "How many courses are there in the Design category?"

Behavior (ADR-0002):
- Streams the graph step by step (rather than a single opaque `invoke`)
  so that if something crashes, we know exactly which node it happened in
  and what the state looked like at that point.
- Enforces an overall wall-clock timeout across the whole run, on top of
  the per-node timeouts already applied in the graph.
- On success, prompts for a human correctness verdict before exiting.
"""

import sys
import time
import traceback

from src.config import settings
from src.feedback import capture_human_feedback
from src.graph import build_graph


class RunTimeoutError(Exception):
    """Raised when the overall run exceeds its wall-clock budget."""


def run(question: str) -> None:
    graph = build_graph()
    initial_state = {"question": question, "generation_attempt": 0}

    start = time.time()
    last_state = dict(initial_state)

    try:
        for step_state in graph.stream(initial_state, stream_mode="values"):
            last_state = step_state
            elapsed = time.time() - start
            if elapsed > settings.run_timeout_seconds:
                raise RunTimeoutError(
                    f"Exceeded overall run timeout of "
                    f"{settings.run_timeout_seconds}s (elapsed {elapsed:.1f}s)"
                )
    except Exception:
        print("\n" + "=" * 60)
        print("RUN FAILED — full traceback below")
        print("=" * 60)
        traceback.print_exc()
        print("\nLast known state before failure:")
        for key in (
            "question",
            "plan",
            "current_step_index",
            "generation_attempt",
            "sql_query",
            "sql_error",
            "clarification_needed",
        ):
            print(f"  {key}: {last_state.get(key)}")
        print("=" * 60)
        sys.exit(1)

    print("\n" + "=" * 60)
    print(last_state.get("final_answer", "(no answer produced)"))
    print("=" * 60)

    capture_human_feedback(last_state)


def main():
    if len(sys.argv) < 2:
        print('Usage: python main.py "your question here"')
        sys.exit(1)

    run(sys.argv[1])


if __name__ == "__main__":
    main()
