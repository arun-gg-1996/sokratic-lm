"""
tests/test_turn_plan.py
───────────────────────
Unit tests for conversation/turn_plan.py (L46 — Dean→Teacher contract).

Coverage:
  * Construction validates required fields, mode/tone enums, types
  * mode/tone are orthogonal — independently validated
  * apply_redaction=True is rejected (Option C invariant)
  * from_llm_json: happy path, markdown fences, missing optional fields,
    extra keys, malformed JSON
  * minimal_fallback shape per L46 spec
  * round-trip via to_dict / to_json
"""
from __future__ import annotations

import json

import pytest

from conversation.turn_plan import (
    DEFAULT_SHAPE_SPEC,
    MODES,
    TONES,
    TurnPlan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Locked enum membership
# ─────────────────────────────────────────────────────────────────────────────

def test_modes_match_l46_spec():
    """L46 + Codex round-2 fix #3: 8 modes covering every situation."""
    assert MODES == {
        "socratic", "clinical", "rapport", "opt_in",
        "redirect", "nudge", "confirm_end", "honest_close",
    }


def test_tones_match_l46_spec():
    assert TONES == {"encouraging", "firm", "neutral", "honest"}


def test_default_shape_spec():
    assert DEFAULT_SHAPE_SPEC == {"max_sentences": 4, "exactly_one_question": True}


# ─────────────────────────────────────────────────────────────────────────────
# Construction validation
# ─────────────────────────────────────────────────────────────────────────────

def _valid_kwargs(**overrides):
    base = {
        "scenario": "student gave partial answer",
        "hint_text": "consider what happens at the SA node first",
        "mode": "socratic",
        "tone": "encouraging",
    }
    base.update(overrides)
    return base


def test_construct_valid_minimal():
    tp = TurnPlan(**_valid_kwargs())
    assert tp.scenario == "student gave partial answer"
    assert tp.permitted_terms == []
    assert tp.forbidden_terms == []
    assert tp.shape_spec == DEFAULT_SHAPE_SPEC
    assert tp.apply_redaction is False
    assert tp.image_context is None


def test_construct_full():
    tp = TurnPlan(
        scenario="x", hint_text="y", mode="clinical", tone="firm",
        permitted_terms=["pacemaker", "atrium"],
        forbidden_terms=["sinoatrial node", "SA node"],
        shape_spec={"max_sentences": 3, "exactly_one_question": True},
        carryover_notes="prior session: confused depolarization with repolarization",
        hint_suggestions=["What initiates the heartbeat?", "Where does the wave start?"],
        student_reached_answer=False,
        image_context={"image_type": "diagram"},
        clinical_scenario="65yo patient with bradycardia",
        clinical_target="SA node dysfunction",
    )
    assert tp.mode == "clinical"
    assert tp.tone == "firm"
    assert tp.image_context == {"image_type": "diagram"}


# Required fields
def test_empty_scenario_rejected():
    with pytest.raises(ValueError, match="scenario"):
        TurnPlan(**_valid_kwargs(scenario=""))
    with pytest.raises(ValueError, match="scenario"):
        TurnPlan(**_valid_kwargs(scenario="   "))


def test_non_string_hint_text_rejected():
    with pytest.raises(ValueError, match="hint_text"):
        TurnPlan(**_valid_kwargs(hint_text=42))  # type: ignore[arg-type]


# Mode / tone enum validation
@pytest.mark.parametrize("mode", sorted(MODES))
def test_each_mode_value_accepted(mode):
    tp = TurnPlan(**_valid_kwargs(mode=mode))
    assert tp.mode == mode


@pytest.mark.parametrize("tone", sorted(TONES))
def test_each_tone_value_accepted(tone):
    tp = TurnPlan(**_valid_kwargs(tone=tone))
    assert tp.tone == tone


def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="mode"):
        TurnPlan(**_valid_kwargs(mode="rocketship"))


def test_invalid_tone_rejected():
    with pytest.raises(ValueError, match="tone"):
        TurnPlan(**_valid_kwargs(tone="sarcastic"))


def test_mode_and_tone_orthogonal():
    """Codex round-2 fix #3: mode is situation/phase; tone is register.
    Every (mode, tone) combination should construct cleanly."""
    for m in MODES:
        for t in TONES:
            TurnPlan(**_valid_kwargs(mode=m, tone=t))


# Option C invariant
def test_apply_redaction_true_rejected():
    """L43 + L52: Option C means redaction is OFF. Forward-compat field
    must stay False until Phase 6 lands."""
    with pytest.raises(ValueError, match="apply_redaction"):
        TurnPlan(**_valid_kwargs(apply_redaction=True))


# Type guards on list / dict fields
def test_permitted_terms_must_be_list_of_strings():
    with pytest.raises(ValueError, match="permitted_terms"):
        TurnPlan(**_valid_kwargs(permitted_terms="not a list"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="permitted_terms"):
        TurnPlan(**_valid_kwargs(permitted_terms=["ok", 42]))  # type: ignore[list-item]


def test_forbidden_terms_must_be_list_of_strings():
    with pytest.raises(ValueError, match="forbidden_terms"):
        TurnPlan(**_valid_kwargs(forbidden_terms=[None]))  # type: ignore[list-item]


def test_shape_spec_validation():
    with pytest.raises(ValueError, match="shape_spec"):
        TurnPlan(**_valid_kwargs(shape_spec=["not", "a", "dict"]))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_sentences"):
        TurnPlan(**_valid_kwargs(shape_spec={"max_sentences": "four"}))  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="exactly_one_question"):
        TurnPlan(**_valid_kwargs(shape_spec={"exactly_one_question": "yes"}))  # type: ignore[dict-item]


# ─────────────────────────────────────────────────────────────────────────────
# from_llm_json
# ─────────────────────────────────────────────────────────────────────────────

VALID_LLM_PAYLOAD = {
    "scenario": "student gave partial answer",
    "hint_text": "consider the SA node",
    "mode": "socratic",
    "tone": "encouraging",
    "permitted_terms": ["pacemaker"],
    "forbidden_terms": ["sinoatrial node"],
    "shape_spec": {"max_sentences": 4, "exactly_one_question": True},
    "carryover_notes": "",
    "hint_suggestions": [],
    "apply_redaction": False,
    "student_reached_answer": False,
}


def test_from_llm_json_happy_path():
    tp = TurnPlan.from_llm_json(json.dumps(VALID_LLM_PAYLOAD))
    assert tp.scenario == "student gave partial answer"
    assert tp.mode == "socratic"


def test_from_llm_json_strips_markdown_fences():
    fenced = "```json\n" + json.dumps(VALID_LLM_PAYLOAD) + "\n```"
    tp = TurnPlan.from_llm_json(fenced)
    assert tp.mode == "socratic"


def test_from_llm_json_strips_unfenced_markdown():
    fenced = "```\n" + json.dumps(VALID_LLM_PAYLOAD) + "\n```"
    tp = TurnPlan.from_llm_json(fenced)
    assert tp.mode == "socratic"


def test_from_llm_json_finds_object_in_noisy_response():
    """LLM occasionally prepends 'Here is the JSON:'. Defensive parser
    extracts the first JSON object."""
    noisy = "Here is the TurnPlan:\n" + json.dumps(VALID_LLM_PAYLOAD)
    tp = TurnPlan.from_llm_json(noisy)
    assert tp.mode == "socratic"


def test_from_llm_json_drops_unknown_keys():
    """Future-proofing: an LLM sending an extra `priority` key shouldn't
    blow up validation."""
    payload = {**VALID_LLM_PAYLOAD, "priority": "high", "future_field": [1, 2]}
    tp = TurnPlan.from_llm_json(json.dumps(payload))
    assert tp.mode == "socratic"


def test_from_llm_json_defaults_missing_optional_fields():
    minimal = {
        "scenario": "x", "hint_text": "y", "mode": "rapport", "tone": "neutral",
    }
    tp = TurnPlan.from_llm_json(json.dumps(minimal))
    assert tp.permitted_terms == []
    assert tp.forbidden_terms == []
    assert tp.shape_spec == DEFAULT_SHAPE_SPEC


def test_from_llm_json_coerces_non_string_term_items():
    """LLM occasionally returns numbers/None inside a terms list."""
    payload = {**VALID_LLM_PAYLOAD, "permitted_terms": ["valid", 42, None]}
    tp = TurnPlan.from_llm_json(json.dumps(payload))
    assert tp.permitted_terms == ["valid", "42", "None"]


def test_from_llm_json_lowercases_mode_and_tone():
    payload = {**VALID_LLM_PAYLOAD, "mode": "SOCRATIC", "tone": "Encouraging"}
    tp = TurnPlan.from_llm_json(json.dumps(payload))
    assert tp.mode == "socratic"
    assert tp.tone == "encouraging"


def test_from_llm_json_raises_on_bad_json():
    with pytest.raises(ValueError):
        TurnPlan.from_llm_json("complete garbage no json anywhere")


def test_from_llm_json_raises_on_non_object():
    with pytest.raises(ValueError):
        TurnPlan.from_llm_json("[1, 2, 3]")


def test_from_llm_json_raises_on_invalid_mode_after_parse():
    payload = {**VALID_LLM_PAYLOAD, "mode": "rocket"}
    with pytest.raises(ValueError, match="mode"):
        TurnPlan.from_llm_json(json.dumps(payload))


def test_from_llm_json_handles_apply_redaction_true_via_validation():
    """LLM occasionally hallucinates apply_redaction:true — must reject."""
    payload = {**VALID_LLM_PAYLOAD, "apply_redaction": True}
    with pytest.raises(ValueError, match="apply_redaction"):
        TurnPlan.from_llm_json(json.dumps(payload))


# ─────────────────────────────────────────────────────────────────────────────
# minimal_fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_minimal_fallback_per_l46_spec():
    """Per L46: 'if 2nd parse fails, ship a minimal TurnPlan with only
    scenario, hint_text, tone="neutral", shape_spec={max_sentences:3,
    exactly_one_question:true}'."""
    tp = TurnPlan.minimal_fallback()
    assert tp.mode == "socratic"
    assert tp.tone == "neutral"
    assert tp.shape_spec == {"max_sentences": 3, "exactly_one_question": True}
    assert tp.permitted_terms == []
    assert tp.forbidden_terms == []
    assert tp.hint_suggestions == []
    assert tp.apply_redaction is False


def test_minimal_fallback_carries_explicit_args():
    tp = TurnPlan.minimal_fallback(
        scenario="dean_qc_failed_twice",
        hint_text="think about the basic anatomy here",
        tone="honest",
    )
    assert tp.scenario == "dean_qc_failed_twice"
    assert tp.hint_text == "think about the basic anatomy here"
    assert tp.tone == "honest"


# ─────────────────────────────────────────────────────────────────────────────
# Serialization round-trip
# ─────────────────────────────────────────────────────────────────────────────

def test_to_dict_and_back():
    original = TurnPlan(**_valid_kwargs(
        permitted_terms=["x"], forbidden_terms=["y"],
        carryover_notes="prior misconception",
        clinical_scenario="case", clinical_target="answer",
    ))
    payload = original.to_dict()
    restored = TurnPlan.from_llm_json(json.dumps(payload))
    assert restored.to_dict() == original.to_dict()


def test_to_json_serializes_cleanly():
    tp = TurnPlan(**_valid_kwargs())
    s = tp.to_json()
    parsed = json.loads(s)
    assert parsed["mode"] == "socratic"
    assert parsed["apply_redaction"] is False
