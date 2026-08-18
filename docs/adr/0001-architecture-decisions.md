# ADR-0001: Core Architecture Decisions for the Analyst Agent

## Status
Accepted

## Context

We are building an autonomous agent that answers natural-language business
questions about an online course marketplace's listing data (course
catalog, categories, pricing, enrollment) by planning, generating SQL,
executing it, validating the result, and escalating to a human when
confidence is low. This ADR captures the three foundational decisions
made before writing the walking skeleton, and the tradeoffs considered
for each. The domain (online course marketplace) was chosen deliberately
as a synthetic, self-contained dataset unrelated to any employer or prior
work context — the value of the project is the agent architecture, not
the dataset.

## Decision 1: Orchestration framework — LangGraph

**Decision:** Use LangGraph to model the agent as an explicit state graph
(Planner → Schema Tool → SQL Generator → SQL Executor → Critic →
[retry | escalate | respond]).

**Alternatives considered:**
- **CrewAI** — simpler multi-agent abstraction, but hides more control flow
  than we want. This project's value is in the *retry/escalation logic*,
  which we want to reason about and test explicitly, not delegate to a
  framework's internal loop.
- **Hand-rolled state machine** — would demonstrate understanding of
  internals, but LangGraph is the most in-demand orchestration skill in
  current job postings, and using it doesn't preclude understanding what
  it's doing under the hood (we still write and own every node).

**Consequence:** Every node is a plain Python function with a typed
state input/output, so the graph structure stays inspectable and testable
independent of LangGraph itself.

## Decision 2: Model — Qwen2.5-Coder-7B via Ollama, with human escalation
rather than a second (larger/API) model as the default fallback

**Decision:** Run Qwen2.5-Coder-7B locally via Ollama for all generation
steps (planning, SQL generation). When confidence is low, escalate to a
human rather than automatically routing to a larger API-based model.

**Reasoning:**
- SQL generation is a comparatively narrow, well-represented task for a
  7B-class coder model, especially when grounded with retrieved schema
  context.
- A local-only model keeps the demo's cost/privacy story intact: no data
  leaves the machine, no per-token API cost during iteration.
- Human-in-the-loop is a more honest and more interesting design than
  silently escalating to a bigger model — it surfaces the actual
  capability boundary instead of papering over it, and it's the safer
  default for anything touching real (even synthetic) business decisions.

**Escalation triggers (v1):**
1. **Retry exhaustion** — Critic rejects the SQL/result N times (N=3).
2. **Low retrieval novelty** — no similar past case found in correction
   memory above a similarity threshold (see Decision 3), meaning this
   looks like unfamiliar territory.

Deferred to a later iteration: self-consistency checks (generate twice,
compare) and explicit model-reported confidence scores. Both are cheap
to add but are not required for the walking skeleton to prove the loop.

**Revisit condition:** If local generation quality proves too unreliable
even for narrow, schema-grounded SQL (measured via the eval harness, not
vibes), we will revisit routing hard cases to an API model — but that is
an explicit, logged decision per-query, not a silent default.

## Decision 3: Learning mechanism — retrieval-based correction memory,
not fine-tuning

**Decision:** When a human resolves an escalation, store a structured
record (question, agent's attempted plan/SQL, what was wrong, the human's
correction) as an embedding in Qdrant. Retrieve top-k similar past
corrections at generation time and inject them as few-shot context.

**Alternatives considered:**
- **Fine-tuning (LoRA/QLoRA) on corrections** — rejected for this project.
  On CPU-only, 16GB RAM hardware this is slow and fragile, and more
  importantly it's the wrong shape for the problem: every correction would
  require retraining to take effect, whereas retrieval makes each new
  correction usable immediately.

**Known limitation (documented deliberately, not hidden):** this is a
cold-start mechanism — it adds no value until enough corrections have
accumulated. For demo purposes we seed a small set of synthetic prior
corrections (`data/seed_corrections.json`) and are explicit in the
project write-up that this is seeded data, not an organically grown
correction history.

## Consequences

- The agent's "learning" is explainable in an interview as an explicit
  architectural tradeoff (retrieval vs. fine-tuning), not a black box.
- The confidence/escalation boundary is testable: we can inject known
  failure cases and assert the agent escalates when it should.
- We accept slower, sometimes-wrong SQL generation from the small local
  model as a known cost of the local-first design, mitigated by grounding,
  validation, and escalation rather than by throwing a bigger model at it.
