"""
End-to-end tests of the FULL graph — every routing path, not individual
nodes in isolation. Ollama is mocked (so these run fast, deterministically,
and in CI without a live model), but SQL execution runs for real against
the seeded test database, so these are genuine integration tests of the
routing logic, not just unit tests wearing a bigger hat.

This replaces the one-off repro_*.py scripts written during live
debugging sessions (identified as a real coverage gap in project review:
routing complexity — retry/escalate/confirm/advance/multi-step — had no
permanent automated test beyond individual node unit tests plus one
trivial @pytest.mark.integration test requiring live Ollama).

Requires the seeded database (same as the rest of the suite):
    python scripts/seed_db.py
"""

from unittest.mock import patch

from src.graph import build_graph

CLEAR = {"message": {"content": "CLEAR"}}


def make_fake_chat(plan_text: str, sql_by_call: list, clarity_response: dict = CLEAR):
    """
    Builds a fake ollama.chat() that routes based on which system prompt
    is being used (clarification / planner / sql generation), and returns
    successive canned SQL responses in order for generation calls.

    sql_by_call: list of SQL strings (or exceptions) returned in sequence
    for each sql_generator invocation, regardless of which step/attempt
    it's for — tests control ordering by knowing the expected call
    sequence for their scenario.
    """
    state = {"n": 0}

    def fake_chat(model, messages, options=None):
        system = messages[0]["content"].lower()
        if "break a business question" in system:
            return {"message": {"content": plan_text}}
        if "check whether a business question" in system:
            return clarity_response

        # sql generation call
        response = sql_by_call[state["n"]]
        state["n"] += 1
        return {"message": {"content": response}}

    return fake_chat


def run_graph(question: str, fake_chat, input_responses=None):
    """Invokes the full graph with Ollama and input() both mocked."""
    input_responses = input_responses or []
    with patch("ollama.chat", side_effect=fake_chat), patch(
        "builtins.input", side_effect=input_responses
    ):
        graph = build_graph()
        return graph.invoke({"question": question, "generation_attempt": 0})


# ---- Scenario 1: simple single-step happy path -------------------------

def test_single_step_question_answers_correctly():
    fake_chat = make_fake_chat(
        plan_text="- How many courses are in the Design category?",
        sql_by_call=[
            "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'"
        ],
    )
    final_state = run_graph("How many Design courses are there?", fake_chat)

    assert not final_state.get("escalated")
    assert len(final_state["step_results"]) == 1
    assert final_state["step_results"][0]["sql_result"][0]["COUNT(*)"] == 1189


# ---- Scenario 2: multi-step plan, both steps succeed --------------------

def test_multi_step_plan_completes_both_steps():
    fake_chat = make_fake_chat(
        plan_text=(
            "- How many courses are in the Design category?\n"
            "- How many courses are in the Health category?"
        ),
        sql_by_call=[
            "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'",
            "SELECT COUNT(*) FROM course_listings WHERE category = 'Health'",
        ],
    )
    final_state = run_graph(
        "Compare Design and Health course counts", fake_chat)

    assert not final_state.get("escalated")
    assert len(final_state["step_results"]) == 2
    assert final_state["step_results"][0]["sql_result"][0]["COUNT(*)"] == 1189
    assert final_state["step_results"][1]["sql_result"][0]["COUNT(*)"] == 1209


# ---- Scenario 3: execution error, retry succeeds -------------------------

def test_execution_error_then_successful_retry():
    """
    Note: a retry that resolves a detected table/column error triggers a
    confirmation prompt (ADR-0004) even when the fix came from the LLM
    retry rather than the mechanical validator — confirmed correct
    behavior, discovered while writing this test. Hence input_responses=["y"].
    """
    fake_chat = make_fake_chat(
        plan_text="- How many courses are in the Design category?",
        sql_by_call=[
            "SELECT COUNT(*) FROM nonexistent_table WHERE category = 'Design'",
            "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'",
        ],
    )
    final_state = run_graph(
        "How many Design courses?", fake_chat, input_responses=["y"]
    )

    assert not final_state.get("escalated")
    assert final_state["step_results"][0]["sql_result"][0]["COUNT(*)"] == 1189


# ---- Scenario 4: retries exhausted, single step escalates ----------------

def test_retries_exhausted_escalates():
    fake_chat = make_fake_chat(
        plan_text="- How many courses are in the Design category?",
        sql_by_call=[
            # Same genuinely broken query every time — nothing for the
            # mechanical validator to fix (unknown, non-fuzzy-matchable
            # table), so every attempt fails identically until retries run out.
            "SELECT COUNT(*) FROM zzz_totally_unknown_table_zzz",
            "SELECT COUNT(*) FROM zzz_totally_unknown_table_zzz",
            "SELECT COUNT(*) FROM zzz_totally_unknown_table_zzz",
        ],
    )
    final_state = run_graph("How many Design courses?", fake_chat)

    assert final_state.get("escalated") is True
    assert "step 1 of 1" in final_state["final_answer"]


# ---- Scenario 5: multi-step plan, second step fails -> whole run escalates,
#      but the first (successful) step is still reported -----------------

def test_multi_step_one_step_fails_escalates_whole_run_with_partial_context():
    fake_chat = make_fake_chat(
        plan_text=(
            "- How many courses are in the Design category?\n"
            "- How many courses are in the Atlantis category?"
        ),
        sql_by_call=[
            "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'",
            "SELECT COUNT(*) FROM zzz_totally_unknown_table_zzz",
            "SELECT COUNT(*) FROM zzz_totally_unknown_table_zzz",
            "SELECT COUNT(*) FROM zzz_totally_unknown_table_zzz",
        ],
    )
    final_state = run_graph(
        "Compare Design and Atlantis course counts", fake_chat
    )

    assert final_state.get("escalated") is True
    assert "step 2 of 2" in final_state["final_answer"]
    # step 1 completed before failure
    assert len(final_state["step_results"]) == 1
    assert "1189" in final_state["final_answer"] or "Design" in final_state["final_answer"]


# ---- Scenario 6: mechanical correction gets confirmed --------------------

def test_identifier_correction_confirmed_proceeds_to_answer():
    fake_chat = make_fake_chat(
        plan_text="- How many courses are in the Design category?",
        sql_by_call=[
            # hallucinated table — identifier_validator should catch and
            # fix this on attempt 1, no retry needed at all
            "SELECT COUNT(*) FROM Courses WHERE category = 'Design'",
        ],
    )
    final_state = run_graph(
        "How many Design courses?", fake_chat, input_responses=["y"]
    )

    assert not final_state.get("escalated")
    assert final_state["assumptions"][0]["confirmed_by_developer"] is True
    assert "Assumptions made during this run" in final_state["final_answer"]


# ---- Scenario 7: mechanical correction gets rejected -> escalates --------

def test_identifier_correction_rejected_escalates():
    fake_chat = make_fake_chat(
        plan_text="- How many courses are in the Design category?",
        sql_by_call=[
            "SELECT COUNT(*) FROM Courses WHERE category = 'Design'",
        ],
    )
    final_state = run_graph(
        "How many Design courses?", fake_chat, input_responses=["n"]
    )

    assert final_state.get("escalated") is True
    assert final_state["assumptions"][0]["confirmed_by_developer"] is False


# ---- Scenario 8: clarification asked and answered, question augmented ---

def test_clarification_answered_augments_question_and_proceeds():
    ambiguous_clarity = {
        "message": {"content": "CLARIFY: Which time range do you mean?"}
    }
    fake_chat = make_fake_chat(
        plan_text="- How many courses were added recently?",
        sql_by_call=[
            "SELECT COUNT(*) FROM course_listings WHERE listing_date >= date('now', '-30 days')"
        ],
        clarity_response=ambiguous_clarity,
    )
    final_state = run_graph(
        "How many courses were added recently?",
        fake_chat,
        input_responses=["the last 30 days"],
    )

    assert not final_state.get("escalated")
    assert final_state["clarification_needed"] is True
    assert final_state["clarification_answer"] == "the last 30 days"
    assert "the last 30 days" in final_state["question"]


# ---- Scenario 9: clarification asked, developer skips, still proceeds ---

def test_clarification_skipped_logs_assumption_and_proceeds():
    ambiguous_clarity = {
        "message": {"content": "CLARIFY: Which time range do you mean?"}
    }
    fake_chat = make_fake_chat(
        plan_text="- How many courses were added recently?",
        sql_by_call=[
            "SELECT COUNT(*) FROM course_listings WHERE listing_date >= date('now', '-30 days')"
        ],
        clarity_response=ambiguous_clarity,
    )
    final_state = run_graph(
        "How many courses were added recently?",
        fake_chat,
        input_responses=[""],  # developer presses Enter without answering
    )

    assert not final_state.get("escalated")
    assert final_state["clarification_answer"] is None
    assumption_descriptions = [
        a.get("description", "") for a in final_state["assumptions"]
    ]
    assert any("did not answer" in d for d in assumption_descriptions)
