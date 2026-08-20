# Analyst Agent

An autonomous agent that answers natural-language business questions about
an online course marketplace's listing data (course catalog, categories,
pricing, enrollment) by generating SQL, executing it, validating the
result, retrying on failure, and escalating to a human when it can't
confidently answer — rather than guessing.

The dataset is entirely synthetic and self-contained — an invented
course-marketplace domain with no connection to any employer or external
work data. The point of the project is the agent architecture, not the
dataset.

Architecture decisions and their tradeoffs are documented in:
- [`docs/adr/0001-architecture-decisions.md`](docs/adr/0001-architecture-decisions.md) — framework, model, and learning-mechanism choices
- [`docs/adr/0002-reliability-and-human-oversight.md`](docs/adr/0002-reliability-and-human-oversight.md) — manual correctness verification, timeouts, live tracing, clarification-seeking, and the escalation-biased retry policy
- [`docs/adr/0003-planner-node.md`](docs/adr/0003-planner-node.md) — multi-step planning, why cross-step chaining is out of scope for now, and why any single failed step escalates the whole run
- [`docs/adr/0004-assumption-transparency.md`](docs/adr/0004-assumption-transparency.md) — why the agent pauses for developer confirmation whenever it self-corrects a hallucinated table/column name, and why every assumption made during a run is surfaced in the final answer, not just the live trace
- [`docs/adr/0005-identifier-validator.md`](docs/adr/0005-identifier-validator.md) — why prompt-based self-correction alone proved unreliable in live testing, and the mechanical fuzzy-match validator that replaced dependence on it
- [`docs/adr/0006-human-in-the-loop-escalation.md`](docs/adr/0006-human-in-the-loop-escalation.md) — why escalation now offers the developer a real chance to resolve a failing step live (routed through the same validation pipeline as any model query), bounded the same way the model's own retries are, with anything unresolved persisted to a review queue
- [`docs/adr/0007-fast-path-heuristic.md`](docs/adr/0007-fast-path-heuristic.md) — why a cheap, non-LLM heuristic gate now skips clarification and planning for obviously simple questions, addressing the ~35-50s of latency those two LLM calls added to every question regardless of complexity
- [`docs/adr/0008-correction-memory.md`](docs/adr/0008-correction-memory.md) — why correction memory is live-wired into feedback capture (not just an offline seed script), embeddings via Ollama rather than a new ML dependency, and why retrieval failure must never break a run
- [`docs/adr/0009-trajectory-evals-and-tracing.md`](docs/adr/0009-trajectory-evals-and-tracing.md) — why Langfuse tracing is fully optional and disabled by default, one trace per run with each retry as its own span, and why trajectory evals are a local offline report rather than a live dashboard
- [`docs/adr/0010-stub-mode-integration-tests.md`](docs/adr/0010-stub-mode-integration-tests.md) — why a real stdlib HTTP server (not a fancier mock) is needed to catch schema mismatches at the `ollama` client boundary, and a real finding about the client being constructed once at import time
- [`docs/adr/0011-semantic-critic.md`](docs/adr/0011-semantic-critic.md) — why the semantic critic reuses the mechanical critic's retry/escalate routing instead of duplicating it, and why it applies universally including fast-pathed questions

## Current stage: all 9 planned stages complete

```
schema_tool -> fast_path_check --(simple)--> sql_generator -> identifier_validator -> sql_executor -> critic
                     |                              ^                                                       |
                     (not simple)                   |----------------------- retry (bounded) ----------------|
                     v                                                                                       |
                clarification -> planner --------------------------------------------------------------------+
                                                                                                            |
                                      escalate <---- (rejected) --- confirm_correction <---- (valid + pending_correction)
                                          ^  |                          |
                                          |  | (human provides SQL,     (confirmed / no correction needed)
                                          |  |  loops back through            v
                                          |  |  validation)          semantic_critic
                                          |  v                                |
                                         END                    (routes via should_retry_or_escalate:
                                                                  retry / escalate / advance)
                                                                               |
                                                                         advance_step
                                                                               |
                                                         more steps? ---------+--> respond
                                                         (loops back to sql_generator)
```

`fast_path_check` (ADR-0007) is a cheap, non-LLM heuristic that skips
clarification and planning entirely for questions that are obviously
simple — every downstream safety net still applies regardless,
including the semantic critic below.

`semantic_critic` (ADR-0011, Stage 3) runs after the mechanical critic
confirms a query executed cleanly, and checks something mechanical
checks can't: whether the result actually answers the question — wrong
aggregation, wrong sort direction ("highest" answered with `MIN` instead
of `MAX`), wrong grouping. It doesn't add new routing — it refines
`is_valid`/`validation_reason` and reuses the same retry/escalate logic,
so a semantic failure draws from the same retry budget as a mechanical
one. Toggleable via `SEMANTIC_CRITIC_ENABLED` — it's a real, repeated
latency cost (one more LLM call per successful attempt), named
explicitly rather than hardcoded on.

Each step in the plan runs through generate → **validate identifiers
mechanically** → execute → validate result. Before a query ever touches
the database, `identifier_validator` checks its table/column references
against the real schema and fuzzy-match-corrects an obvious hallucination
(e.g. a model-generated `Courses` → the real `course_listings`) —
deterministically, not by hoping the model reads a correction prompt
correctly (see ADR-0005: this was added after prompt-based self-correction
failed twice in live testing). Any correction — mechanical or
retry-based — pauses for developer confirmation before being trusted;
rejecting it routes to a real, interactive escalation (ADR-0006): the
developer is offered a chance to resolve the step themselves, with their
query going through the same validation pipeline — bounded to 2 attempts,
persisted to `data/escalation_queue.jsonl` if unresolved. Every
assumption made during a run is listed explicitly in the final answer.

Two more things run alongside all of this, both optional and both
degrading silently if unavailable: **correction memory** (ADR-0008) —
`sql_generator` retrieves similar past human corrections from Qdrant on
the first attempt per step and injects them as few-shot context — and
**Langfuse tracing** (ADR-0009) — one trace per run, one span per node,
disabled by default. Separately, `scripts/run_trajectory_evals.py`
computes offline aggregate metrics (retry rate, escalation rate, human-
intervention rate) from the accumulated local logs.

`advance_step` records a completed step and either loops back for the
next one or moves to `respond` once the plan is done. If *any* step
exhausts its retries or has its correction rejected, the whole run
escalates — no partial multi-step answers (see ADR-0003, Decision 2).

**Known limitation, by design:** steps can't depend on each other's
results yet — "find the top category, then find its average price"
gets folded back into a single step rather than attempted as a chain.
See ADR-0003, Decision 1.

Also in place (ADR-0002):
- **Clarification** — asks the developer directly when the question has
  ambiguity that would change the actual computation.
- **Live decision trace** — every node prints what it did and how long it
  took, as it happens.
- **Per-node + run-level timeouts** — a hung model call can't hang the
  process; the whole run has a wall-clock budget too.
- **Full-context crash reporting** — any real failure prints the full
  traceback plus the last known state (which node, which plan step, what
  attempt, what error).
- **Manual correctness verification** — after every successful run, the
  CLI asks you to confirm the answer was actually right, and logs the
  verdict (and correction, if wrong) to `data/eval_log.jsonl`.
- **Correction memory** — when a run is marked incorrect with a
  correction, that correction is embedded and stored in Qdrant; future
  similar questions retrieve it automatically as context before
  generation (ADR-0008).

No multi-turn conversation yet — that's a later stage (see below).

## Setup

1. **Install Ollama** natively (not in Docker — better CPU performance):
   https://ollama.com
   ```
   ollama pull qwen2.5-coder:7b
   ollama pull all-minilm
   ```
   (`all-minilm` is a small embedding model used by correction memory —
   see ADR-0008. The agent still works without it; correction memory
   just won't have anything to embed.)

2. **Start Qdrant** (correction memory, ADR-0008, is live and needs this
   running — the agent still works without it, just with no memory
   benefit):
   ```
   docker-compose up -d
   ```

3. **Python environment:**
   ```
   python -m venv venv
   source venv/bin/activate   # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   cp .env.example .env
   ```

4. **Seed the sandbox database:**
   ```
   python scripts/seed_db.py
   ```

5. **Run it:**
   ```
   python main.py "How many courses are there in the Design category?"
   python main.py "Which category has the highest average course price?"
   ```
   You'll see a live trace of each step as it runs, and — if the question
   has genuine ambiguity — a clarifying question before generation
   proceeds. After a successful answer, you'll be asked to confirm
   whether it was actually correct; verdicts accumulate in
   `data/eval_log.jsonl`.

   Tunable via `.env`: `NODE_TIMEOUT_SECONDS`, `RUN_TIMEOUT_SECONDS`,
   `CLARIFICATION_ENABLED`, `SEMANTIC_CRITIC_ENABLED`, `MAX_SQL_RETRIES`,
   `RETRIEVAL_SIMILARITY_THRESHOLD`, `EMBEDDING_MODEL`,
   `LANGFUSE_ENABLED` (+ `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/
   `LANGFUSE_HOST` if enabling tracing — see ADR-0009).

6. **Check trajectory metrics** (once you've accumulated a few runs):
   ```
   python scripts/run_trajectory_evals.py
   ```
   Reads `data/eval_log.jsonl` and `data/escalation_queue.jsonl` — will
   report "no runs recorded yet" until you've actually run and verified
   a few questions.

## Testing

```
pytest -m "not integration"   # unit + graph + stub-mode tests, fast, no live Ollama needed
pytest -m integration         # full graph against LIVE Ollama, requires it running
```

Three tiers, only the last needs anything running locally:
- **`test_walking_skeleton.py`** — individual node logic, fully mocked.
- **`test_graph_integration.py`** — full graph routing (every
  retry/escalate/confirm/advance path), Ollama mocked at the Python
  function level, real SQL execution against the seeded test DB.
- **`test_stub_integration.py`** (ADR-0010) — full graph through the
  REAL `ollama` client's HTTP code path, against a real stdlib server
  that speaks Ollama's actual API — catches response-schema mismatches
  the mocks above can't.

All three run in CI on every push. `test_graph_integration.py` replaced
a set of one-off manual reproduction scripts used during live debugging
sessions — those caught real bugs at the time but weren't permanent
regression protection; this suite is.

## Build plan / roadmap

- [x] **Stage 1 — walking skeleton**: generate → execute → validate →
      retry/escalate loop, one question type, no memory.
- [x] **Reliability & oversight layer (ADR-0002)**: clarification node,
      live decision trace, per-node + run-level timeouts, full-context
      crash reporting, manual correctness verification log.
- [x] **Stage 2 — Planner node (ADR-0003)**: decomposes multi-part
      questions into independent sub-questions, each run through the
      existing retry loop; escalates the whole run if any step fails.
      Cross-step chaining (steps that depend on a prior step's result)
      is explicitly out of scope for now.
- [x] **Stage 3 — semantic critic (ADR-0011)**: `semantic_critic.py`
      checks whether a mechanically-valid result actually answers the
      question — wrong aggregation, wrong sort direction, wrong
      grouping — that mechanical checks can't catch by construction.
      Reuses `critic.should_retry_or_escalate` rather than duplicating
      routing logic, so a semantic failure draws from the same retry
      budget as a mechanical one. Toggleable via
      `SEMANTIC_CRITIC_ENABLED` (default on) — a real, named latency
      cost, not hidden.
- [x] **Stage 4 — real human-in-the-loop escalation (ADR-0006)**: when a
      step exhausts its retries (or a correction is rejected), the
      developer is offered a real chance to resolve it — their query
      runs through the same validation pipeline as any model-generated
      one, bounded to 2 attempts. Anything unresolved is persisted to
      `data/escalation_queue.jsonl`, not just printed to console.
- [x] **Stage 5 — correction memory (ADR-0008)**: `src/memory/correction_store.py`
      is live — Ollama embeddings (`all-minilm`) + Qdrant. Every human-
      corrected answer is stored automatically at the moment it's given
      (`src/feedback.py`), not just via a batch script. `sql_generator`
      retrieves similar past corrections before generating (first attempt
      per step only). `scripts/seed_corrections.py` bootstraps from
      `data/eval_log.jsonl` history that predates this feature. Every
      Qdrant/embedding call degrades gracefully if unavailable — never
      crashes the run.
- [x] **Stage 6 — trajectory evals + Langfuse tracing (ADR-0009)**:
      `src/tracing.py` sends one Langfuse trace per run, one span per
      node — disabled by default (`LANGFUSE_ENABLED=false`), degrades
      silently if enabled but unreachable. Separately, `src/evals.py` +
      `scripts/run_trajectory_evals.py` compute offline aggregate
      metrics (correctness rate, escalation rate, retry rate, human-
      intervention rate) from `data/eval_log.jsonl` and
      `data/escalation_queue.jsonl` — required extending both to record
      `total_attempts` and `human_intervention`, which didn't exist
      before this stage.
- [x] **Stage 7 — CI/CD (ADR-0010)**: GitHub Actions runs the full suite
      on every push/PR to `main`/`dev`. The originally-planned "stub-mode
      integration tests" piece is now built: `tests/fake_ollama_server.py`
      is a real stdlib HTTP server implementing Ollama's actual API,
      exercised through a genuine `ollama.Client` (not a Python-level
      mock) — catches response-schema mismatches a mock would hide.
      Found and confirmed via source inspection: the `ollama` client is
      built once at import time, so tests monkeypatch the client
      directly rather than relying on `OLLAMA_HOST` env var timing.

## Known limitations (documented on purpose)

- Correction memory is a cold-start mechanism — until seeded/populated
  with real resolved cases, retrieval has nothing useful to return.
- Qwen2.5-Coder-7B running on CPU will be slow (tens of seconds per
  generation) and will sometimes get multi-table joins or window
  functions wrong. This is expected and is exactly what the
  retry/escalate loop exists to catch — see ADR-0001 for the reasoning.
- The fast-path heuristic (ADR-0007) is deliberately blunt and can
  misjudge a short-but-ambiguous question as simple. Any such miss is
  still caught by the existing validation/escalation pipeline — just
  without the upfront clarifying question the LLM-based check would have
  asked. The keyword list has already been extended once after a real
  test failure (see the ADR); treat it as a living list, not a finished
  one.
- Correction memory (ADR-0008) has no mechanism to remove or down-rank a
  bad memory entry — if a human mis-judges a correction, that mistake
  gets remembered too, with no current way to un-remember it.
- For a multi-step question, `capture_human_feedback` only logs the
  *last* step's query/result to `data/eval_log.jsonl` and correction
  memory — eval_log is currently one record per run, not per step.
- Trajectory metrics (`scripts/run_trajectory_evals.py`) have the same
  cold-start problem as correction memory — a handful of manual test
  runs won't produce statistically meaningful rates, and `total_attempts`/
  `human_intervention` fields only exist on records created after ADR-0009
  — older `eval_log.jsonl`/`escalation_queue.jsonl` entries won't have
  them and are counted as `total_attempts: None` (excluded from the
  retry-rate calculation, not treated as zero).
- The semantic critic (ADR-0011) is a second opinion from the same
  7B-class model doing the generation — not a ground-truth oracle. It
  catches a different class of error than mechanical checks (wrong
  aggregation/direction/grouping), but its own judgment can itself be
  wrong. `data/eval_log.jsonl`'s human verdict remains the only actual
  ground truth in this system.
