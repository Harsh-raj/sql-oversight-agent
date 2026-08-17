# ADR-0002: Reliability, Observability, and Human Oversight

## Status
Accepted — amends ADR-0001.

## Context

The walking skeleton (ADR-0001) proved the generate → execute → validate →
retry/escalate loop works, but it has no real answer to the two hardest
criticisms this kind of project draws: *how do you know the LLM's SQL is
actually correct*, and *what happens when something goes wrong that isn't
a clean SQL error*. This ADR closes those gaps before adding new
capability (Planner, correction memory) on top of an unverified
foundation.

## Decision 1: Manual correctness verification, logged persistently

**Decision:** After every run, prompt the developer to confirm whether
the answer was actually correct. Every verdict (correct/incorrect, plus
the correct answer if wrong) is appended to `data/eval_log.jsonl`.

**Why not wait for automated evals (deferred to a later stage):**
automated trajectory evals measure *process* (did it retry sensibly, did
it escalate appropriately) — they cannot tell you if a syntactically
valid, successfully-executed query answers the *wrong* question. Only a
human who knows the intended semantics can catch that class of error, and
it's the class most likely to produce a confident-looking wrong answer.
This log is also the seed data for the correction-memory store (ADR-0001,
Decision 3) — real human-labeled cases instead of only synthetic ones.

## Decision 2: Bounded, observable failure — per-node timeout, run-level
timeout, and full-context tracebacks on crash

**Decision:**
- Any node that calls the local model (currently: clarification check,
  SQL generation) runs under a hard per-node timeout. A timeout is treated
  as a generation failure and feeds the same retry/escalate logic as any
  other error — it does not hang the process.
- The overall run is bounded by a wall-clock budget, checked between
  graph steps.
- If anything fails outside the normal retry/escalate path (a real
  exception — connection error, unexpected crash), the run prints the
  full Python traceback *and* the last known state (question, attempt
  number, last query, last error) before exiting non-zero. Nothing is
  swallowed silently.

**Why this matters for the project's credibility:** an agent that can
hang indefinitely or fail with a bare, contextless stack trace is not
something you can defend as "reliable" in an interview. Bounding time and
preserving failure context turns "it crashed" into "it crashed at attempt
2 of SQL generation because Ollama wasn't running — here's the exact
state at that point."

## Decision 3: Real-time, human-readable decision trace

**Decision:** Every node prints, as it happens: which node started, what
it decided, and how long it took. This is deliberately console-only and
synchronous — no persistence, no UI. It is a strict subset of what the
deferred observability stage (Langfuse tracing) will eventually persist,
so building it now doesn't create rework later; it creates the raw
material that stage will structure and store.

## Decision 4 (supersedes part of ADR-0001): the agent asks for
clarification instead of silently assuming

**Original decision (ADR-0001, embedded in the SQL generator's system
prompt):** "If the question is ambiguous about a metric, pick the most
reasonable concrete interpretation and proceed."

**New decision:** A `clarification` node runs after schema grounding and
before SQL generation. It checks whether the question has ambiguity that
would change *what* gets computed (undefined metric, missing time range,
unclear comparison baseline) — not stylistic ambiguity (sort order,
formatting). If so, it pauses and asks the developer directly via CLI
input before generation proceeds. The SQL generator's prompt is updated
accordingly: it no longer improvises interpretations, because that
responsibility has moved upstream.

**Why the reversal:** ADR-0001's original reasoning was to keep the
walking skeleton simple and non-interactive. That tradeoff is no longer
acceptable once "hallucination is intolerable" is a hard requirement —
an agent that silently guesses at intent is a hallucination risk by
construction, even if every individual SQL query it writes is
syntactically perfect. This is logged as a supersede, not a silent edit,
because it's exactly the kind of design reversal that should be visible
in the project's history.

## Decision 5: Bias toward escalation over forced automation

**Decision:** Lower the retry ceiling from 3 to 2, and keep the Responder
strictly mechanical (it echoes literal query results — no LLM-generated
narrative summary layer). Any future narrative/summary layer is
explicitly out of scope until it comes with its own groundedness
guarantee (every claim traceable to a specific returned value).

**Why:** every additional retry is another chance for the model to
produce a plausible-looking but wrong query instead of admitting it's
stuck. Escalating one attempt sooner is a deliberate trade of full
automation for reliability, matching the requirement directly.

## Consequences

- Slower iteration (clarification pauses, manual feedback prompts) in
  exchange for a defensible reliability story.
- The eval log becomes a real, growing regression-test asset almost
  immediately, rather than something built later from scratch.
- The SQL generator's prompt now depends on the clarification node having
  already run — the two are coupled by design, not incidentally.
