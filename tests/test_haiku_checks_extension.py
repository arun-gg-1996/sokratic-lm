"""
tests/test_haiku_checks_extension.py
────────────────────────────────────
Tests for the Track 4.2 additions to conversation/classifiers.py:
  * haiku_shape_check — L48 #3 + L59 (5 sub-checks in one Haiku call)
  * haiku_pedagogy_check — L48 #4 + L60 (EULER relevance + helpful)
  * to_universal_check_result — L61 adapter to {pass, reason, evidence}

Mocks the underlying _haiku_call so the tests are hermetic + fast.
"""
from __future__ import annotations

import json

import pytest

from conversation import classifiers as C


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — patch _haiku_call for hermetic tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_haiku(monkeypatch):
    """Make _haiku_call return whatever the test sets via .set_response()."""
    holder: dict = {"response": "", "raise_exc": None}

    def fake(system_blocks, user_text):
        if holder["raise_exc"]:
            raise holder["raise_exc"]
        return holder["response"]

    monkeypatch.setattr(C, "_haiku_call", fake)

    class Setter:
        def set_response(self, text: str):
            holder["response"] = text

        def raise_exception(self, exc: Exception):
            holder["raise_exc"] = exc

    return Setter()


# ─────────────────────────────────────────────────────────────────────────────
# haiku_shape_check
# ─────────────────────────────────────────────────────────────────────────────


def _shape_response(passed: bool, **overrides) -> str:
    payload = {
        "pass": passed,
        "reason": "" if passed else "draft has 2 questions",
        "evidence": "" if passed else "What is X? And what is Y?",
        "checks": {
            "length": True, "single_question": passed,
            "banned_prefix": True, "hint_level_alignment": True,
            "no_repetition": True,
        },
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_shape_check_pass(mock_haiku):
    mock_haiku.set_response(_shape_response(True))
    out = C.haiku_shape_check(
        "What initiates the heartbeat in this region?",
        shape_spec={"max_sentences": 4, "exactly_one_question": True},
        hint_level=1,
        hint_text="think about pacemaker cells",
        prior_tutor_questions=["Where does cardiac conduction begin?"],
    )
    assert out["pass"] is True
    assert out["reason"] == ""
    assert out["evidence"] == ""
    assert out["checks"]["single_question"] is True


def test_shape_check_fail_two_questions(mock_haiku):
    draft = "What is X? And what is Y?"
    payload = json.loads(_shape_response(False))
    payload["evidence"] = draft  # match the draft so evidence validation passes
    mock_haiku.set_response(json.dumps(payload))
    out = C.haiku_shape_check(draft)
    assert out["pass"] is False
    assert "2 questions" in out["reason"]
    assert out["checks"]["single_question"] is False


def test_shape_check_evidence_validation_downgrades_hallucination(mock_haiku):
    """If LLM cites evidence that's NOT in the draft, downgrade to pass
    (mirrors the existing hint_leak_check policy)."""
    draft = "What initiates the heartbeat?"
    bad = json.loads(_shape_response(False))
    bad["evidence"] = "completely_fabricated_fragment_not_in_draft"
    mock_haiku.set_response(json.dumps(bad))
    out = C.haiku_shape_check(draft)
    assert out["pass"] is True
    assert out["_error"] == "evidence_invalid"


def test_shape_check_handles_empty_draft(mock_haiku):
    out = C.haiku_shape_check("")
    assert out["pass"] is True
    assert out["_error"] == "empty_draft"


def test_shape_check_handles_haiku_exception(mock_haiku):
    mock_haiku.raise_exception(RuntimeError("Bedrock 503"))
    out = C.haiku_shape_check("What is X?")
    assert out["pass"] is True  # safe default
    assert "haiku_error" in out["_error"]


def test_shape_check_handles_garbage_response(mock_haiku):
    mock_haiku.set_response("totally not json")
    out = C.haiku_shape_check("What is X?")
    assert out["pass"] is True
    assert out["_error"] == "parse_fail"


def test_shape_check_passes_shape_spec_to_prompt(mock_haiku, monkeypatch):
    """Verify shape_spec.max_sentences ends up in the user prompt
    (so Haiku can verify the right limit)."""
    captured = {"user": ""}

    def fake(system_blocks, user_text):
        captured["user"] = user_text
        return _shape_response(True)

    monkeypatch.setattr(C, "_haiku_call", fake)
    C.haiku_shape_check(
        "test", shape_spec={"max_sentences": 7, "exactly_one_question": True},
        hint_level=2,
    )
    assert "max_sentences: 7" in captured["user"]
    assert "hint_level: 2" in captured["user"]


# ─────────────────────────────────────────────────────────────────────────────
# haiku_pedagogy_check
# ─────────────────────────────────────────────────────────────────────────────


def _pedagogy_response(passed: bool, **overrides) -> str:
    payload = {
        "pass": passed,
        "reason": "" if passed else "draft just restates the question",
        "evidence": "" if passed else "what is the SA node?",
        "checks": {"relevance": True, "helpful": passed},
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_pedagogy_check_pass(mock_haiku):
    mock_haiku.set_response(_pedagogy_response(True))
    out = C.haiku_pedagogy_check(
        "What kind of cells initiate cardiac contractions?",
        locked_subsection="Conduction System of the Heart",
        locked_question="What is the SA node?",
    )
    assert out["pass"] is True
    assert out["checks"]["relevance"] is True
    assert out["checks"]["helpful"] is True


def test_pedagogy_check_fail_unhelpful(mock_haiku):
    draft = "what is the SA node?"
    fail = json.loads(_pedagogy_response(False))
    fail["evidence"] = draft  # so evidence-validate passes
    mock_haiku.set_response(json.dumps(fail))
    out = C.haiku_pedagogy_check(draft, locked_subsection="x", locked_question="What is the SA node?")
    assert out["pass"] is False
    assert "restates" in out["reason"]


def test_pedagogy_check_handles_haiku_exception(mock_haiku):
    mock_haiku.raise_exception(RuntimeError("network"))
    out = C.haiku_pedagogy_check("anything", locked_subsection="x", locked_question="x")
    assert out["pass"] is True
    assert "haiku_error" in out["_error"]


def test_pedagogy_check_handles_empty_draft(mock_haiku):
    out = C.haiku_pedagogy_check("", locked_subsection="x", locked_question="x")
    assert out["pass"] is True
    assert out["_error"] == "empty_draft"


# ─────────────────────────────────────────────────────────────────────────────
# to_universal_check_result — L61 adapter
# ─────────────────────────────────────────────────────────────────────────────


def test_adapter_passthrough_for_universal_shape():
    """haiku_shape_check / haiku_pedagogy_check already emit the universal
    shape; adapter should pass them through with _check_name attached."""
    raw = {"pass": False, "reason": "two questions", "evidence": "What X? What Y?",
           "checks": {"single_question": False}}
    out = C.to_universal_check_result(raw, check_name="haiku_shape_check")
    assert out["_check_name"] == "haiku_shape_check"
    assert out["pass"] is False
    assert out["reason"] == "two questions"
    assert out["checks"]["single_question"] is False


def test_adapter_maps_leak_verdict_to_fail():
    """haiku_hint_leak_check returns verdict='leak' on failure."""
    raw = {"verdict": "leak", "rationale": "starts with letter A", "evidence": "A...",
           "leak_type": "letter"}
    out = C.to_universal_check_result(raw, check_name="haiku_leak_check")
    assert out["pass"] is False
    assert out["reason"] == "starts with letter A"
    assert out["evidence"] == "A..."
    assert out["_verdict"] == "leak"


def test_adapter_maps_clean_verdict_to_pass():
    raw = {"verdict": "clean", "rationale": "", "evidence": ""}
    out = C.to_universal_check_result(raw, check_name="haiku_leak_check")
    assert out["pass"] is True
    assert out["reason"] == ""
    assert out["evidence"] == ""


def test_adapter_maps_sycophancy_verdict():
    raw = {"verdict": "sycophantic", "rationale": "starts with 'Excellent!'",
           "evidence": "Excellent!"}
    out = C.to_universal_check_result(raw, check_name="haiku_sycophancy_check")
    assert out["pass"] is False
    assert "Excellent" in out["reason"]


def test_adapter_maps_off_domain_verdicts():
    """off_domain has 4 fail verdicts; all of them must map to fail."""
    for v in ["substance", "chitchat", "jailbreak", "answer_demand"]:
        raw = {"verdict": v, "rationale": "off-topic", "evidence": "..."}
        out = C.to_universal_check_result(raw, check_name="haiku_off_domain_check")
        assert out["pass"] is False, f"verdict={v} should map to fail"
        assert out["_verdict"] == v


def test_adapter_maps_in_domain_to_pass():
    raw = {"verdict": "in_domain", "rationale": "", "evidence": ""}
    out = C.to_universal_check_result(raw, check_name="haiku_off_domain_check")
    assert out["pass"] is True


def test_adapter_preserves_diagnostics():
    raw = {"pass": True, "reason": "", "evidence": "",
           "_elapsed_s": 0.42, "_error": ""}
    out = C.to_universal_check_result(raw, check_name="haiku_shape_check")
    assert out["_elapsed_s"] == 0.42
    assert out["_error"] == ""


def test_adapter_handles_truncated_strings():
    """Long reason/evidence shouldn't blow up trace serialization."""
    raw = {"pass": False, "reason": "x" * 500, "evidence": "y" * 500}
    out = C.to_universal_check_result(raw, check_name="haiku_shape_check")
    assert len(out["reason"]) <= 240
    assert len(out["evidence"]) <= 240
