# ADR-0003: Multi-Step Planning via the Planner Node

## Status
Accepted — extends ADR-0001/0002's single-question loop to multi-part
questions.

## Context

The walking skeleton only ever answers one atomic question per run. Real
business questions are often multi-part ("compare X and Y", "break this
down by category and by region"). A Planner node decomposes the
(already-clarified) question into an ordered list of independent
sub-questions, each of which goes through the existing
generate → execute → validate → retry loop on its own.

## Decision 1: Sub-questions must be independent — no cross-step chaining (v1)

**Decision:** The Planner is instructed to only decompose a question into
steps that can each be answered by one SQL query with no dependency on
another step's result. If a question genuinely requires chaining
("find the top category, then find its average price"), the Planner
folds it back into a single step rather than attempting composition.

**Why:** Chaining requires the SQL generator to consume a previous step's
*result* as input to the next query — a meaningfully harder capability
(essentially multi-hop reasoning) that deserves its own design pass, not
a wedged-in extension of the current architecture. Attempting it now
would risk exactly the kind of confident-but-wrong composition failure
ADR-0002 was built to prevent. This is a real, acknowledged scope limit,
not an oversight.

**Revisit condition:** if a genuinely useful class of business questions
turns out to need chaining, that becomes its own ADR — likely requiring
the SQL generator to see prior step results as structured input, plus a
stricter validation pass on the composed logic.

## Decision 2: Any single step failing escalates the entire run

**Decision:** If any sub-question exhausts its retries, the whole run
escalates to the human — it does not return a partial answer covering
only the steps that succeeded.

**Why:** A partial multi-step answer is arguably worse than no answer —
it looks complete but silently omits the piece that failed, which is a
subtler and more dangerous failure mode than an obvious full escalation.
This is the same "bias toward escalation over forced automation"
principle from ADR-0002, Decision 5, applied at the plan level instead
of the single-query level.

## Decision 3: Plan parsing is line-based with a safe single-step fallback

**Decision:** The Planner asks the model to output one sub-question per
line, each prefixed with "- ". If parsing finds zero valid lines (model
didn't follow the format), the whole original question is used as a
single-step plan rather than crashing or retrying the planning call
itself.

**Why:** The Planner's own output format is itself a place a 7B model can
misbehave. Treating malformed planner output as "this is just one step"
degrades gracefully to the previous (working) walking-skeleton behavior
instead of introducing a new failure mode on top of the ones already
handled downstream.

## Consequences

- Single-part questions (the majority case so far) still work exactly as
  before — the Planner just produces a one-item plan for them.
- The escalation message must now report *which step* in the plan failed
  and what (if anything) completed before it, not just the last query
  tried.
- `AgentState` grows three fields: `plan`, `current_step_index`,
  `step_results` — deliberately kept as plain lists/ints rather than a
  nested sub-state, so existing single-step logic (critic, retry) needs
  no changes beyond reading the "current step's" question from the plan
  instead of the top-level question.
