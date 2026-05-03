"""
tests/test_observation_extractor.py
───────────────────────────────────
Unit tests for memory/observation_extractor.py (L4 — single Haiku
extraction at session end, replaces 5 _build_* heuristic methods).

Coverage:
  * extract_observations returns Observation list with both categories
  * malformed JSON, LLM exception → returns [] (never raises)
  * markdown-fenced JSON parses correctly
  * empty / no-message session → still calls but returns whatever Haiku gives
  * topic info pulled from locked_topic + sticky snapshot fallback
  * categories restricted to {misconception, learning_style}
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from memory.observation_extractor import (
    ALLOWED_CATEGORIES,
    Observation,
    extract_observations,
)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Anthropic client
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _MockContent:
    text: str


@dataclass
class _MockResponse:
    content: list


class MockClient:
    def __init__(self, response_text: str = "", raise_exc: Exception | None = None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.last_messages = None

    @property
    def messages(self):
        return self

    def create(self, model, max_tokens, temperature, messages):
        self.last_messages = messages
        if self.raise_exc:
            raise self.raise_exc
        return _MockResponse(content=[_MockContent(text=self.response_text)])


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def base_state() -> dict:
    return {
        "thread_id": "alice_t1",
        "student_id": "alice",
        "locked_topic": {
            "path": "Ch11|Pectoral Girdle and Upper Limbs|Muscles That Move the Humerus",
            "chapter": "The Muscular System",
            "section": "Muscles of the Pectoral Girdle and Upper Limbs",
            "subsection": "Muscles That Move the Humerus",
        },
        "locked_question": "Which muscle initiates shoulder abduction?",
        "locked_answer": "supraspinatus",
        "student_reached_answer": False,
        "hint_level": 2,
        "messages": [
            {"role": "tutor", "content": "Good morning. Which muscle initiates shoulder abduction?"},
            {"role": "student", "content": "deltoid?"},
            {"role": "tutor", "content": "The deltoid is involved later — what acts first 0-15°?"},
            {"role": "student", "content": "i'm not sure, maybe the trapezius?"},
            {"role": "tutor", "content": "Think about the rotator cuff."},
            {"role": "student", "content": "infraspinatus"},
        ],
    }


GOOD_RESPONSE = json.dumps({
    "misconceptions": [
        {"text": "Student initially thought the deltoid initiates shoulder abduction at 0-15°, but the supraspinatus is responsible for that range.",
         "evidence": "deltoid? ... infraspinatus"},
    ],
    "learning_style": [
        {"text": "Hedges responses frequently with 'i'm not sure, maybe...' patterns; benefits from validation before pushing forward.",
         "evidence": "i'm not sure, maybe the trapezius?"},
    ],
})


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_returns_both_categories(base_state):
    client = MockClient(response_text=GOOD_RESPONSE)
    out = extract_observations(base_state, client=client, model="haiku")
    assert len(out) == 2
    cats = sorted(o.category for o in out)
    assert cats == ["learning_style", "misconception"]
    misc = next(o for o in out if o.category == "misconception")
    assert "supraspinatus" in misc.text


def test_categories_restricted():
    """Only misconception + learning_style are allowed (per L1)."""
    assert ALLOWED_CATEGORIES == {"misconception", "learning_style"}


# ─────────────────────────────────────────────────────────────────────────────
# Resilience
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_handles_llm_exception(base_state):
    client = MockClient(raise_exc=RuntimeError("AWS Bedrock 503"))
    out = extract_observations(base_state, client=client, model="haiku")
    assert out == []


def test_extract_handles_garbage_response(base_state):
    client = MockClient(response_text="this is not json at all")
    out = extract_observations(base_state, client=client, model="haiku")
    assert out == []


def test_extract_handles_markdown_fenced_json(base_state):
    fenced = "```json\n" + GOOD_RESPONSE + "\n```"
    client = MockClient(response_text=fenced)
    out = extract_observations(base_state, client=client, model="haiku")
    assert len(out) == 2


def test_extract_drops_items_without_text(base_state):
    response = json.dumps({
        "misconceptions": [
            {"text": "valid one", "evidence": "x"},
            {"text": "", "evidence": "x"},  # empty text → dropped
            {"evidence": "no text key"},     # missing key → dropped
        ],
        "learning_style": [],
    })
    client = MockClient(response_text=response)
    out = extract_observations(base_state, client=client, model="haiku")
    assert len(out) == 1 and out[0].text == "valid one"


def test_extract_handles_empty_categories(base_state):
    response = json.dumps({"misconceptions": [], "learning_style": []})
    client = MockClient(response_text=response)
    out = extract_observations(base_state, client=client, model="haiku")
    assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# State handling
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_uses_sticky_snapshot_when_locked_topic_blank():
    """When state['locked_topic'] is None at session end (clinical-pass
    artifact), falls back to debug.locked_topic_snapshot."""
    state = {
        "thread_id": "t1",
        "messages": [{"role": "student", "content": "test"}],
        "locked_topic": None,
        "debug": {
            "locked_topic_snapshot": {
                "subsection": "Sarcomere Structure",
                "path": "Ch10|Skeletal Muscle|Sarcomere Structure",
            }
        },
    }
    client = MockClient(response_text=GOOD_RESPONSE)
    extract_observations(state, client=client, model="haiku")
    # Verify the prompt the LLM saw mentions the sticky-snapshot topic
    prompt = client.last_messages[0]["content"]
    assert "Sarcomere Structure" in prompt


def test_extract_handles_no_locked_topic(base_state):
    """No topic at all → LLM still gets a prompt, just with '(unspecified)'."""
    state = {**base_state, "locked_topic": None, "debug": {}, "topic_selection": ""}
    client = MockClient(response_text=GOOD_RESPONSE)
    out = extract_observations(state, client=client, model="haiku")
    assert "(unspecified)" in client.last_messages[0]["content"]
    assert len(out) == 2  # extractor still works


def test_extract_includes_transcript_in_prompt(base_state):
    client = MockClient(response_text=GOOD_RESPONSE)
    extract_observations(base_state, client=client, model="haiku")
    prompt = client.last_messages[0]["content"]
    assert "deltoid?" in prompt
    assert "infraspinatus" in prompt
    assert "STUDENT" in prompt and "TUTOR" in prompt
