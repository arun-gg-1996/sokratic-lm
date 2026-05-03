"""
tests/test_dean_v2.py
─────────────────────
Tests for conversation/dean_v2.py — single-call Dean TurnPlan emitter
per L46/L47/L51/L53 (Track 4.5).

Coverage:
  * DeanV2.plan happy path → TurnPlan + diagnostics
  * Parse failure on attempt 1 → re-prompt → success on attempt 2
  * Parse failure on both attempts → minimal_fallback per L46
  * LLM exception → fallback with error string
  * Prior-attempts/failures appended to user prompt (replan path)
  * plan_hint_bank returns up to N angles, [] on error
  * Sticky-snapshot fallback for state["locked_topic"]==None
  * Aliases + carryover_notes injected into prompt
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from conversation.dean_v2 import DeanPlanResult, DeanV2
from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Mock client (similar shape to Anthropic SDK)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _MockContent:
    text: str


@dataclass
class _MockUsage:
    input_tokens: int = 2000
    output_tokens: int = 200
    cache_read_input_tokens: int = 0


@dataclass
class _MockResponse:
    content: list
    usage: _MockUsage


class MockClient:
    """Returns canned responses; tests cycle through `responses` per call."""

    def __init__(self, responses: list[str], raise_exc: Exception | None = None):
        self.responses = list(responses)
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    @property
    def messages(self):
        return self

    def create(self, model, max_tokens, temperature, system=None, messages=None):
        self.calls.append({
            "model": model, "system": system, "messages": messages,
        })
        if self.raise_exc:
            raise self.raise_exc
        text = self.responses.pop(0) if self.responses else "{}"
        return _MockResponse(
            content=[_MockContent(text=text)],
            usage=_MockUsage(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

VALID_PLAN_JSON = json.dumps({
    "scenario": "student gave partial answer; needs SA scaffold",
    "hint_text": "consider what initiates the heartbeat",
    "mode": "socratic",
    "tone": "encouraging",
    "permitted_terms": ["pacemaker", "atrium"],
    "forbidden_terms": ["sinoatrial node", "SA node"],
    "shape_spec": {"max_sentences": 4, "exactly_one_question": True},
    "carryover_notes": "",
    "hint_suggestions": [],
    "apply_redaction": False,
    "student_reached_answer": False,
})


def _state(**overrides):
    base = {
        "thread_id": "alice_t1",
        "student_id": "alice",
        "locked_topic": {
            "subsection": "Conduction System of the Heart",
            "section": "Cardiac Muscle and Electrical Activity",
            "chapter": "The Cardiovascular System: The Heart",
            "path": "Ch19|Cardiac Muscle and Electrical Activity|Conduction System",
        },
        "locked_question": "What initiates the heartbeat?",
        "locked_answer": "sinoatrial node",
        "locked_answer_aliases": ["SA node", "sinus node"],
        "hint_level": 1,
        "phase": "tutoring",
        "messages": [
            {"role": "tutor", "content": "Q?"},
            {"role": "student", "content": "the heart muscle?"},
        ],
    }
    base.update(overrides)
    return base


def _chunks():
    return [
        {"text": "The SA node is the heart's primary pacemaker.",
         "subsection_title": "Conduction System"},
        {"text": "Atrial depolarization spreads from the SA node.",
         "subsection_title": "Conduction System"},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_plan_returns_validated_turn_plan():
    client = MockClient(responses=[VALID_PLAN_JSON])
    dean = DeanV2(client, model="sonnet")
    result = dean.plan(_state(), _chunks())
    assert isinstance(result, DeanPlanResult)
    assert isinstance(result.turn_plan, TurnPlan)
    assert result.turn_plan.mode == "socratic"
    assert result.turn_plan.tone == "encouraging"
    assert "sinoatrial node" in result.turn_plan.forbidden_terms
    assert result.parse_attempts == 1
    assert result.used_fallback is False
    assert result.input_tokens == 2000
    assert result.error is None


def test_plan_passes_state_into_prompt():
    client = MockClient(responses=[VALID_PLAN_JSON])
    dean = DeanV2(client, model="sonnet")
    dean.plan(_state(), _chunks())
    user_msg = client.calls[0]["messages"][0]["content"]
    assert "Conduction System of the Heart" in user_msg
    assert "What initiates the heartbeat?" in user_msg
    assert "sinoatrial node" in user_msg
    # aliases
    assert "SA node" in user_msg
    # chunks
    assert "primary pacemaker" in user_msg
    # history
    assert "the heart muscle?" in user_msg


def test_plan_uses_sticky_snapshot_when_locked_topic_blank():
    state = _state(
        locked_topic=None,
        debug={"locked_topic_snapshot": {
            "subsection": "Sarcomere Structure",
            "section": "Skeletal Muscle",
        }},
    )
    client = MockClient(responses=[VALID_PLAN_JSON])
    dean = DeanV2(client, model="sonnet")
    dean.plan(state, _chunks())
    user_msg = client.calls[0]["messages"][0]["content"]
    assert "Sarcomere Structure" in user_msg


def test_plan_injects_carryover_notes():
    client = MockClient(responses=[VALID_PLAN_JSON])
    dean = DeanV2(client, model="sonnet")
    dean.plan(_state(), _chunks(),
              carryover_notes="Last session: confused depolarization with repolarization")
    user_msg = client.calls[0]["messages"][0]["content"]
    assert "depolarization" in user_msg


# ─────────────────────────────────────────────────────────────────────────────
# Parse-failure recovery (L46)
# ─────────────────────────────────────────────────────────────────────────────


def test_plan_reprompts_once_on_parse_fail_then_succeeds():
    client = MockClient(responses=["this is not json", VALID_PLAN_JSON])
    dean = DeanV2(client, model="sonnet")
    result = dean.plan(_state(), _chunks())
    assert result.parse_attempts == 2
    assert result.used_fallback is False
    assert result.turn_plan.mode == "socratic"
    assert len(client.calls) == 2
    # The 2nd call's prompt should include the RE-PROMPT instruction
    assert "RE-PROMPT" in client.calls[1]["messages"][0]["content"]
    assert "STRICT JSON" in client.calls[1]["messages"][0]["content"]


def test_plan_falls_back_to_minimal_after_two_parse_failures():
    client = MockClient(responses=["garbage 1", "garbage 2"])
    dean = DeanV2(client, model="sonnet")
    result = dean.plan(_state(), _chunks())
    assert result.used_fallback is True
    assert result.parse_attempts == 3
    # minimal_fallback shape per L46
    assert result.turn_plan.mode == "socratic"
    assert result.turn_plan.tone == "neutral"
    assert result.turn_plan.shape_spec == {"max_sentences": 3, "exactly_one_question": True}
    assert "dean_parse_failed" in result.turn_plan.scenario


def test_plan_falls_back_on_llm_exception():
    client = MockClient(responses=[], raise_exc=ConnectionError("Bedrock 503"))
    dean = DeanV2(client, model="sonnet")
    result = dean.plan(_state(), _chunks())
    assert result.used_fallback is True
    # Two attempts both raise
    assert "ConnectionError" in (result.error or "")
    # Still returns a usable TurnPlan
    assert result.turn_plan.mode == "socratic"


def test_plan_validation_failure_falls_back():
    """LLM returns parseable JSON but with invalid mode → TurnPlan
    validation rejects → treated as parse fail per the contract."""
    bad_plan = json.dumps({
        "scenario": "x", "hint_text": "y",
        "mode": "rocketship",  # invalid
        "tone": "neutral",
    })
    client = MockClient(responses=[bad_plan, bad_plan])
    dean = DeanV2(client, model="sonnet")
    result = dean.plan(_state(), _chunks())
    assert result.used_fallback is True


# ─────────────────────────────────────────────────────────────────────────────
# Replan (L50 path)
# ─────────────────────────────────────────────────────────────────────────────


def test_replan_appends_prior_attempts_and_failures_to_prompt():
    client = MockClient(responses=[VALID_PLAN_JSON])
    dean = DeanV2(client, model="sonnet")
    prior_plan = TurnPlan(scenario="x", hint_text="y", mode="socratic", tone="neutral")
    dean.replan(
        _state(), _chunks(),
        prior_plan=prior_plan,
        prior_attempts=["What's the SA node?", "Tell me about SA?"],
        prior_failures=[
            {"_check_name": "haiku_leak_check", "reason": "leaked SA node"},
            {"_check_name": "haiku_leak_check", "reason": "leaked SA"},
        ],
    )
    user_msg = client.calls[0]["messages"][0]["content"]
    assert "PRIOR ATTEMPTS THIS TURN" in user_msg
    assert "Attempt 1: What's the SA node?" in user_msg
    assert "leaked SA node" in user_msg
    assert "RE-PLANNING" in user_msg


# ─────────────────────────────────────────────────────────────────────────────
# Hint bank (L47)
# ─────────────────────────────────────────────────────────────────────────────


def test_plan_hint_bank_returns_angles():
    client = MockClient(responses=[json.dumps({
        "hint_angles": [
            "Where in the heart does the electrical impulse originate?",
            "What kind of cells fire spontaneously?",
            "Which structure sets the heart's rhythm?",
            "What's special about the wall of the right atrium?",
            "Why doesn't the heart need external nervous input to beat?",
        ]
    })])
    dean = DeanV2(client, model="sonnet")
    angles = dean.plan_hint_bank(_state(), _chunks())
    assert len(angles) == 5
    assert all(isinstance(a, str) and a.strip() for a in angles)


def test_plan_hint_bank_caps_at_n_angles():
    client = MockClient(responses=[json.dumps({
        "hint_angles": ["a", "b", "c", "d", "e", "f", "g"],
    })])
    dean = DeanV2(client, model="sonnet")
    angles = dean.plan_hint_bank(_state(), _chunks(), n_angles=3)
    assert angles == ["a", "b", "c"]


def test_plan_hint_bank_returns_empty_on_error():
    client = MockClient(responses=[], raise_exc=ConnectionError("network"))
    dean = DeanV2(client, model="sonnet")
    assert dean.plan_hint_bank(_state(), _chunks()) == []


def test_plan_hint_bank_returns_empty_on_garbage_response():
    client = MockClient(responses=["completely not json"])
    dean = DeanV2(client, model="sonnet")
    assert dean.plan_hint_bank(_state(), _chunks()) == []


def test_plan_hint_bank_filters_empty_angles():
    client = MockClient(responses=[json.dumps({
        "hint_angles": ["valid one", "", "  ", "valid two"],
    })])
    dean = DeanV2(client, model="sonnet")
    angles = dean.plan_hint_bank(_state(), _chunks())
    assert angles == ["valid one", "valid two"]
