"""
Two tiers of test here, matching the testing-pyramid approach used on
other projects:

1. Unit tests: each node tested in isolation with mocked dependencies —
   no Ollama, no DB required, fast, run in CI on every commit.
2. Integration test: the full graph against the real seeded SQLite DB
   and a real Ollama call — slower, requires local setup, marked so it
   can be skipped in CI environments without Ollama available.
"""

import json
import time
from unittest.mock import patch

import pytest

from src.feedback import append_eval_record, capture_human_feedback
from src.nodes.critic import should_retry_or_escalate, validate_result
from src.nodes.sql_executor import execute_sql
from src.observability import NodeTimeoutError, timed_node


# ---- Unit tests: critic ----------------------------------------------

def test_critic_flags_sql_error_as_invalid():
    state = {"sql_error": "no such column: foo", "sql_result": None}
    result = validate_result(state)
    assert result["is_valid"] is False
    assert "foo" in result["validation_reason"]


def test_critic_flags_empty_result_as_invalid():
    state = {"sql_error": None, "sql_result": []}
    result = validate_result(state)
    assert result["is_valid"] is False


def test_critic_accepts_valid_nonempty_result():
    state = {"sql_error": None, "sql_result": [{"count": 5}]}
    result = validate_result(state)
    assert result["is_valid"] is True


def test_routing_retries_within_limit():
    state = {"is_valid": False, "generation_attempt": 1}
    assert should_retry_or_escalate(state) == "retry"


def test_routing_escalates_after_max_retries():
    state = {"is_valid": False, "generation_attempt": 3}
    assert should_retry_or_escalate(state) == "escalate"


def test_routing_advances_when_valid_and_no_correction_pending():
    state = {"is_valid": True, "generation_attempt": 0, "pending_correction": None}
    assert should_retry_or_escalate(state) == "advance"


def test_routing_confirms_when_valid_with_pending_correction():
    state = {
        "is_valid": True,
        "generation_attempt": 1,
        "pending_correction": {"error_type": "table", "original_identifier": "courses"},
    }
    assert should_retry_or_escalate(state) == "confirm"


# ---- Unit tests: sql_executor guardrail --------------------------------

def test_executor_rejects_write_queries():
    state = {"sql_query": "DELETE FROM course_listings WHERE 1=1"}
    result = execute_sql(state)
    assert result["sql_result"] is None
    assert "read-only" in result["sql_error"]


# ---- Unit tests: clarification node ------------------------------------

def test_clarity_check_proceeds_when_clear():
    from src.nodes.clarification import check_clarity

    with patch(
        "src.nodes.clarification._check_ambiguity", return_value="CLEAR"
    ):
        state = {"question": "How many courses in Design?", "schema_context": "..."}
        result = check_clarity(state)

    assert result["clarification_needed"] is False


def test_clarity_check_asks_and_augments_question_when_ambiguous():
    from src.nodes.clarification import check_clarity

    with patch(
        "src.nodes.clarification._check_ambiguity",
        return_value="CLARIFY: Do you mean by enrollment or by price?",
    ), patch("builtins.input", return_value="By enrollment"):
        state = {"question": "Which category is most popular?", "schema_context": "..."}
        result = check_clarity(state)

    assert result["clarification_needed"] is True
    assert result["clarification_answer"] == "By enrollment"
    assert "By enrollment" in result["question"]


def test_clarity_check_proceeds_without_answer_if_developer_skips():
    from src.nodes.clarification import check_clarity

    with patch(
        "src.nodes.clarification._check_ambiguity",
        return_value="CLARIFY: Which time range?",
    ), patch("builtins.input", return_value=""):
        state = {"question": "Recent trend in enrollments?", "schema_context": "..."}
        result = check_clarity(state)

    assert result["clarification_needed"] is True
    assert result["clarification_answer"] is None
    # question left unmodified when developer doesn't answer
    assert result["question"] == "Recent trend in enrollments?"


def test_clarity_check_disabled_via_settings():
    from src.nodes import clarification as clarification_module

    with patch.object(
        clarification_module.settings, "clarification_enabled", False
    ):
        result = clarification_module.check_clarity(
            {"question": "anything", "schema_context": "..."}
        )

    assert result["clarification_needed"] is False


# ---- Unit tests: observability / timeout wrapper -----------------------

def test_timed_node_passes_through_result_without_timeout():
    @timed_node("dummy")
    def fast_node(state):
        return {**state, "done": True}

    result = fast_node({"x": 1})
    assert result == {"x": 1, "done": True}


def test_timed_node_raises_on_timeout():
    @timed_node("slow", timeout_seconds=0.1)
    def slow_node(state):
        time.sleep(1)
        return state

    with pytest.raises(NodeTimeoutError):
        slow_node({})


# ---- Unit tests: feedback / eval logging --------------------------------

def test_append_eval_record_writes_expected_jsonl(tmp_path):
    log_path = tmp_path / "eval_log.jsonl"
    with patch("src.feedback.EVAL_LOG_PATH", log_path):
        append_eval_record(
            question="How many Design courses?",
            sql_query="SELECT COUNT(*) FROM course_listings WHERE category='Design'",
            sql_result=[{"count": 1189}],
            is_correct=True,
        )

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["human_verdict"] == "correct"
    assert record["question"] == "How many Design courses?"


def test_capture_human_feedback_skips_when_escalated():
    result = capture_human_feedback({"escalated": True, "question": "x"})
    assert result is None


def test_capture_human_feedback_logs_incorrect_with_correction(tmp_path):
    log_path = tmp_path / "eval_log.jsonl"
    with patch("src.feedback.EVAL_LOG_PATH", log_path), patch(
        "builtins.input", side_effect=["n", "Should filter by status='published'"]
    ):
        record = capture_human_feedback(
            {
                "question": "How many active Design courses?",
                "sql_query": "SELECT COUNT(*) FROM course_listings WHERE category='Design'",
                "sql_result": [{"count": 1189}],
            }
        )

    assert record["human_verdict"] == "incorrect"
    assert "published" in record["correction"]


# ---- Unit tests: planner (ADR-0003) -------------------------------------

def test_planner_parses_multiple_steps():
    from src.nodes.planner import create_plan

    raw = "- How many courses are in Design?\n- How many courses are in Health?"
    with patch("src.nodes.planner._generate_plan_text", return_value=raw):
        state = {"question": "Compare Design and Health course counts", "schema_context": "..."}
        result = create_plan(state)

    assert result["plan"] == [
        "How many courses are in Design?",
        "How many courses are in Health?",
    ]
    assert result["current_step_index"] == 0
    assert result["step_results"] == []
    assert result["plan_complete"] is False


def test_planner_single_step_question_produces_one_item_plan():
    from src.nodes.planner import create_plan

    with patch(
        "src.nodes.planner._generate_plan_text",
        return_value="- How many courses are in Design?",
    ):
        state = {"question": "How many courses are in Design?", "schema_context": "..."}
        result = create_plan(state)

    assert len(result["plan"]) == 1


def test_planner_falls_back_to_single_step_on_malformed_output():
    from src.nodes.planner import create_plan

    with patch(
        "src.nodes.planner._generate_plan_text",
        return_value="I think this question is about courses.",
    ):
        state = {"question": "How many courses are in Design?", "schema_context": "..."}
        result = create_plan(state)

    assert result["plan"] == ["How many courses are in Design?"]


# ---- Unit tests: advance_step (ADR-0003) --------------------------------

def test_advance_step_moves_to_next_step_and_resets_scratch_state():
    from src.nodes.advance_step import advance_step

    state = {
        "plan": ["step one", "step two"],
        "current_step_index": 0,
        "step_results": [],
        "sql_query": "SELECT 1",
        "sql_result": [{"1": 1}],
        "generation_attempt": 2,
    }
    result = advance_step(state)

    assert result["current_step_index"] == 1
    assert result["plan_complete"] is False
    assert len(result["step_results"]) == 1
    assert result["step_results"][0]["sub_question"] == "step one"
    assert result["generation_attempt"] == 0
    assert result["sql_query"] is None


def test_advance_step_marks_plan_complete_on_last_step():
    from src.nodes.advance_step import advance_step

    state = {
        "plan": ["only step"],
        "current_step_index": 0,
        "step_results": [],
        "sql_query": "SELECT 1",
        "sql_result": [{"1": 1}],
        "generation_attempt": 1,
    }
    result = advance_step(state)

    assert result["plan_complete"] is True
    assert result["current_step_index"] == 1


def test_route_after_advance_goes_to_respond_when_complete():
    from src.nodes.advance_step import route_after_advance

    assert route_after_advance({"plan_complete": True}) == "respond"


def test_route_after_advance_goes_back_to_generator_when_steps_remain():
    from src.nodes.advance_step import route_after_advance

    assert route_after_advance({"plan_complete": False}) == "sql_generator"


# ---- Unit tests: sql_generator hallucination fixes ----------------------

def test_extract_table_names_parses_schema_format():
    from src.nodes.sql_generator import _extract_table_names

    schema = "Table `course_listings`: id (INTEGER), category (TEXT)"
    assert _extract_table_names(schema) == ["course_listings"]


def test_extract_table_names_handles_multiple_tables():
    from src.nodes.sql_generator import _extract_table_names

    schema = "Table `a`: x (INTEGER)\nTable `b`: y (TEXT)"
    assert _extract_table_names(schema) == ["a", "b"]


def test_retry_feedback_names_the_bad_table_explicitly():
    from src.nodes.sql_generator import _build_retry_feedback

    feedback = _build_retry_feedback(
        "no such table: courses", "SELECT * FROM courses"
    )
    assert "'courses'" in feedback
    assert "does NOT exist" in feedback


def test_retry_feedback_names_the_bad_column_explicitly():
    from src.nodes.sql_generator import _build_retry_feedback

    feedback = _build_retry_feedback(
        "no such column: genre", "SELECT genre FROM course_listings"
    )
    assert "'genre'" in feedback


def test_retry_feedback_falls_back_to_generic_for_unknown_errors():
    from src.nodes.sql_generator import _build_retry_feedback

    feedback = _build_retry_feedback("syntax error near SELECT", "SELECT * FRO x")
    assert "syntax error near SELECT" in feedback


def test_generate_sql_includes_explicit_table_list_and_bumps_temperature_on_retry():
    from src.nodes.sql_generator import generate_sql

    captured = {}

    def fake_chat(model, messages, options=None):
        captured["messages"] = messages
        captured["options"] = options
        return {"message": {"content": "SELECT COUNT(*) FROM course_listings"}}

    with patch("ollama.chat", side_effect=fake_chat):
        state = {
            "plan": ["How many Design courses?"],
            "current_step_index": 0,
            "schema_context": "Table `course_listings`: category (TEXT)",
            "question": "How many Design courses?",
            "generation_attempt": 1,  # this is a retry
            "sql_error": "no such table: courses",
            "sql_query": "SELECT * FROM courses",
        }
        generate_sql(state)

    user_message = captured["messages"][1]["content"]
    assert "Valid table names" in user_message
    assert "course_listings" in user_message
    assert "'courses'" in user_message  # targeted retry correction present
    assert captured["options"]["temperature"] > 0.2  # bumped above base


def test_generate_sql_uses_base_temperature_on_first_attempt():
    from src.nodes.sql_generator import generate_sql

    captured = {}

    def fake_chat(model, messages, options=None):
        captured["options"] = options
        return {"message": {"content": "SELECT COUNT(*) FROM course_listings"}}

    with patch("ollama.chat", side_effect=fake_chat):
        state = {
            "plan": ["How many Design courses?"],
            "current_step_index": 0,
            "schema_context": "Table `course_listings`: category (TEXT)",
            "question": "How many Design courses?",
            "generation_attempt": 0,
        }
        generate_sql(state)

    assert captured["options"]["temperature"] == 0.2


# ---- Unit tests: confirm_correction (ADR-0004) --------------------------

def test_confirm_correction_accepts_when_developer_confirms():
    from src.nodes.confirm_correction import confirm_correction

    state = {
        "pending_correction": {
            "error_type": "table",
            "original_identifier": "courses",
            "original_query": "SELECT * FROM courses",
            "step_question": "How many Design courses?",
        },
        "sql_query": "SELECT * FROM course_listings",
        "assumptions": [],
        "is_valid": True,  # confirm_correction only runs when already valid
    }
    with patch("builtins.input", return_value="y"):
        result = confirm_correction(state)

    assert result["is_valid"] is True  # unchanged
    assert result["pending_correction"] is None
    assert result["assumptions"][0]["confirmed_by_developer"] is True


def test_confirm_correction_rejects_and_forces_invalid():
    from src.nodes.confirm_correction import confirm_correction

    state = {
        "pending_correction": {
            "error_type": "table",
            "original_identifier": "courses",
            "original_query": "SELECT * FROM courses",
            "step_question": "How many Design courses?",
        },
        "sql_query": "SELECT * FROM course_listings",
        "assumptions": [],
        "is_valid": True,
    }
    with patch("builtins.input", return_value="n"):
        result = confirm_correction(state)

    assert result["is_valid"] is False
    assert "rejected" in result["validation_reason"].lower()
    assert result["assumptions"][0]["confirmed_by_developer"] is False


def test_route_after_confirmation_advances_when_confirmed():
    from src.nodes.confirm_correction import route_after_confirmation

    assert route_after_confirmation({"is_valid": True}) == "advance_step"


def test_route_after_confirmation_escalates_when_rejected():
    from src.nodes.confirm_correction import route_after_confirmation

    assert route_after_confirmation({"is_valid": False}) == "escalate"


# ---- Unit tests: assumption surfacing in the final answer (ADR-0004) ----

def test_respond_surfaces_confirmed_correction():
    from src.nodes.responder import respond

    state = {
        "question": "How many Design courses?",
        "step_results": [
            {
                "sub_question": "How many Design courses?",
                "sql_query": "SELECT COUNT(*) FROM course_listings",
                "sql_result": [{"count": 1189}],
            }
        ],
        "assumptions": [
            {
                "step": "How many Design courses?",
                "type": "identifier_correction",
                "original_identifier": "courses",
                "confirmed_by_developer": True,
            }
        ],
    }
    result = respond(state)
    assert "Assumptions made during this run" in result["final_answer"]
    assert "confirmed" in result["final_answer"]
    assert "courses" in result["final_answer"]


def test_respond_omits_assumptions_section_when_none_made():
    from src.nodes.responder import respond

    state = {
        "question": "How many Design courses?",
        "step_results": [
            {
                "sub_question": "How many Design courses?",
                "sql_query": "SELECT COUNT(*) FROM course_listings",
                "sql_result": [{"count": 1189}],
            }
        ],
        "assumptions": [],
    }
    result = respond(state)
    assert "Assumptions made during this run" not in result["final_answer"]


# ---- Unit tests: schema_utils --------------------------------------------

def test_extract_all_column_names_parses_single_table():
    from src.schema_utils import extract_all_column_names

    schema = "Table `course_listings`: category (TEXT), price_usd (REAL)"
    assert extract_all_column_names(schema) == ["category", "price_usd"]


# ---- Unit tests: identifier_validator (ADR-0005) -------------------------

def test_find_best_match_returns_confident_match():
    from src.nodes.identifier_validator import _find_best_match

    assert _find_best_match("Courses", ["course_listings"]) == "course_listings"


def test_find_best_match_returns_none_below_threshold():
    from src.nodes.identifier_validator import _find_best_match

    assert _find_best_match("xyz123", ["course_listings"]) is None


def test_find_best_match_returns_none_when_ambiguous():
    from src.nodes.identifier_validator import _find_best_match

    # two very similar candidates — neither should be confidently chosen
    result = _find_best_match("studnet", ["student", "students"])
    assert result is None


def test_validate_identifiers_corrects_hallucinated_table():
    from src.nodes.identifier_validator import validate_identifiers

    schema = "Table `course_listings`: category (TEXT), price_usd (REAL)"
    state = {
        "sql_query": "SELECT COUNT(*) FROM Courses WHERE category = 'Design'",
        "schema_context": schema,
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
    }
    result = validate_identifiers(state)

    assert "course_listings" in result["sql_query"]
    assert "Courses" not in result["sql_query"]
    assert result["pending_correction"]["error_type"] == "table"
    assert result["pending_correction"]["original_identifier"] == "Courses"


def test_validate_identifiers_corrects_hallucinated_column():
    from src.nodes.identifier_validator import validate_identifiers

    schema = "Table `course_listings`: category (TEXT), price_usd (REAL)"
    state = {
        "sql_query": "SELECT COUNT(*) FROM course_listings WHERE categry = 'Design'",
        "schema_context": schema,
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
    }
    result = validate_identifiers(state)

    assert "category" in result["sql_query"]
    assert result["pending_correction"]["error_type"] == "column"


def test_validate_identifiers_leaves_correct_query_untouched():
    from src.nodes.identifier_validator import validate_identifiers

    schema = "Table `course_listings`: category (TEXT), price_usd (REAL)"
    sql = "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'"
    state = {
        "sql_query": sql,
        "schema_context": schema,
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
    }
    result = validate_identifiers(state)

    assert result.get("sql_query", sql) == sql
    assert result.get("pending_correction") is None


def test_validate_identifiers_ignores_harmless_case_difference():
    """Real bug found during manual testing: SQLite identifiers are
    case-insensitive, so a casing difference alone must not be treated
    as a hallucination requiring a confirmation prompt."""
    from src.nodes.identifier_validator import validate_identifiers

    schema = "Table `course_listings`: category (TEXT), price_usd (REAL)"
    sql = "SELECT COUNT(*) FROM course_listings WHERE Category = 'Design'"
    state = {
        "sql_query": sql,
        "schema_context": schema,
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
    }
    result = validate_identifiers(state)

    assert result.get("pending_correction") is None
    assert result.get("sql_query", sql) == sql


def test_validate_identifiers_does_not_flag_count_as_a_column():
    """Real bug found during manual testing: sqlparse tokenizes function
    names like COUNT as plain Name tokens, indistinguishable from a
    column reference by type alone — must be excluded via the
    followed-by-'(' check."""
    from src.nodes.identifier_validator import validate_identifiers

    schema = "Table `course_listings`: category (TEXT), price_usd (REAL)"
    sql = "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'"
    state = {
        "sql_query": sql,
        "schema_context": schema,
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
    }
    result = validate_identifiers(state)

    assert result.get("pending_correction") is None


# ---- Integration test (requires seeded DB + running Ollama) -----------

@pytest.mark.integration
def test_full_graph_answers_simple_question():
    from src.graph import build_graph

    graph = build_graph()
    initial_state = {
        "question": "How many courses are there in the Design category?",
        "generation_attempt": 0,
    }
    final_state = graph.invoke(initial_state)

    assert final_state.get("final_answer") is not None
    # Either it answered, or it escalated honestly — both are valid
    # outcomes for this test; what we're checking is that the graph
    # terminates cleanly either way.
    assert final_state.get("escalated") in (True, None) or final_state.get(
        "sql_result"
    ) is not None
