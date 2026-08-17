# ADR-0004: Surfacing and Confirming Model Assumptions

## Status
Accepted.

## Context

ADR-0002's retry fix (Decision from the live-bug fix session) makes the
SQL generator self-correct hallucinated table/column names using a
targeted retry prompt. That's good for getting to a *working* query, but
it introduces a new problem: the correction itself is an assumption the
model makes about what you meant ("you said 'courses', I'm going to use
'course_listings' instead") — and until now, that assumption was only
visible in the scrolling live trace, not in the final answer. A developer
who didn't watch the trace in real time would have no way to know a
correction happened at all.

More generally: any point where the agent silently proceeds despite
uncertainty (an unconfirmed identifier correction, a clarifying question
the developer chose not to answer) is a place hallucination risk hides —
even if the individual query that results is syntactically valid.

## Decision 1: Identifier corrections during retry require explicit confirmation

**Decision:** When a retry succeeds specifically by correcting a
table/column name flagged by `_build_retry_feedback`, the run pauses
before advancing to the next step. It shows the developer the original
(bad) query and the corrected one, and asks for a yes/no confirmation.

- **Confirmed:** proceed normally; the confirmation is logged as a
  resolved assumption.
- **Rejected:** the run escalates immediately — treated the same as
  exhausting retries. We do not attempt a further auto-retry after an
  explicit human rejection; at that point the model has already shown it
  can produce a structurally valid but semantically wrong correction, and
  continuing to retry is asking it to keep guessing after being told the
  guess was wrong once already.

**Why block instead of just noting it and moving on:** this project's
explicit standing requirement (see ADR-0002) is that reliability
outranks full automation. A silently-accepted self-correction is exactly
the class of "confident but possibly wrong" output that requirement was
meant to prevent — the query runs and returns real data, which makes it
easy to mistake for a correct answer instead of a guess wearing a
answer's clothes.

## Decision 2: Every assumption made during a run — not just corrections — appears in the final answer

**Decision:** `AgentState` gains an `assumptions` list. Any node can
append to it. Currently two sources populate it:
- A confirmed or rejected identifier correction (Decision 1).
- A clarifying question the developer chose not to answer (from the
  clarification node — this was already logged to the live trace, but
  not previously surfaced in the final output).

The Responder always renders an "Assumptions made during this run"
section when the list is non-empty, and `feedback.py` persists the list
alongside the correctness verdict in `data/eval_log.jsonl`.

**Why put this in the final answer, not just the trace:** the live trace
is real-time and ephemeral — useful while watching a run happen, useless
for someone reviewing a saved answer later, or someone who wasn't
watching when it happened. The final answer is the artifact that
persists; if an assumption isn't visible there, it's effectively
invisible to anyone judging the answer's correctness after the fact.

## Consequences

- Runs that require a correction now cost an extra blocking prompt, on
  top of the clarification prompt already added in ADR-0002. This is an
  explicit, repeated tradeoff of automation for reliability — consistent
  with prior decisions, not a new philosophy.
- The eval log (`data/eval_log.jsonl`) becomes richer: a human's
  correctness verdict can now be cross-referenced against exactly which
  assumptions were made and whether the developer confirmed them at the
  time — useful groundwork for the deferred correction-memory stage.
- `pending_correction` is scoped to one identifier correction at a time
  per step. If multiple different bad identifiers appear across retries
  on the same step, only the most recent is tracked for confirmation —
  a deliberate simplification for v1, noted here rather than hidden.
