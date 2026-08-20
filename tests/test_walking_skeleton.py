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
from unittest.mock import MagicMock, patch

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
    state = {"is_valid": True, "generation_attempt": 0,
             "pending_correction": None}
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
        state = {"question": "How many courses in Design?",
                 "schema_context": "..."}
        result = check_clarity(state)

    assert result["clarification_needed"] is False


def test_clarity_check_asks_and_augments_question_when_ambiguous():
    from src.nodes.clarification import check_clarity

    with patch(
        "src.nodes.clarification._check_ambiguity",
        return_value="CLARIFY: Do you mean by enrollment or by price?",
    ), patch("builtins.input", return_value="By enrollment"):
        state = {"question": "Which category is most popular?",
                 "schema_context": "..."}
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
        state = {"question": "Recent trend in enrollments?",
                 "schema_context": "..."}
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
        state = {"question": "Compare Design and Health course counts",
                 "schema_context": "..."}
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
        state = {"question": "How many courses are in Design?",
                 "schema_context": "..."}
        result = create_plan(state)

    assert len(result["plan"]) == 1


def test_planner_falls_back_to_single_step_on_malformed_output():
    from src.nodes.planner import create_plan

    with patch(
        "src.nodes.planner._generate_plan_text",
        return_value="I think this question is about courses.",
    ):
        state = {"question": "How many courses are in Design?",
                 "schema_context": "..."}
        result = create_plan(state)

    assert result["plan"] == ["How many courses are in Design?"]


def test_planner_falls_back_when_step_is_actually_sql():
    """Real bug found in live testing: the model sometimes leaks the SQL
    query itself as a plan step instead of a natural-language question,
    which would otherwise show up as 'Step 1: SELECT COUNT(*) FROM...'
    in the final answer."""
    from src.nodes.planner import create_plan

    with patch(
        "src.nodes.planner._generate_plan_text",
        return_value="- SELECT COUNT(*) FROM course_listings WHERE category = 'Design';",
    ):
        state = {
            "question": "How many courses are in the Design category?",
            "schema_context": "...",
        }
        result = create_plan(state)

    assert result["plan"] == ["How many courses are in the Design category?"]


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

    feedback = _build_retry_feedback(
        "syntax error near SELECT", "SELECT * FRO x")
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

    assert route_after_confirmation({"is_valid": True}) == "semantic_critic"


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

    assert _find_best_match(
        "Courses", ["course_listings"]) == "course_listings"


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


# ---- Unit tests: human-in-the-loop escalation (ADR-0006) ----------------

def test_escalate_offers_resolution_and_captures_human_sql():
    from src.nodes.escalation import escalate_to_human

    state = {
        "question": "How many Design courses?",
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
        "step_results": [],
        "validation_reason": "Query error: no such table: courses",
        "generation_attempt": 2,
        "human_attempts": 0,
    }
    with patch(
        "builtins.input",
        side_effect=[
            "y", "SELECT COUNT(*) FROM course_listings WHERE category='Design'"],
    ):
        result = escalate_to_human(state)

    assert result["human_provided"] is True
    assert result["human_wants_to_retry"] is True
    assert result["human_attempts"] == 1
    assert "course_listings" in result["sql_query"]


def test_escalate_finalizes_and_persists_when_developer_declines(tmp_path):
    from src.nodes import escalation as escalation_module

    queue_path = tmp_path / "escalation_queue.jsonl"
    state = {
        "question": "How many Design courses?",
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
        "step_results": [],
        "validation_reason": "Query error: no such table: courses",
        "generation_attempt": 2,
        "human_attempts": 0,
    }
    with patch.object(
        escalation_module, "ESCALATION_QUEUE_PATH", queue_path
    ), patch("builtins.input", return_value="n"):
        result = escalation_module.escalate_to_human(state)

    assert result["escalated"] is True
    assert result["human_wants_to_retry"] is False
    assert queue_path.exists()
    record = json.loads(queue_path.read_text().strip().splitlines()[0])
    assert record["resolved"] is False
    assert record["question"] == "How many Design courses?"


def test_escalate_stops_after_max_human_attempts_without_asking_again(tmp_path):
    from src.nodes import escalation as escalation_module

    queue_path = tmp_path / "escalation_queue.jsonl"
    state = {
        "question": "How many Design courses?",
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
        "step_results": [],
        "validation_reason": "still failing",
        "generation_attempt": 2,
        "human_attempts": 2,  # already at MAX_HUMAN_ATTEMPTS
    }
    with patch.object(escalation_module, "ESCALATION_QUEUE_PATH", queue_path):
        # note: no input() mock provided at all — if the code tried to
        # prompt again, this test would raise StopIteration/error
        result = escalation_module.escalate_to_human(state)

    assert result["escalated"] is True


def test_route_after_escalation_goes_to_validator_when_retrying():
    from src.nodes.escalation import route_after_escalation

    assert route_after_escalation(
        {"human_wants_to_retry": True}) == "identifier_validator"


def test_route_after_escalation_ends_when_not_retrying():
    from src.nodes.escalation import route_after_escalation

    assert route_after_escalation({"human_wants_to_retry": False}) == "end"


def test_routing_sends_failed_human_query_back_to_escalate_not_retry():
    from src.nodes.critic import should_retry_or_escalate

    state = {
        "is_valid": False,
        "human_provided": True,
        "generation_attempt": 1,  # would normally still have retries left
    }
    assert should_retry_or_escalate(state) == "escalate"


def test_advance_step_tracks_human_source():
    from src.nodes.advance_step import advance_step

    state = {
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
        "step_results": [],
        "sql_query": "SELECT COUNT(*) FROM course_listings",
        "sql_result": [{"count": 1189}],
        "generation_attempt": 0,
        "human_provided": True,
    }
    result = advance_step(state)

    assert result["step_results"][0]["source"] == "human"
    assert result["human_provided"] is False  # reset for next step


def test_respond_labels_human_provided_steps():
    from src.nodes.responder import respond

    state = {
        "question": "How many Design courses?",
        "step_results": [
            {
                "sub_question": "How many Design courses?",
                "sql_query": "SELECT COUNT(*) FROM course_listings",
                "sql_result": [{"count": 1189}],
                "source": "human",
            }
        ],
        "assumptions": [],
    }
    result = respond(state)
    assert "(human-provided query)" in result["final_answer"]


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


# ---- Unit tests: fast_path heuristic gate (ADR-0007) ---------------------

def test_fast_path_fires_for_short_simple_question():
    from src.nodes.fast_path import check_fast_path

    state = {"question": "How many Design courses are there?"}
    result = check_fast_path(state)

    assert result["fast_path"] is True
    assert result["plan"] == ["How many Design courses are there?"]
    assert result["clarification_needed"] is False


def test_fast_path_skips_for_comparison_question():
    from src.nodes.fast_path import check_fast_path

    state = {"question": "Compare Design and Health course counts"}
    result = check_fast_path(state)

    assert result["fast_path"] is False
    assert "plan" not in result


def test_fast_path_skips_for_long_question():
    from src.nodes.fast_path import check_fast_path

    long_question = " ".join(["word"] * 20) + "?"
    result = check_fast_path({"question": long_question})

    assert result["fast_path"] is False


def test_fast_path_skips_for_multiple_question_marks():
    from src.nodes.fast_path import check_fast_path

    state = {"question": "How many Design courses? What about Health?"}
    result = check_fast_path(state)

    assert result["fast_path"] is False


def test_fast_path_skips_for_vague_temporal_language():
    """Real bug found via testing: a short question containing vague
    language ('recently') was originally fast-pathed despite being
    exactly the kind of ambiguity ADR-0002's clarification node exists
    to catch — this would have silently bypassed that safety net."""
    from src.nodes.fast_path import check_fast_path

    result = check_fast_path(
        {"question": "How many courses were added recently?"}
    )
    assert result["fast_path"] is False


def test_route_after_fast_path_check_goes_direct_when_fast():
    from src.nodes.fast_path import route_after_fast_path_check

    assert route_after_fast_path_check({"fast_path": True}) == "sql_generator"


def test_route_after_fast_path_check_goes_to_clarification_when_not_fast():
    from src.nodes.fast_path import route_after_fast_path_check

    assert route_after_fast_path_check({"fast_path": False}) == "clarification"


# ---- Unit tests: correction memory (ADR-0008) ----------------------------

def test_correction_store_degrades_gracefully_when_qdrant_unavailable():
    from src.memory.correction_store import CorrectionStore

    with patch("src.memory.correction_store.QdrantClient", side_effect=Exception("refused")):
        store = CorrectionStore()

    assert store.client is None
    assert store.retrieve_similar("any question") == []
    assert store.store_correction("q", "sql", None, "correction") is False


def test_correction_store_retrieve_similar_formats_results():
    from src.memory.correction_store import CorrectionStore

    store = CorrectionStore.__new__(CorrectionStore)  # bypass __init__
    store.collection = "test_collection"
    store.client = MagicMock()
    store.client.get_collections.return_value = MagicMock(
        collections=[MagicMock(name="test_collection")]
    )

    fake_hit = MagicMock()
    fake_hit.score = 0.92
    fake_hit.payload = {
        "question": "How many Design courses?",
        "attempted_sql": "SELECT * FROM courses",
        "error": None,
        "human_correction": "Use course_listings, not courses",
    }
    store.client.search.return_value = [fake_hit]

    with patch("ollama.embeddings", return_value={"embedding": [0.1] * 384}):
        results = store.retrieve_similar("How many Design courses are there?")

    assert len(results) == 1
    assert results[0]["score"] == 0.92
    assert results[0]["human_correction"] == "Use course_listings, not courses"


def test_correction_store_store_correction_upserts_point():
    from src.memory.correction_store import CorrectionStore

    store = CorrectionStore.__new__(CorrectionStore)
    store.collection = "test_collection"
    store.client = MagicMock()
    store.client.get_collections.return_value = MagicMock(collections=[])

    with patch("ollama.embeddings", return_value={"embedding": [0.1] * 384}):
        result = store.store_correction(
            question="How many Design courses?",
            attempted_sql="SELECT * FROM courses",
            error="no such table: courses",
            human_correction="Use course_listings",
        )

    assert result is True
    store.client.upsert.assert_called_once()


# ---- Unit tests: memory wiring in sql_generator (ADR-0008) ---------------

def test_format_memory_context_includes_results_above_threshold():
    from src.nodes.sql_generator import _format_memory_context
    from src.config import settings

    similar = [
        {
            "score": settings.retrieval_similarity_threshold + 0.1,
            "question": "How many Design courses?",
            "attempted_sql": "SELECT * FROM courses",
            "human_correction": "Use course_listings",
        }
    ]
    context = _format_memory_context(similar)
    assert "Use course_listings" in context


def test_format_memory_context_excludes_low_similarity_results():
    from src.nodes.sql_generator import _format_memory_context
    from src.config import settings

    similar = [
        {
            "score": settings.retrieval_similarity_threshold - 0.3,
            "question": "unrelated question",
            "attempted_sql": None,
            "human_correction": "irrelevant correction",
        }
    ]
    context = _format_memory_context(similar)
    assert context == ""


def test_generate_sql_queries_memory_only_on_first_attempt():
    from src.nodes.sql_generator import generate_sql

    with patch("ollama.chat", return_value={"message": {"content": "SELECT 1"}}), \
            patch("src.nodes.sql_generator.CorrectionStore") as MockStore:
        MockStore.return_value.retrieve_similar.return_value = []

        state_first_attempt = {
            "plan": ["q"], "current_step_index": 0,
            "schema_context": "Table `t`: c (TEXT)",
            "question": "q", "generation_attempt": 0,
        }
        generate_sql(state_first_attempt)
        assert MockStore.return_value.retrieve_similar.called

        MockStore.reset_mock()
        state_retry = {
            "plan": ["q"], "current_step_index": 0,
            "schema_context": "Table `t`: c (TEXT)",
            "question": "q", "generation_attempt": 1,
            "sql_error": "some error", "sql_query": "SELECT bad",
        }
        generate_sql(state_retry)
        assert not MockStore.return_value.retrieve_similar.called


# ---- Unit tests: feedback wiring + the sql_query/sql_result bug fix -----

def test_capture_human_feedback_reads_from_step_results_not_stale_top_level(tmp_path):
    """Regression test for a real bug found while wiring memory: advance_step
    resets top-level sql_query/sql_result to None even on the last step, so
    capture_human_feedback must read from step_results instead."""
    from src.feedback import capture_human_feedback

    log_path = tmp_path / "eval_log.jsonl"
    state = {
        "question": "How many Design courses?",
        "sql_query": None,  # stale, reset by advance_step — must NOT be used
        "sql_result": None,
        "step_results": [
            {
                "sub_question": "How many Design courses?",
                "sql_query": "SELECT COUNT(*) FROM course_listings WHERE category='Design'",
                "sql_result": [{"count": 1189}],
            }
        ],
    }
    with patch("src.feedback.EVAL_LOG_PATH", log_path), patch(
        "builtins.input", return_value="y"
    ):
        record = capture_human_feedback(state)

    assert "course_listings" in record["sql_query"]
    assert record["sql_result"] == [{"count": 1189}]


def test_capture_human_feedback_stores_correction_in_memory_when_incorrect(tmp_path):
    from src.feedback import capture_human_feedback

    log_path = tmp_path / "eval_log.jsonl"
    state = {
        "question": "How many Design courses?",
        "step_results": [
            {
                "sub_question": "q",
                "sql_query": "SELECT * FROM courses",
                "sql_result": None,
            }
        ],
    }
    with patch("src.feedback.EVAL_LOG_PATH", log_path), patch(
        "builtins.input", side_effect=["n", "Use course_listings instead"]
    ), patch("src.feedback.CorrectionStore") as MockStore:
        capture_human_feedback(state)

    MockStore.return_value.store_correction.assert_called_once_with(
        question="How many Design courses?",
        attempted_sql="SELECT * FROM courses",
        error=None,
        human_correction="Use course_listings instead",
    )


def test_capture_human_feedback_does_not_store_memory_when_correct(tmp_path):
    from src.feedback import capture_human_feedback

    log_path = tmp_path / "eval_log.jsonl"
    state = {
        "question": "How many Design courses?",
        "step_results": [{"sub_question": "q", "sql_query": "SELECT 1", "sql_result": [{"c": 1}]}],
    }
    with patch("src.feedback.EVAL_LOG_PATH", log_path), patch(
        "builtins.input", return_value="y"
    ), patch("src.feedback.CorrectionStore") as MockStore:
        capture_human_feedback(state)

    MockStore.return_value.store_correction.assert_not_called()


# ---- Unit tests: tracing (ADR-0009) ---------------------------------------

def test_tracing_disabled_by_default_returns_none():
    from src import tracing

    with patch.object(tracing.settings, "langfuse_enabled", False):
        tracing._client = None
        tracing._client_init_attempted = False
        trace_id = tracing.create_run_trace_id("some-run-id")

    assert trace_id is None


def test_tracing_start_span_returns_none_when_trace_id_is_none():
    from src import tracing

    span = tracing.start_span(None, "some_node", {"question": "x"})
    assert span is None


def test_tracing_end_span_handles_none_span_without_error():
    from src import tracing

    tracing.end_span(None, {"result": "ok"})  # must not raise


def test_tracing_degrades_gracefully_when_client_init_fails():
    from src import tracing

    tracing._client = None
    tracing._client_init_attempted = False
    with patch.object(tracing.settings, "langfuse_enabled", True), patch(
        "langfuse.Langfuse", side_effect=Exception("connection refused")
    ):
        trace_id = tracing.create_run_trace_id("some-run-id")

    assert trace_id is None
    tracing._client = None
    tracing._client_init_attempted = False


def test_tracing_flush_never_raises_when_disabled():
    from src import tracing

    with patch.object(tracing.settings, "langfuse_enabled", False):
        tracing.flush()  # must not raise


def test_end_span_uses_update_then_end_not_end_with_output_kwarg():
    """Real bug found via live testing (with genuinely unreachable
    Langfuse credentials, not just mocks): span.end() does not accept an
    output kwarg in the installed SDK version — output must be set via
    span.update(output=...) first, then span.end() with no arguments."""
    from src import tracing

    mock_span = MagicMock()
    tracing.end_span(mock_span, {"summary": "done"})

    mock_span.update.assert_called_once_with(output={"summary": "done"})
    mock_span.end.assert_called_once_with()


# ---- Unit tests: trajectory evals (ADR-0009) ------------------------------

def test_compute_trajectory_metrics_with_no_data_returns_zero_note(tmp_path):
    from src.evals import compute_trajectory_metrics

    metrics = compute_trajectory_metrics(
        eval_log_path=tmp_path / "eval_log.jsonl",
        escalation_queue_path=tmp_path / "escalation_queue.jsonl",
    )
    assert metrics["total_runs"] == 0
    assert "note" in metrics


def test_compute_trajectory_metrics_basic_rates(tmp_path):
    from src.evals import compute_trajectory_metrics

    eval_path = tmp_path / "eval_log.jsonl"
    escalation_path = tmp_path / "escalation_queue.jsonl"

    eval_records = [
        {"human_verdict": "correct", "total_attempts": 1, "human_intervention": False},
        {"human_verdict": "correct", "total_attempts": 2, "human_intervention": False},
        {"human_verdict": "incorrect", "total_attempts": 1,
            "human_intervention": False},
    ]
    with open(eval_path, "w") as f:
        for r in eval_records:
            f.write(json.dumps(r) + "\n")

    escalation_records = [
        {"total_attempts": 3, "human_intervention": True},
    ]
    with open(escalation_path, "w") as f:
        for r in escalation_records:
            f.write(json.dumps(r) + "\n")

    metrics = compute_trajectory_metrics(
        eval_log_path=eval_path, escalation_queue_path=escalation_path
    )

    assert metrics["total_runs"] == 4
    assert metrics["answered_runs"] == 3
    assert metrics["escalated_runs"] == 1
    assert metrics["escalation_rate"] == 0.25
    assert metrics["correct_count"] == 2
    assert metrics["incorrect_count"] == 1
    assert metrics["runs_needing_retry"] == 2  # attempts of 2 and 3
    assert metrics["human_intervention_count"] == 1


def test_format_report_handles_zero_runs():
    from src.evals import format_report

    report = format_report({"total_runs": 0, "note": "No runs recorded yet."})
    assert "No runs recorded" in report


def test_format_report_renders_nonzero_metrics():
    from src.evals import compute_trajectory_metrics, format_report

    metrics = {
        "total_runs": 2,
        "answered_runs": 2,
        "escalated_runs": 0,
        "escalation_rate": 0.0,
        "correct_count": 2,
        "incorrect_count": 0,
        "correctness_rate_among_answered": 1.0,
        "runs_needing_retry": 0,
        "retry_rate": 0.0,
        "avg_attempts_per_run": 1.0,
        "human_intervention_count": 0,
        "human_intervention_rate": 0.0,
    }
    report = format_report(metrics)
    assert "Total runs:               2" in report
    assert "100.0%" in report


# ---- Unit tests: attempts/human-intervention tracking (ADR-0009) --------

def test_advance_step_records_attempts_used():
    from src.nodes.advance_step import advance_step

    state = {
        "plan": ["How many Design courses?"],
        "current_step_index": 0,
        "step_results": [],
        "sql_query": "SELECT COUNT(*) FROM course_listings",
        "sql_result": [{"count": 1189}],
        "generation_attempt": 3,
    }
    result = advance_step(state)

    assert result["step_results"][0]["attempts_used"] == 3


def test_capture_human_feedback_aggregates_attempts_and_intervention(tmp_path):
    from src.feedback import capture_human_feedback

    with patch("src.feedback.EVAL_LOG_PATH", tmp_path / "eval_log.jsonl"), patch(
        "builtins.input", return_value="y"
    ), patch("src.feedback.CorrectionStore"):
        state = {
            "question": "Compare Design and Health",
            "step_results": [
                {
                    "sub_question": "Design?",
                    "sql_query": "q1",
                    "sql_result": [{"a": 1}],
                    "source": "model",
                    "attempts_used": 2,
                },
                {
                    "sub_question": "Health?",
                    "sql_query": "q2",
                    "sql_result": [{"a": 2}],
                    "source": "human",
                    "attempts_used": 1,
                },
            ],
        }
        record = capture_human_feedback(state)

    assert record["total_attempts"] == 3
    assert record["human_intervention"] is True


# ---- Unit tests: semantic critic (ADR-0011, Stage 3) ---------------------

def test_semantic_critic_skips_when_disabled():
    from src.nodes.semantic_critic import check_semantic_validity

    with patch("src.nodes.semantic_critic.settings.semantic_critic_enabled", False), \
            patch("ollama.chat") as mock_chat:
        state = {"is_valid": True, "plan": ["q"], "current_step_index": 0}
        result = check_semantic_validity(state)

    mock_chat.assert_not_called()
    assert result["is_valid"] is True


def test_semantic_critic_skips_when_mechanically_already_invalid():
    """No point spending an LLM call judging a query that already
    failed mechanically (ADR-0011, Decision 1)."""
    from src.nodes.semantic_critic import check_semantic_validity

    with patch("ollama.chat") as mock_chat:
        state = {
            "is_valid": False,
            "validation_reason": "Query error: no such table",
            "plan": ["q"],
            "current_step_index": 0,
        }
        result = check_semantic_validity(state)

    mock_chat.assert_not_called()
    assert result["is_valid"] is False


def test_semantic_critic_keeps_valid_when_verdict_is_valid():
    from src.nodes.semantic_critic import check_semantic_validity

    with patch("src.nodes.semantic_critic._judge_result", return_value="VALID"):
        state = {
            "is_valid": True,
            "plan": ["Which category has the highest average price?"],
            "current_step_index": 0,
            "sql_query": "SELECT category, AVG(price_usd) FROM course_listings GROUP BY category ORDER BY AVG(price_usd) DESC LIMIT 1",
            "sql_result": [{"category": "Technology", "avg_price": 140.98}],
        }
        result = check_semantic_validity(state)

    assert result["is_valid"] is True


def test_semantic_critic_flags_wrong_direction_as_invalid():
    """The exact failure class this stage exists to catch: a
    mechanically valid query that answers the wrong question (MIN
    instead of MAX for a 'highest' question)."""
    from src.nodes.semantic_critic import check_semantic_validity

    verdict = "INVALID: query used MIN instead of MAX, so it found the lowest price, not the highest"
    with patch("src.nodes.semantic_critic._judge_result", return_value=verdict):
        state = {
            "is_valid": True,
            "plan": ["Which category has the highest average price?"],
            "current_step_index": 0,
            "sql_query": "SELECT category, MIN(price_usd) FROM course_listings GROUP BY category ORDER BY MIN(price_usd) DESC LIMIT 1",
            "sql_result": [{"category": "Health", "min_price": 10.0}],
        }
        result = check_semantic_validity(state)

    assert result["is_valid"] is False
    assert result["validation_reason"].startswith("Semantic critic:")
    assert "MIN instead of MAX" in result["validation_reason"]


def test_semantic_critic_falls_back_to_generic_reason_when_unparseable():
    from src.nodes.semantic_critic import check_semantic_validity

    with patch("src.nodes.semantic_critic._judge_result", return_value="INVALID"):
        state = {
            "is_valid": True,
            "plan": ["q"],
            "current_step_index": 0,
            "sql_query": "SELECT 1",
            "sql_result": [{"1": 1}],
        }
        result = check_semantic_validity(state)

    assert result["is_valid"] is False
    assert "flagged as not answering the question" in result["validation_reason"]
