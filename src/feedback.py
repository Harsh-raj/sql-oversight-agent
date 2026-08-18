"""
Captures a human's correctness verdict on each run's answer and appends
it to a persistent log. This is deliberately separate from (and available
well before) the automated trajectory evals deferred to a later stage —
see ADR-0002, Decision 1, for why a human verdict on *correctness* and an
automated check on *process* are answering different questions.

This log doubles as:
- a growing regression-test set (compare future runs of the same question
  against past verdicts)
- the seed data source for the correction-memory store (ADR-0001,
  Decision 3) once that stage is built
"""

import json
import time
from pathlib import Path
from typing import Optional

EVAL_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "eval_log.jsonl"


def append_eval_record(
    question: str,
    sql_query: Optional[str],
    sql_result: Optional[list],
    is_correct: bool,
    correction: Optional[str] = None,
) -> dict:
    EVAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": question,
        "sql_query": sql_query,
        "sql_result": sql_result,
        "human_verdict": "correct" if is_correct else "incorrect",
        "correction": correction,
    }

    with open(EVAL_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")

    return record


def capture_human_feedback(state: dict) -> Optional[dict]:
    """
    Interactive CLI prompt run after every answer. Skipped automatically
    if the run ended in escalation (there's no answer to verify yet) or
    if there's no question in state (defensive — shouldn't happen).
    """
    if state.get("escalated"):
        return None
    if not state.get("question"):
        return None

    answer = input("\nWas this answer correct? [y/n]: ").strip().lower()
    is_correct = answer.startswith("y")

    correction = None
    if not is_correct:
        correction = input(
            "What should the correct SQL/answer have been? "
        ).strip()

    return append_eval_record(
        question=state.get("question"),
        sql_query=state.get("sql_query"),
        sql_result=state.get("sql_result"),
        is_correct=is_correct,
        correction=correction,
    )
