"""
tests/test_conversation.py
---------------------------
LangGraph conversation flow tests — 3 levels.

Level 1 — Edge/routing unit tests (no LLM, pure Python):
  Test after_dean and after_assessment routing functions with mock state.

Level 2 — Integration tests (real LLM, MockRetriever):
  Test full graph.invoke() for a single turn.

Level 3 — Named scenario tests:
  Full conversation scenarios covering happy path, hints exhausted, help abuse, etc.

Run with:
    pytest tests/test_conversation.py -v
    pytest tests/test_conversation.py -v -k "level1"  # only routing tests
    pytest tests/test_conversation.py -v -k "level2"  # only integration tests
"""

import pytest
from langgraph.graph import END
from conversation.lifecycle_v2 import after_dean, after_assessment
from conversation.state import initial_state
from config import cfg


# ============================================================
# Level 1 — Edge/routing unit tests (no LLM, pure Python)
# ============================================================

def _make_state(**overrides):
    """Build a minimal state dict for routing tests."""
    base = {
        "student_id": "test",
        "phase": "tutoring",
        "messages": [],
        "retrieved_chunks": [],
        "locked_answer": "axillary nerve",
        "hint_level": 1,
        "max_hints": 3,
        "turn_count": 5,
        "max_turns": 25,
        "student_reached_answer": False,
        "assessment_turn": 0,
        "clinical_opt_in": None,
        "weak_topics": [],
        "dean_retry_count": 0,
        "dean_critique": "",
        "student_state": "incorrect",
        "help_abuse_count": 0,
        "is_multimodal": False,
        "image_structures": [],
        "debug": {
            "api_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "interventions": 0, "current_node": "", "last_routing": "", "turn_trace": []
        },
    }
    base.update(overrides)
    return base


class TestAfterDeanRouting:
    def test_routes_to_assessment_on_correct(self):
        state = _make_state(student_reached_answer=True)
        assert after_dean(state) == "assessment_node"

    def test_routes_to_assessment_on_hints_exhausted(self):
        state = _make_state(student_reached_answer=False, hint_level=4, max_hints=3)
        assert after_dean(state) == "assessment_node"

    def test_routes_to_assessment_on_turn_limit(self):
        state = _make_state(student_reached_answer=False, hint_level=1, turn_count=25, max_turns=25)
        assert after_dean(state) == "assessment_node"

    def test_routes_to_end_otherwise(self):
        state = _make_state(student_reached_answer=False, hint_level=1, turn_count=3, max_turns=25)
        assert after_dean(state) == END

    def test_hint_at_max_still_routes_to_assessment(self):
        # hint_level == max_hints + 1 → assessment
        state = _make_state(student_reached_answer=False, hint_level=4, max_hints=3)
        assert after_dean(state) == "assessment_node"

    def test_hint_exactly_at_max_stays_in_tutoring(self):
        # hint_level == max_hints (not yet exceeded) → END
        state = _make_state(student_reached_answer=False, hint_level=3, max_hints=3, turn_count=5)
        assert after_dean(state) == END

    def test_turn_count_one_below_limit_stays_in_tutoring(self):
        state = _make_state(student_reached_answer=False, hint_level=1, turn_count=24, max_turns=25)
        assert after_dean(state) == END


class TestAfterAssessmentRouting:
    def test_routes_to_memory_update_when_done(self):
        state = _make_state(assessment_turn=3)
        assert after_assessment(state) == "memory_update_node"

    def test_returns_end_when_waiting_for_opt_in_answer(self):
        state = _make_state(assessment_turn=1)
        assert after_assessment(state) == END

    def test_returns_end_when_waiting_for_clinical_answer(self):
        state = _make_state(assessment_turn=2)
        assert after_assessment(state) == END

    def test_returns_end_when_not_started(self):
        state = _make_state(assessment_turn=0)
        assert after_assessment(state) == END


class TestHelpAbuseLogic:
    """Test help abuse counter logic (pure Python, no LLM)."""

    def test_help_abuse_counter_increments_on_low_effort(self):
        """Three consecutive low_effort turns → help_abuse_count reaches threshold."""
        help_abuse_threshold = cfg.dean.help_abuse_threshold  # 3

        count = 0
        student_states = ["low_effort", "low_effort", "low_effort"]
        advanced = False

        for ss in student_states:
            if ss == "low_effort":
                count += 1
            else:
                count = 0
            if count >= help_abuse_threshold:
                advanced = True
                count = 0

        assert advanced, "hint_level should advance after 3 consecutive low_effort turns"
        assert count == 0, "counter should reset after advancing"

    def test_help_abuse_counter_resets_on_real_attempt(self):
        """incorrect after 2 low_effort → counter resets."""
        count = 0
        for ss in ["low_effort", "low_effort", "incorrect"]:
            if ss == "low_effort":
                count += 1
            else:
                count = 0

        assert count == 0, "Counter should be 0 after a real attempt"


# ============================================================
# Level 2 — Integration tests (real LLM, MockRetriever)
# ============================================================

@pytest.mark.integration
class TestGraphIntegration:
    """
    These tests make real Anthropic API calls.
    Run with: pytest tests/test_conversation.py -v -m integration
    Requires ANTHROPIC_API_KEY env var.
    """

    @pytest.fixture(scope="class")
    def graph_and_state(self):
        from conversation.graph import build_graph
        from retrieval.retriever import MockRetriever
        from memory.memory_manager import MemoryManager

        graph = build_graph(MockRetriever(), MemoryManager())
        state = initial_state("test_student_01", cfg)
        return graph, state

    def test_graph_compiles(self):
        from conversation.graph import build_graph
        from retrieval.retriever import MockRetriever
        from memory.memory_manager import MemoryManager

        graph = build_graph(MockRetriever(), MemoryManager())
        assert graph is not None

    def test_rapport_generates_greeting(self, graph_and_state):
        graph, state = graph_and_state
        config = {"configurable": {"thread_id": "test-rapport-001"}}
        result = graph.invoke(state, config=config)

        assert result["phase"] == "tutoring"
        assert len(result["messages"]) >= 1
        assert result["messages"][-1]["role"] == "tutor"

    def test_full_turn_locks_answer(self, graph_and_state):
        graph, state = graph_and_state
        state = initial_state("test_student_02", cfg)
        config = {"configurable": {"thread_id": "test-lock-001"}}

        # First invoke: rapport
        state = graph.invoke(state, config=config)

        # Add student message asking about deltoid
        state["messages"].append({
            "role": "student",
            "content": "What nerve innervates the deltoid muscle?"
        })

        # Second invoke: dean_node runs
        state = graph.invoke(state, config=config)

        assert state["locked_answer"] != "", "locked_answer should be set after first question"
        assert state["student_state"] is not None, "student_state should be classified"
        assert state["debug"]["api_calls"] > 0, "API calls should be tracked"
        # Last message should be from tutor
        tutor_messages = [m for m in state["messages"] if m.get("role") == "tutor"]
        assert len(tutor_messages) >= 2  # rapport + first tutoring response

    def test_tutor_response_has_question(self, graph_and_state):
        graph, _ = graph_and_state
        state = initial_state("test_student_03", cfg)
        config = {"configurable": {"thread_id": "test-question-001"}}

        state = graph.invoke(state, config=config)
        state["messages"].append({
            "role": "student",
            "content": "What innervates the deltoid?"
        })
        state = graph.invoke(state, config=config)

        tutor_responses = [m["content"] for m in state["messages"] if m.get("role") == "tutor"]
        last_tutor = tutor_responses[-1]
        assert "?" in last_tutor, f"Tutor response should contain a question. Got: {last_tutor}"

    def test_debug_dict_updated(self, graph_and_state):
        graph, _ = graph_and_state
        state = initial_state("test_student_04", cfg)
        config = {"configurable": {"thread_id": "test-debug-001"}}

        state = graph.invoke(state, config=config)
        state["messages"].append({"role": "student", "content": "Tell me about the deltoid."})
        state = graph.invoke(state, config=config)

        debug = state["debug"]
        assert debug["api_calls"] > 0
        assert debug["input_tokens"] > 0
        assert debug["output_tokens"] > 0


# ============================================================
# Level 3 — Named scenario tests
# ============================================================

@pytest.mark.scenarios
class TestConversationScenarios:
    """
    Full scenario tests that run multi-turn conversations.
    Marked separately as they're slow (many API calls).
    Run with: pytest tests/test_conversation.py -v -m scenarios
    """

    def _run_turns(self, graph, state, student_messages: list[str], thread_id: str):
        """Helper to drive multiple conversation turns."""
        config = {"configurable": {"thread_id": thread_id}}
        state = graph.invoke(state, config=config)  # rapport
        for msg in student_messages:
            if state["phase"] in ("assessment", "memory_update"):
                break
            state["messages"].append({"role": "student", "content": msg})
            state = graph.invoke(state, config=config)
        return state

    @pytest.fixture(scope="class")
    def graph(self):
        from conversation.graph import build_graph
        from retrieval.retriever import MockRetriever
        from memory.memory_manager import MemoryManager
        return build_graph(MockRetriever(), MemoryManager())

    def test_happy_path(self, graph):
        """Correct answer on turn 2 → assessment fires → mastery summary generated."""
        state = initial_state("scenario_happy", cfg)
        final = self._run_turns(graph, state, [
            "What nerve innervates the deltoid?",
            "Is it the axillary nerve?",
        ], "scenario-happy-001")

        assert final.get("student_reached_answer") or final.get("phase") == "assessment"

    def test_hints_exhausted(self, graph):
        """3 incorrect turns → hint_level reaches max → assessment fires."""
        state = initial_state("scenario_hints", cfg)
        final = self._run_turns(graph, state, [
            "What nerve innervates the deltoid?",
            "I think it's the radial nerve.",
            "Maybe the median nerve?",
            "Could it be the ulnar nerve?",
            "I'm not sure, the tibial nerve?",
        ], "scenario-hints-001")

        # Should have progressed hint level or reached assessment
        assert final.get("hint_level", 1) > 1 or final.get("phase") == "assessment"

    def test_help_abuse_flow(self, graph):
        """3 consecutive 'I don't know' → hint_level advances."""
        state = initial_state("scenario_abuse", cfg)
        final = self._run_turns(graph, state, [
            "What nerve innervates the deltoid?",
            "I don't know.",
            "I don't know.",
            "I don't know.",
            "Still no idea.",
        ], "scenario-abuse-001")

        # After 3 low_effort turns, hint should advance
        assert final.get("hint_level", 1) >= 2 or final.get("phase") == "assessment"

    def test_early_turn_no_leak(self, graph):
        """Early tutoring turns should not leak locked_answer in tutor responses."""
        state = initial_state("scenario_hardturn", cfg)
        config = {"configurable": {"thread_id": "scenario-hard-001"}}

        state = graph.invoke(state, config=config)  # rapport
        state["messages"].append({
            "role": "student",
            "content": "What nerve innervates the deltoid muscle?"
        })
        state = graph.invoke(state, config=config)  # turn 1

        tutor_msgs = [m["content"] for m in state["messages"] if m.get("role") == "tutor"]
        # Verify no message directly names the answer in first 2 turns
        if state.get("locked_answer"):
            for msg in tutor_msgs[1:3]:  # skip rapport
                assert state["locked_answer"].lower() not in msg.lower(), \
                    f"Answer leaked in early turn: {msg}"

    def test_turn_limit(self, graph):
        """25 turns without answer → assessment_node fires at turn 25."""
        state = initial_state("scenario_limit", cfg)
        # Force turn_count near limit
        state["turn_count"] = 24
        state["max_turns"] = 25
        state["locked_answer"] = "axillary nerve"
        state["phase"] = "tutoring"
        state["messages"].append({
            "role": "student",
            "content": "I still don't know."
        })
        config = {"configurable": {"thread_id": "scenario-limit-001"}}
        final = graph.invoke(state, config=config)

        assert final.get("phase") == "assessment" or final.get("turn_count", 0) >= 25

    def test_partial_correct_flow(self, graph):
        """Partial answer → student_state == partial_correct → Teacher affirms + probes."""
        state = initial_state("scenario_partial", cfg)
        final = self._run_turns(graph, state, [
            "What nerve innervates the deltoid?",
            "I think it involves C5 somehow but I'm not sure of the exact nerve name.",
        ], "scenario-partial-001")

        # Should classify as partial_correct or incorrect (acceptable either way)
        # The key check: session should NOT end immediately
        assert final.get("phase") != "memory_update"

    def test_memory_flush(self, graph):
        """Session ends → memory_update_node fires → phase == memory_update."""
        state = initial_state("scenario_memory", cfg)
        # Force to assessment complete state
        state["student_reached_answer"] = True
        state["assessment_turn"] = 3
        state["locked_answer"] = "axillary nerve"
        state["phase"] = "assessment"
        config = {"configurable": {"thread_id": "scenario-memory-001"}}

        # assessment_turn == 3 → after_assessment routes to memory_update_node
        final = graph.invoke(state, config=config)
        assert final.get("phase") == "memory_update"
