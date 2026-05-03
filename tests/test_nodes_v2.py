"""
tests/test_nodes_v2.py
──────────────────────
Tests for conversation/nodes_v2.py — the v2 graph nodes that compose
preflight + dean_v2 + teacher_v2 + retry_orchestrator (Track 4.7b).

Coverage:
  * use_v2_flow() reads SOKRATIC_USE_V2_FLOW env var correctly
  * dean_node_v2 routes unlocked-topic turns through topic_lock_v2
  * dean_node_v2 whitespace-input guard (no LLM call on empty messages)
  * Pre-flight FIRES → Dean SKIPPED, Teacher renders redirect, counters
    update, hint_level forced advance at strike 4 per L55
  * Pre-flight FIRES with should_end_session → phase transitions to
    memory_update + last message tagged is_closing
  * Pre-flight passes → Dean.plan + retry orchestrator run; final text
    appended; turn_count increments
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from conversation import nodes_v2 as N
from conversation.preflight import PreflightResult
from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────────────


def test_use_v2_flow_default_off(monkeypatch):
    monkeypatch.delenv("SOKRATIC_USE_V2_FLOW", raising=False)
    assert N.use_v2_flow() is False


def test_use_v2_flow_on_when_env_set(monkeypatch):
    monkeypatch.setenv("SOKRATIC_USE_V2_FLOW", "1")
    assert N.use_v2_flow() is True


def test_use_v2_flow_off_when_env_zero(monkeypatch):
    monkeypatch.setenv("SOKRATIC_USE_V2_FLOW", "0")
    assert N.use_v2_flow() is False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _state(**overrides):
    base = {
        "thread_id": "alice_t1",
        "student_id": "alice",
        "phase": "tutoring",
        "hint_level": 1,
        "turn_count": 5,
        "help_abuse_count": 0,
        "off_topic_count": 0,
        "locked_topic": {
            "subsection": "Conduction System of the Heart",
            "section": "Cardiac Muscle and Electrical Activity",
            "chapter": "The Cardiovascular System: The Heart",
            "path": "Ch19|Cardiac|Conduction System",
        },
        "locked_question": "What initiates the heartbeat?",
        "locked_answer": "sinoatrial node",
        "locked_answer_aliases": ["SA node"],
        "messages": [
            {"role": "tutor", "content": "What initiates the heartbeat?"},
            {"role": "student", "content": "the heart muscle?"},
        ],
        "debug": {"turn_trace": [], "all_turn_traces": []},
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Topic-not-locked → v2 topic lock
# ─────────────────────────────────────────────────────────────────────────────


def test_dean_node_v2_routes_to_topic_lock_v2_when_topic_unlocked():
    """When state.locked_topic is empty/missing, use Track 4.7d v2 lock flow."""
    state = _state(locked_topic={})  # not locked
    with patch("conversation.nodes_v2.run_topic_lock_v2") as mock_lock:
        mock_lock.return_value = {"phase": "tutoring", "messages": ["topic_lock_v2"]}
        result = N.dean_node_v2(
            state, dean=MagicMock(), teacher=MagicMock(), retriever=MagicMock(),
        )
        mock_lock.assert_called_once()
        assert result["messages"] == ["topic_lock_v2"]


# ─────────────────────────────────────────────────────────────────────────────
# Whitespace input guard
# ─────────────────────────────────────────────────────────────────────────────


def test_dean_node_v2_whitespace_guard_skips_llm():
    """Empty/whitespace-only student messages get a templated nudge —
    no LLM call."""
    state = _state(messages=[
        {"role": "tutor", "content": "What is X?"},
        {"role": "student", "content": "   "},
    ])
    # Note: locked_topic is set, so dean_node_v2 takes ownership; the guard
    # fires inside v2 logic before any LLM call.
    result = N.dean_node_v2(
        state, dean=MagicMock(), teacher=MagicMock(), retriever=MagicMock(),
    )
    assert "messages" in result
    last = result["messages"][-1]
    assert last["role"] == "tutor"
    assert "empty" in last["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight fires → Teacher renders redirect/nudge/confirm_end
# ─────────────────────────────────────────────────────────────────────────────


def _mock_preflight_fires(category="help_abuse", suggested_mode="redirect",
                           suggested_tone="neutral",
                           should_force_hint_advance=False,
                           should_end_session=False,
                           help_count=1, off_count=0):
    return PreflightResult(
        fired=True,
        category=category,
        evidence="just tell me",
        rationale="help-abuse pattern",
        new_help_abuse_count=help_count,
        new_off_topic_count=off_count,
        suggested_mode=suggested_mode,
        suggested_tone=suggested_tone,
        should_force_hint_advance=should_force_hint_advance,
        should_end_session=should_end_session,
        elapsed_s=0.5,
    )


def test_dean_node_v2_preflight_help_abuse_renders_redirect(monkeypatch):
    """Help-abuse fired → teacher_v2 writes redirect; Dean SKIPPED."""
    state = _state()
    monkeypatch.setattr(N, "run_preflight", lambda *a, **kw: _mock_preflight_fires())

    # Mock teacher_v2.draft to return a canned redirect text
    fake_draft = MagicMock()
    fake_draft.text = "Let's think together — what part of the heart starts it?"
    fake_draft.mode = "redirect"
    fake_draft.tone = "neutral"
    fake_draft.elapsed_ms = 100
    fake_draft.input_tokens = 50
    fake_draft.output_tokens = 20
    fake_draft.error = None

    with patch("conversation.nodes_v2.TeacherV2") as MockTeacher, \
         patch("conversation.llm_client.make_anthropic_client", return_value=MagicMock()), \
         patch("conversation.llm_client.resolve_model", side_effect=lambda m: m):
        MockTeacher.return_value.draft.return_value = fake_draft

        result = N.dean_node_v2(
            state, dean=MagicMock(), teacher=MagicMock(), retriever=MagicMock(),
        )

    # New tutor message appended
    last = result["messages"][-1]
    assert last["role"] == "tutor"
    assert "Let's think together" in last["content"]
    assert last["metadata"]["preflight_category"] == "help_abuse"
    assert last["metadata"]["mode"] == "redirect"
    # Counter updated, hint not advanced (strike < 4)
    assert result["help_abuse_count"] == 1
    assert result["hint_level"] == 1  # unchanged


def test_dean_node_v2_preflight_strike_4_forces_hint_advance(monkeypatch):
    state = _state(hint_level=1, help_abuse_count=3)
    monkeypatch.setattr(
        N, "run_preflight",
        lambda *a, **kw: _mock_preflight_fires(
            help_count=4, should_force_hint_advance=True,
        ),
    )
    fake_draft = MagicMock(
        text="Let me try a more direct version: ...",
        mode="redirect", tone="neutral", elapsed_ms=100,
        input_tokens=50, output_tokens=20, error=None,
    )
    with patch("conversation.nodes_v2.TeacherV2") as MockTeacher, \
         patch("conversation.llm_client.make_anthropic_client", return_value=MagicMock()), \
         patch("conversation.llm_client.resolve_model", side_effect=lambda m: m):
        MockTeacher.return_value.draft.return_value = fake_draft
        result = N.dean_node_v2(
            state, dean=MagicMock(), teacher=MagicMock(), retriever=MagicMock(),
        )
    assert result["hint_level"] == 2  # advanced from 1 to 2


def test_dean_node_v2_preflight_off_domain_strike_4_ends_session(monkeypatch):
    state = _state(off_topic_count=3)
    monkeypatch.setattr(
        N, "run_preflight",
        lambda *a, **kw: _mock_preflight_fires(
            category="off_domain", suggested_mode="honest_close",
            suggested_tone="honest", off_count=4, should_end_session=True,
        ),
    )
    fake_draft = MagicMock(
        text="It looks like we're not making progress on the locked topic — let's wrap up.",
        mode="honest_close", tone="honest", elapsed_ms=100,
        input_tokens=50, output_tokens=20, error=None,
    )
    with patch("conversation.nodes_v2.TeacherV2") as MockTeacher, \
         patch("conversation.llm_client.make_anthropic_client", return_value=MagicMock()), \
         patch("conversation.llm_client.resolve_model", side_effect=lambda m: m):
        MockTeacher.return_value.draft.return_value = fake_draft
        result = N.dean_node_v2(
            state, dean=MagicMock(), teacher=MagicMock(), retriever=MagicMock(),
        )
    assert result["phase"] == "memory_update"
    assert result["messages"][-1]["metadata"]["is_closing"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight passes → Dean.plan + retry orchestrator
# ─────────────────────────────────────────────────────────────────────────────


def _mock_preflight_passes():
    return PreflightResult(
        fired=False, category="none",
        new_help_abuse_count=0, new_off_topic_count=0,
        elapsed_s=0.5,
    )


def test_dean_node_v2_normal_path_runs_retry_orchestrator(monkeypatch):
    """All pre-flight checks pass → Dean.plan → retry orchestrator → ship."""
    state = _state()
    monkeypatch.setattr(N, "run_preflight", lambda *a, **kw: _mock_preflight_passes())

    # Mock retriever
    retriever = MagicMock()
    retriever.retrieve.return_value = [
        {"text": "The SA node is the pacemaker.", "subsection_title": "Conduction"},
    ]

    # Mock dean_v2.plan returning a valid TurnPlan + result wrapper
    from conversation.dean_v2 import DeanPlanResult
    fake_plan = TurnPlan(scenario="x", hint_text="y", mode="socratic", tone="encouraging")
    plan_result = DeanPlanResult(
        turn_plan=fake_plan, elapsed_ms=200, input_tokens=2000,
        output_tokens=200, parse_attempts=1, used_fallback=False,
    )

    # Mock retry_orchestrator.run_turn returning final text
    from conversation.retry_orchestrator import TurnRunResult
    final_result = TurnRunResult(
        final_text="What kind of cells fire spontaneously to start the heartbeat?",
        final_attempt=1, used_safe_generic_probe=False,
        used_dean_replan=False, leak_cap_fallback_fired=False,
        timed_out=False, elapsed_ms=1500, attempts=[], final_turn_plan=fake_plan,
    )

    with patch("conversation.nodes_v2.DeanV2") as MockDean, \
         patch("conversation.nodes_v2.TeacherV2") as MockTeacher, \
         patch("conversation.nodes_v2.run_turn", return_value=final_result), \
         patch("conversation.llm_client.make_anthropic_client", return_value=MagicMock()), \
         patch("conversation.llm_client.resolve_model", side_effect=lambda m: m):
        MockDean.return_value.plan.return_value = plan_result
        MockTeacher.return_value = MagicMock()

        result = N.dean_node_v2(
            state, dean=MagicMock(), teacher=MagicMock(), retriever=retriever,
        )

    last = result["messages"][-1]
    assert last["role"] == "tutor"
    assert "fire spontaneously" in last["content"]
    assert last["metadata"]["mode"] == "socratic"
    assert last["metadata"]["tone"] == "encouraging"
    assert last["metadata"]["final_attempt"] == 1
    assert result["turn_count"] == 6  # 5 + 1
    assert result["help_abuse_count"] == 0  # reset on engagement


def test_dean_node_v2_writes_to_turn_trace(monkeypatch):
    """Trace consumers can see preflight + dean.plan + retry orchestrator
    diagnostics in state.debug.turn_trace."""
    state = _state()
    monkeypatch.setattr(N, "run_preflight", lambda *a, **kw: _mock_preflight_passes())
    retriever = MagicMock()
    retriever.retrieve.return_value = []

    from conversation.dean_v2 import DeanPlanResult
    fake_plan = TurnPlan(scenario="x", hint_text="y", mode="socratic", tone="neutral")
    plan_result = DeanPlanResult(
        turn_plan=fake_plan, elapsed_ms=100, parse_attempts=1, used_fallback=False,
    )
    from conversation.retry_orchestrator import TurnRunResult
    final_result = TurnRunResult(
        final_text="ok", final_attempt=1, used_safe_generic_probe=False,
        used_dean_replan=False, leak_cap_fallback_fired=False,
        timed_out=False, elapsed_ms=100, attempts=[], final_turn_plan=fake_plan,
    )
    with patch("conversation.nodes_v2.DeanV2") as MockDean, \
         patch("conversation.nodes_v2.TeacherV2") as MockTeacher, \
         patch("conversation.nodes_v2.run_turn", return_value=final_result), \
         patch("conversation.llm_client.make_anthropic_client", return_value=MagicMock()), \
         patch("conversation.llm_client.resolve_model", side_effect=lambda m: m):
        MockDean.return_value.plan.return_value = plan_result
        N.dean_node_v2(state, dean=MagicMock(), teacher=MagicMock(), retriever=retriever)

    trace = state["debug"]["turn_trace"]
    wrapper_names = [t.get("wrapper") for t in trace]
    assert "preflight" in wrapper_names
    assert "dean_v2.plan" in wrapper_names
    assert "retry_orchestrator.run_turn" in wrapper_names
