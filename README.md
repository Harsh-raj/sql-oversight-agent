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

## Current stage: multi-step planning + reliability/oversight layer

```
schema_tool -> clarification -> planner -> sql_generator -> sql_executor -> critic
                                                 ^                              |
                                                 |--------- retry (bounded) -----|
                                                                                 |
                                          escalate <-------- (any step fails) --+
                                              |                                 |
                                             END                          advance_step
                                                                                 |
                                              more steps remain? --------- yes --+
                                                     |
                                                     no
                                                     |
                                                  respond
```

The **Planner** breaks a question into an ordered list of independent
sub-questions (e.g. "compare X and Y" → two separate single-query steps).
Each step in the plan runs through generate → **validate identifiers
mechanically** → execute → validate result. Before a query ever touches
the database, `identifier_validator` checks its table/column references
against the real schema and fuzzy-match-corrects an obvious hallucination
(e.g. a model-generated `Courses` → the real `course_listings`) —
deterministically, not by hoping the model reads a correction prompt
correctly (see ADR-0005: this was added after prompt-based self-correction
failed twice in live testing). Any correction — mechanical or
retry-based — pauses for developer confirmation before being trusted;
rejecting it escalates immediately, same as exhausting retries. Every
assumption made during a run is listed explicitly in the final answer.
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

No correction memory or multi-turn conversation yet — those are later
stages (see below).

## Setup

1. **Install Ollama** natively (not in Docker — better CPU performance):
   https://ollama.com
   ```
   ollama pull qwen2.5-coder:7b
   ```

2. **Start Qdrant** (not used by the walking skeleton yet, but needed from
   stage 5 onward — starting it now so `docker-compose` is one command):
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
   `CLARIFICATION_ENABLED`, `MAX_SQL_RETRIES`.

## Testing

```
pytest -m "not integration"   # unit + end-to-end graph tests, fast, no Ollama needed
pytest -m integration         # full graph against LIVE Ollama, requires it running
```

The unit/end-to-end suite (`test_walking_skeleton.py` for individual
node logic, `test_graph_integration.py` for full graph routing — every
retry/escalate/confirm/advance path, mocked Ollama but real SQL execution
against the seeded test DB) runs in CI on every push. It replaced a set
of one-off manual reproduction scripts used during live debugging
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
- [ ] **Stage 3 — richer Critic**: model-based "does this answer the
      question" check per step, not just mechanical error/empty-result
      checks. Also where cross-step chaining would eventually be
      revisited, if needed.
- [ ] **Stage 4 — real human-in-the-loop for escalation**: currently the
      clarification step blocks for real input, but escalation after
      retries are exhausted still just returns immediately rather than
      blocking on a review queue — that's the remaining piece here.
- [ ] **Stage 5 — correction memory**: wire up `src/memory/correction_store.py`
      (Qdrant), seed with `data/seed_corrections.json`, inject retrieved
      corrections into the generation prompt.
- [ ] **Stage 6 — trajectory evals + Langfuse tracing**: track retry
      rate, escalation rate, and correctness against a labeled question
      set — not just whether the final answer looked right.
- [ ] **Stage 7 — CI/CD**: GitHub Actions running the unit tests (and
      stub-mode integration tests) on every commit.

## Known limitations (documented on purpose)

- Correction memory is a cold-start mechanism — until seeded/populated
  with real resolved cases, retrieval has nothing useful to return.
- Qwen2.5-Coder-7B running on CPU will be slow (tens of seconds per
  generation) and will sometimes get multi-table joins or window
  functions wrong. This is expected and is exactly what the
  retry/escalate loop exists to catch — see ADR-0001 for the reasoning.
