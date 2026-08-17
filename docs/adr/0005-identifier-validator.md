# ADR-0005: Mechanical Pre-Execution Identifier Validation

## Status
Accepted.

## Context

ADR-0004's retry-based correction (explicit table list + targeted retry
feedback + temperature bump) was tested live twice against real Ollama
output. Both times, the model repeated the exact same hallucinated table
name on retry, unchanged, despite being told explicitly which identifier
was wrong and what the valid options were. Prompt engineering increases
the odds of self-correction but cannot guarantee it — a small model's
confidence in a wrong answer can outweigh corrective instructions in the
same prompt. Continuing to refine wording is chasing diminishing returns
on a problem that has a more reliable, non-LLM-dependent solution.

## Decision 1: Validate identifiers mechanically, before execution

**Decision:** A new node, `identifier_validator`, runs between
`sql_generator` and `sql_executor` on every attempt (not just retries).
It parses the generated SQL (via `sqlparse`) to find referenced table and
column names, checks them against the real schema, and — if a reference
doesn't exist — looks for a confident fuzzy match among the valid names.
A confident match is substituted directly in the SQL text.

**Why this is more reliable than the retry-based approach:** it doesn't
depend on the model reading or acting on anything. It's ordinary code:
either a string similarity check finds a confident match or it doesn't.
This removes the specific failure mode observed live (the model ignoring
explicit correction instructions) rather than trying to word around it.

## Decision 2: Reuse the existing confirmation gate — don't build a new one

**Decision:** When the validator substitutes an identifier, it populates
the same `pending_correction` field that ADR-0004's `confirm_correction`
node already reads. No new confirmation mechanism, no new state shape —
the mechanical correction and the LLM-retry-based correction both flow
through the same developer-facing gate.

**Why:** the confirmation requirement (ADR-0004) is about the *fact* that
an assumption was made, not about *how* it was made. Building a second,
parallel confirmation path would duplicate logic and create two slightly
different places a developer has to learn to watch for.

## Decision 3: Fuzzy matching requires both a minimum similarity AND a
clear margin over the next-best candidate

**Decision:** A candidate is only auto-substituted if (a) its similarity
score clears a minimum threshold, and (b) it's clearly better than the
second-best candidate (a minimum gap, not just the top score). If either
condition fails, the validator leaves the query unchanged and lets it
proceed to execution — falling back to the existing execution-error +
retry path from ADR-0002/0004.

**Why the margin check matters:** with today's single-table schema, a
wrong table name has exactly one plausible correct answer, so ambiguity
isn't yet a real risk. That won't hold once a schema has multiple
similarly-named tables or columns — at that point, confidently picking
between two close candidates is itself a hallucination risk in a
different disguise. Building the margin check in now, even though it's
not load-bearing yet, avoids a silent regression later.

## Decision 4: This is a heuristic parser, not a full SQL semantic analysis

**Decision:** Table/column extraction handles the query shapes this
project currently generates — single-table, non-aliased, non-subquery
aggregate queries. Qualified references (`table.column`), aliases, joins,
and CTEs are not specifically handled and may not be parsed correctly.

**Why this is acceptable now, and what would change it:** the Planner
(ADR-0003) explicitly limits itself to independent, single-query steps
for the same reason — the project hasn't yet taken on multi-table
composition. If/when joins become a real requirement, this parser needs
a corresponding upgrade (likely: resolve aliases first, then validate
qualified references against the correct table) — noted here as a known
gap, not a silent limitation discovered later.

## Consequences

- The old retry-based correction path (ADR-0004) is not removed — it
  remains the fallback for cases the fuzzy matcher can't confidently
  resolve (a wrong identifier with no close match, or an ambiguous one).
  The two mechanisms are complementary: mechanical validation is the
  first line of defense; LLM retry is the fallback for what it can't
  resolve.
- `sqlparse` is added as a new dependency — a small, pure-Python library
  with no C extensions, chosen over a regex-only approach specifically
  because regex-based table/column extraction breaks down quickly on
  anything beyond the simplest queries, and this project already plans
  to eventually need more query complexity (joins, in a future ADR).
- The validator's own "original_query" tracking, when it fires
  immediately after a retry-based correction already fired once on the
  same attempt, reflects the most recent change rather than the very
  first bad query — the same single-slot simplification already accepted
  in ADR-0004 for `pending_correction`.
