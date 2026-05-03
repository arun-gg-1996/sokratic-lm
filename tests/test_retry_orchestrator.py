"""
tests/test_retry_orchestrator.py
────────────────────────────────
Tests for conversation/retry_orchestrator.py — bounded retry loop per
L50 + L62 (Track 4.6).

Coverage:
  * Attempt 1 passes → ship immediately, no replan
  * Attempt 1-2 fail / Attempt 3 passes → ship draft 3 (no replan)
  * All 3 attempts fail → Dean re-plans → attempt 4 passes → ship draft 4
  * All 3 + replan still fails leak_check → safe-generic-probe (CRITICAL)
  * All 3 + replan fails non-leak only → ship draft 4 anyway (per L50)
  * Teacher empty draft → retry counts but feeds the failure forward
  * Hard timeout → safe-generic-probe (timed_out=True)
  * Retry feedback (prior_drafts + prior_failures) appended to Teacher
    + Dean prompts
  * leak_cap_fallback_fired flag set correctly
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from conversation import classifiers as C
from conversation import dean_v2, teacher_v2
from conversation.dean_v2 import DeanPlanResult, DeanV2
from conversation.retry_orchestrator import (
    MAX_TEACHER_ATTEMPTS,
    SAFE_GENERIC_PROBE,
    TurnAttempt,
    TurnRunResult,
    run_turn,
)
from conversation.teacher_v2 import (
    TeacherDraftResult,
    TeacherPromptInputs,
    TeacherV2,
)
from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles for Teacher / Dean / Haiku quartet
# ─────────────────────────────────────────────────────────────────────────────


class FakeTeacher:
    """Returns canned drafts from `drafts` list, one per call."""
    def __init__(self, drafts: list[str], errors: list[bool] | None = None):
        self.drafts = list(drafts)
        self.errors = list(errors or [])
        self.calls: list[dict] = []

    def draft(self, turn_plan, inputs, *, prior_attempts=None, prior_failures=None):
        self.calls.append({
            "turn_plan": turn_plan,
            "prior_attempts": list(prior_attempts or []),
            "prior_failures": list(prior_failures or []),
        })
        i = len(self.calls) - 1
        is_error = i < len(self.errors) and self.errors[i]
        return TeacherDraftResult(
            text="" if is_error else self.drafts[i] if i < len(self.drafts) else "leftover",
            mode=turn_plan.mode,
            tone=turn_plan.tone,
            elapsed_ms=10,
            input_tokens=100,
            output_tokens=20,
            error="simulated_teacher_error" if is_error else None,
        )


class FakeDean:
    """Returns canned replanned TurnPlan."""
    def __init__(self, replan_plan: TurnPlan):
        self.replan_plan = replan_plan
        self.replan_calls: list[dict] = []

    def replan(self, state, chunks, *, prior_plan, prior_attempts, prior_failures, carryover_notes=""):
        self.replan_calls.append({
            "state": state,
            "prior_plan": prior_plan,
            "prior_attempts": list(prior_attempts),
            "prior_failures": list(prior_failures),
        })
        return DeanPlanResult(
            turn_plan=self.replan_plan,
            elapsed_ms=10, input_tokens=100, output_tokens=20,
            parse_attempts=1, used_fallback=False,
        )


# Patch the 4 Haiku checks so the test controls pass/fail per call
@pytest.fixture
def mock_quartet(monkeypatch):
    """Sequence-based control over the quartet results.

    Each test sets `quartet.results` to a list-of-list-of-dicts:
      results[0] = check results for attempt 1, etc.
    Each inner list has 4 dicts, one per check (in order:
    leak, sycophancy, shape, pedagogy), each with at least
    {"_check_name": "...", "pass": bool, "reason": "...", "evidence": "..."}.
    """
    state = {"results": [], "call_count": 0}

    def fake_run_quartet(*args, **kwargs):
        idx = state["call_count"]
        state["call_count"] += 1
        if idx >= len(state["results"]):
            # Default: all pass
            return [
                {"_check_name": n, "pass": True, "reason": "", "evidence": ""}
                for n in ["haiku_leak_check", "haiku_sycophancy_check",
                          "haiku_shape_check", "haiku_pedagogy_check"]
            ]
        return state["results"][idx]

    from conversation import retry_orchestrator as RO
    monkeypatch.setattr(RO, "_run_haiku_quartet", fake_run_quartet)
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _plan(mode="socratic", tone="encouraging", forbidden_terms=None):
    return TurnPlan(
        scenario="test", hint_text="hint",
        mode=mode, tone=tone,
        forbidden_terms=forbidden_terms or ["sinoatrial node", "SA node"],
    )


def _inputs():
    return TeacherPromptInputs(
        chunks=[], history=[],
        locked_subsection="Conduction System of the Heart",
        locked_question="What initiates the heartbeat?",
    )


def _all_pass():
    return [{"_check_name": n, "pass": True, "reason": "", "evidence": ""}
            for n in ["haiku_leak_check", "haiku_sycophancy_check",
                      "haiku_shape_check", "haiku_pedagogy_check"]]


def _leak_fail():
    return [
        {"_check_name": "haiku_leak_check", "pass": False,
         "reason": "leaked SA node", "evidence": "SA node"},
        {"_check_name": "haiku_sycophancy_check", "pass": True, "reason": "", "evidence": ""},
        {"_check_name": "haiku_shape_check", "pass": True, "reason": "", "evidence": ""},
        {"_check_name": "haiku_pedagogy_check", "pass": True, "reason": "", "evidence": ""},
    ]


def _shape_fail():
    return [
        {"_check_name": "haiku_leak_check", "pass": True, "reason": "", "evidence": ""},
        {"_check_name": "haiku_sycophancy_check", "pass": True, "reason": "", "evidence": ""},
        {"_check_name": "haiku_shape_check", "pass": False,
         "reason": "two questions", "evidence": "What X? What Y?"},
        {"_check_name": "haiku_pedagogy_check", "pass": True, "reason": "", "evidence": ""},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Happy path: attempt 1 passes
# ─────────────────────────────────────────────────────────────────────────────


def test_attempt_1_passes_ships_immediately(mock_quartet):
    teacher = FakeTeacher(drafts=["What kind of cells start the heartbeat?"])
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_all_pass()]
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    assert out.final_attempt == 1
    assert out.final_text == "What kind of cells start the heartbeat?"
    assert out.used_safe_generic_probe is False
    assert out.used_dean_replan is False
    assert out.leak_cap_fallback_fired is False
    assert len(out.attempts) == 1
    assert dean.replan_calls == []  # no replan


# ─────────────────────────────────────────────────────────────────────────────
# Mid-retry success
# ─────────────────────────────────────────────────────────────────────────────


def test_attempt_3_passes_after_two_failures(mock_quartet):
    teacher = FakeTeacher(drafts=["draft1", "draft2", "draft3"])
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_shape_fail(), _shape_fail(), _all_pass()]
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    assert out.final_attempt == 3
    assert out.final_text == "draft3"
    assert out.used_dean_replan is False
    assert len(out.attempts) == 3


def test_retry_feedback_appended_per_attempt(mock_quartet):
    teacher = FakeTeacher(drafts=["draft1", "draft2", "draft3"])
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_shape_fail(), _shape_fail(), _all_pass()]
    run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    # Attempt 1 — no priors
    assert teacher.calls[0]["prior_attempts"] == []
    # Attempt 2 — has draft1 + its failure
    assert teacher.calls[1]["prior_attempts"] == ["draft1"]
    assert teacher.calls[1]["prior_failures"][0]["_check_name"] == "haiku_shape_check"
    # Attempt 3 — has draft1+draft2 + 2 failures
    assert teacher.calls[2]["prior_attempts"] == ["draft1", "draft2"]
    assert len(teacher.calls[2]["prior_failures"]) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Dean re-plan path (3 attempts fail → replan → attempt 4)
# ─────────────────────────────────────────────────────────────────────────────


def test_dean_replan_fires_after_three_failures(mock_quartet):
    teacher = FakeTeacher(drafts=["d1", "d2", "d3", "d4"])
    new_plan = _plan(forbidden_terms=["sinoatrial node", "SA node", "pacemaker"])
    dean = FakeDean(replan_plan=new_plan)
    mock_quartet["results"] = [_shape_fail(), _shape_fail(), _shape_fail(), _all_pass()]
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    assert out.used_dean_replan is True
    assert out.final_attempt == 4
    assert out.final_text == "d4"
    assert len(dean.replan_calls) == 1
    # Replan got all 3 prior attempts + their failures
    assert len(dean.replan_calls[0]["prior_attempts"]) == 3
    assert dean.replan_calls[0]["prior_failures"][0]["_check_name"] == "haiku_shape_check"
    # Attempt 4 used the new plan
    assert teacher.calls[3]["turn_plan"] is new_plan


# ─────────────────────────────────────────────────────────────────────────────
# Critical safety: leak after replan → safe-generic-probe (Codex round-1 fix #5)
# ─────────────────────────────────────────────────────────────────────────────


def test_leak_after_replan_falls_back_to_safe_generic_probe(mock_quartet):
    teacher = FakeTeacher(drafts=["d1", "d2", "d3", "d4_with_leak"])
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_leak_fail(), _leak_fail(), _leak_fail(), _leak_fail()]
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    assert out.used_safe_generic_probe is True
    assert out.leak_cap_fallback_fired is True
    assert out.final_text == SAFE_GENERIC_PROBE
    assert out.final_attempt == 5
    # Draft 4 was NOT shipped
    assert "d4_with_leak" not in out.final_text


def test_non_leak_failure_after_replan_ships_draft_anyway(mock_quartet):
    """Per L50: if only sycophancy/shape/pedagogy fails on attempt 4,
    ship draft 4 anyway (less critical than leak)."""
    teacher = FakeTeacher(drafts=["d1", "d2", "d3", "d4_two_questions"])
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_shape_fail(), _shape_fail(), _shape_fail(), _shape_fail()]
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    assert out.used_safe_generic_probe is False
    assert out.leak_cap_fallback_fired is False
    assert out.final_text == "d4_two_questions"
    assert out.final_attempt == 4
    assert out.used_dean_replan is True


# ─────────────────────────────────────────────────────────────────────────────
# Teacher errors during retry
# ─────────────────────────────────────────────────────────────────────────────


def test_teacher_empty_draft_counts_as_attempt_and_continues(mock_quartet):
    """If Teacher returns empty (LLM error), the attempt counts but the
    failure is fed forward and the loop continues."""
    teacher = FakeTeacher(
        drafts=["", "d2", "d3"],
        errors=[True, False, False],
    )
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_all_pass(), _all_pass()]  # called for d2 + d3 only
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
    )
    # First call returned empty → no quartet ran, advanced to attempt 2
    # Second call returned d2 → quartet ran (call 0), all passed → ship d2
    assert out.final_text == "d2"
    assert out.final_attempt == 2
    assert out.attempts[0].draft == ""  # empty attempt recorded


# ─────────────────────────────────────────────────────────────────────────────
# Timeout
# ─────────────────────────────────────────────────────────────────────────────


def test_hard_timeout_falls_back_to_safe_probe(mock_quartet, monkeypatch):
    """If wall-clock exceeds timeout_s before completion → safe probe."""
    teacher = FakeTeacher(drafts=["d1"])
    dean = FakeDean(replan_plan=_plan())
    mock_quartet["results"] = [_shape_fail()]
    out = run_turn(
        teacher=teacher, dean=dean,
        turn_plan=_plan(), teacher_inputs=_inputs(),
        dean_state={}, dean_chunks=[],
        locked_answer="sinoatrial node",
        timeout_s=0.0,  # immediate timeout
    )
    assert out.timed_out is True
    assert out.used_safe_generic_probe is True
    assert out.final_text == SAFE_GENERIC_PROBE


# ─────────────────────────────────────────────────────────────────────────────
# Helper sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_attempt_all_passed_helper():
    att = TurnAttempt(attempt_num=1, draft="x", checks=_all_pass())
    assert att.all_passed is True
    assert att.leak_passed is True
    assert att.failed_check_names() == []


def test_attempt_failed_helpers():
    att = TurnAttempt(attempt_num=2, draft="x", checks=_leak_fail())
    assert att.all_passed is False
    assert att.leak_passed is False
    assert att.failed_check_names() == ["haiku_leak_check"]


def test_attempt_failure_summary():
    att = TurnAttempt(attempt_num=2, draft="x", checks=_leak_fail())
    summary = att.failure_summary()
    assert summary["_check_name"] == "haiku_leak_check"
    assert "leaked" in summary["reason"]


def test_max_attempts_constant_is_3():
    """L50: 3 Teacher attempts before Dean re-plans."""
    assert MAX_TEACHER_ATTEMPTS == 3


def test_safe_generic_probe_is_non_leaking():
    """Sanity: the templated fallback must not contain anything that
    could possibly be a leak-pattern."""
    assert "answer" not in SAFE_GENERIC_PROBE.lower()
    assert "the" in SAFE_GENERIC_PROBE.lower()  # at least non-empty natural language
    assert "?" in SAFE_GENERIC_PROBE  # has a question
