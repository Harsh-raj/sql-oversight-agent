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

from src.memory.correction_store import CorrectionStore

EVAL_LOG_PATH = Path(__file__).resolve().parent.parent / \
    "data" / "eval_log.jsonl"


def append_eval_record(
    question: str,
    sql_query: Optional[str],
    sql_result: Optional[list],
    is_correct: bool,
    correction: Optional[str] = None,
    total_attempts: Optional[int] = None,
    human_intervention: bool = False,
) -> dict:
    EVAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": question,
        "sql_query": sql_query,
        "sql_result": sql_result,
        "human_verdict": "correct" if is_correct else "incorrect",
        "correction": correction,
        # ADR-0009: needed to compute trajectory metrics (retry rate,
        # human-intervention rate) — didn't exist before this stage.
        "total_attempts": total_attempts,
        "human_intervention": human_intervention,
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

    # Real bug found while wiring correction memory (ADR-0008):
    # advance_step resets the top-level sql_query/sql_result scratch
    # fields to None even on the LAST step ("no-op if last" in its
    # comment was wrong — it always resets). The actual data lives in
    # step_results. For a multi-step run this captures only the last
    # step's query/result — a known simplification, since eval_log is
    # currently one record per RUN, not per step.
    step_results = state.get("step_results") or []
    last_step = step_results[-1] if step_results else {}
    sql_query = last_step.get("sql_query") or state.get("sql_query")
    sql_result = last_step.get("sql_result") or state.get("sql_result")

    answer = input("\nWas this answer correct? [y/n]: ").strip().lower()
    is_correct = answer.startswith("y")

    correction = None
    if not is_correct:
        correction = input(
            "What should the correct SQL/answer have been? "
        ).strip()

    # ADR-0009: aggregate across all steps for trajectory metrics.
    total_attempts = sum(s.get("attempts_used", 1)
                         for s in step_results) or None
    human_intervention = any(s.get("source") == "human" for s in step_results)

    record = append_eval_record(
        question=state.get("question"),
        sql_query=sql_query,
        sql_result=sql_result,
        is_correct=is_correct,
        correction=correction,
        total_attempts=total_attempts,
        human_intervention=human_intervention,
    )

    # Live-wire into correction memory (ADR-0008, Decision 2): a resolved
    # correction becomes available to the very next run immediately, not
    # only after someone remembers to run the seed script. CorrectionStore
    # never raises — Qdrant being unavailable just means no memory benefit
    # this run, not a crash.
    if not is_correct and correction:
        CorrectionStore().store_correction(
            question=state.get("question"),
            attempted_sql=sql_query,
            error=None,
            human_correction=correction,
        )

    return record
