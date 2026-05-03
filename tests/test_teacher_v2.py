"""
tests/test_teacher_v2.py
────────────────────────
Tests for conversation/teacher_v2.py — single Teacher entry point per L49.

Coverage:
  * build_teacher_prompt: every mode renders correctly with the right blocks
  * Forbidden / permitted / carryover blocks appear iff their fields are set
  * Mode-specific blocks (clinical scenario, locked context, chunks, history)
  * Unknown mode raises ValueError
  * TeacherV2.draft: mock client; returns TeacherDraftResult with diagnostics
  * Retry feedback (prior_attempts + prior_failures) appended to prompt
  * LLM exception → empty text + error string (never raises)
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from conversation.teacher_v2 import (
    TeacherDraftResult,
    TeacherPromptInputs,
    TeacherV2,
    build_teacher_prompt,
)
from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _socratic_plan(**overrides) -> TurnPlan:
    base = dict(
        scenario="student needs scaffolding",
        hint_text="consider what initiates the heartbeat",
        mode="socratic",
        tone="encouraging",
        permitted_terms=["pacemaker", "atrium"],
        forbidden_terms=["sinoatrial node", "SA node"],
        shape_spec={"max_sentences": 4, "exactly_one_question": True},
        carryover_notes="prior session: confused depolarization with repolarization",
    )
    base.update(overrides)
    return TurnPlan(**base)


def _clinical_plan(**overrides) -> TurnPlan:
    base = dict(
        scenario="clinical phase ready",
        hint_text="",
        mode="clinical",
        tone="neutral",
        clinical_scenario="65yo patient presents with bradycardia, HR 35",
        clinical_target="SA node dysfunction; pacemaker indicated",
        forbidden_terms=["SA node dysfunction"],
    )
    base.update(overrides)
    return TurnPlan(**base)


def _inputs(**overrides) -> TeacherPromptInputs:
    base = dict(
        chunks=[
            {"text": "The SA node is the heart's primary pacemaker.",
             "subsection_title": "Conduction System"},
            {"text": "Atrial depolarization spreads from the SA node.",
             "subsection_title": "Conduction System"},
        ],
        history=[
            {"role": "tutor", "content": "What initiates the heartbeat?"},
            {"role": "student", "content": "the heart muscle?"},
        ],
        locked_subsection="Conduction System of the Heart",
        locked_question="What initiates the heartbeat?",
        # Set explicit domain so the test isn't coupled to the
        # dataclass default (which changed under L78 to "subject"
        # so a missing cfg surfaces loudly in production).
        domain_name="human anatomy",
        domain_short="anatomy",
    )
    base.update(overrides)
    return TeacherPromptInputs(**base)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Anthropic client
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _MockContent:
    text: str


@dataclass
class _MockUsage:
    input_tokens: int = 1500
    output_tokens: int = 60
    cache_read_input_tokens: int = 0


@dataclass
class _MockResponse:
    content: list
    usage: _MockUsage


class MockClient:
    def __init__(self, response_text: str = "What kind of cells initiate the heartbeat?",
                 raise_exc: Exception | None = None):
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
        return _MockResponse(
            content=[_MockContent(text=self.response_text)],
            usage=_MockUsage(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# build_teacher_prompt — pure function, every mode
# ─────────────────────────────────────────────────────────────────────────────


def test_socratic_prompt_includes_all_required_blocks():
    p = build_teacher_prompt(_socratic_plan(), _inputs())
    # Mode instructions present
    assert "Socratic anatomy tutor" in p
    # Tone variable injected
    assert "TONE: encouraging" in p
    # Forbidden terms block
    assert "FORBIDDEN TERMS" in p and "sinoatrial node" in p
    # Permitted terms block
    assert "PERMITTED TERMS" in p and "pacemaker" in p
    # Carryover notes
    assert "CARRYOVER NOTES" in p and "depolarization" in p
    # Hint text
    assert "HINT TEXT" in p and "what initiates the heartbeat" in p
    # Locked context
    assert "LOCKED SUBSECTION" in p and "Conduction System" in p
    # Chunks
    assert "RETRIEVED CHUNKS" in p and "primary pacemaker" in p
    # History
    assert "CONVERSATION HISTORY" in p and "STUDENT: the heart muscle" in p
    # Output instruction at end
    assert p.rstrip().endswith("explanations.")


def test_socratic_prompt_omits_blocks_when_fields_empty():
    """Note: the mode instructions themselves mention "FORBIDDEN TERMS"
    in prose ("Do NOT reveal the FORBIDDEN TERMS"). The block-specific
    suffix "(you must NOT use these or any close variant)" only appears
    when the block itself is rendered."""
    plan = _socratic_plan(
        permitted_terms=[],
        forbidden_terms=[],
        carryover_notes="",
        hint_text="",
    )
    p = build_teacher_prompt(plan, _inputs(chunks=[], history=[]))
    assert "you must NOT use these or any close variant" not in p
    assert "PERMITTED TERMS (you may lean on" not in p
    assert "CARRYOVER NOTES (relevant prior-session" not in p
    assert "HINT TEXT (Dean's intended scaffolding" not in p
    assert "RETRIEVED CHUNKS (ground every claim" not in p
    assert "CONVERSATION HISTORY (most recent last)" not in p


def test_clinical_prompt_includes_scenario_and_target():
    p = build_teacher_prompt(_clinical_plan(), _inputs())
    assert "CLINICAL SCENARIO" in p
    assert "65yo patient" in p
    assert "CLINICAL TARGET" in p
    assert "SA node dysfunction" in p
    assert "TONE: neutral" in p


def test_clinical_prompt_omits_clinical_block_when_fields_empty():
    """Same as the socratic test — instructions reference CLINICAL
    SCENARIO in prose; the explicit block (with the actual scenario
    text) only renders when clinical_scenario is set."""
    plan = TurnPlan(
        scenario="x", hint_text="", mode="clinical", tone="neutral",
        clinical_scenario=None, clinical_target=None,
    )
    p = build_teacher_prompt(plan, _inputs())
    # Block-specific suffix only present when block is rendered
    assert "CLINICAL TARGET (do not reveal — this is what student must reach)" not in p


def test_rapport_prompt_uses_time_of_day():
    plan = TurnPlan(scenario="opening", hint_text="", mode="rapport", tone="encouraging")
    p = build_teacher_prompt(plan, _inputs(time_of_day="morning"))
    assert "Good morning" in p
    assert "RETRIEVED CHUNKS" not in p  # rapport doesn't use chunks
    assert "CONVERSATION HISTORY" not in p  # nor history


def test_opt_in_prompt_short_and_no_chunks():
    plan = TurnPlan(scenario="reached answer", hint_text="", mode="opt_in", tone="encouraging")
    p = build_teacher_prompt(plan, _inputs())
    assert "clinical-application" in p
    assert "RETRIEVED CHUNKS" not in p
    assert "CONVERSATION HISTORY" not in p
    # Opt-in still surfaces the locked subsection so question is anchored
    assert "LOCKED SUBSECTION" in p


def test_redirect_prompt_omits_chunks_includes_hint():
    plan = TurnPlan(
        scenario="help abuse", hint_text="invite a partial guess",
        mode="redirect", tone="neutral",
        forbidden_terms=["SA node"],
    )
    p = build_teacher_prompt(plan, _inputs())
    assert "HELP-ABUSE" in p
    assert "HINT TEXT" in p
    assert "FORBIDDEN TERMS" in p
    assert "RETRIEVED CHUNKS" not in p


def test_nudge_prompt_off_domain():
    plan = TurnPlan(scenario="off topic", hint_text="", mode="nudge", tone="firm")
    p = build_teacher_prompt(plan, _inputs())
    assert "OFF-DOMAIN" in p
    assert "TONE: firm" in p


def test_confirm_end_prompt_yes_no():
    plan = TurnPlan(scenario="deflect", hint_text="", mode="confirm_end", tone="neutral")
    p = build_teacher_prompt(plan, _inputs())
    assert "wrap up" in p
    assert "yes/no question" in p
    assert "LOCKED SUBSECTION" in p


def test_honest_close_prompt_no_question():
    plan = TurnPlan(scenario="ended off domain", hint_text="", mode="honest_close",
                    tone="honest")
    p = build_teacher_prompt(plan, _inputs())
    assert "honestly" in p
    assert "Conduction System" in p  # locked_subsection injected
    assert "No closing question" in p


def test_unknown_mode_raises():
    """Constructing a TurnPlan with bad mode is rejected at TurnPlan
    validation time. But guard the prompt builder too in case enum changes."""
    plan = TurnPlan(scenario="x", hint_text="y", mode="socratic", tone="neutral")
    plan.mode = "rocketship"  # bypass validation
    with pytest.raises(ValueError, match="Unknown TurnPlan mode"):
        build_teacher_prompt(plan, _inputs())


# ─────────────────────────────────────────────────────────────────────────────
# TeacherV2.draft — happy path + diagnostics
# ─────────────────────────────────────────────────────────────────────────────


def test_draft_happy_path_returns_text_and_diagnostics():
    client = MockClient(response_text="What kind of cells start the heartbeat?")
    teacher = TeacherV2(client, model="sonnet")
    out = teacher.draft(_socratic_plan(), _inputs())
    assert isinstance(out, TeacherDraftResult)
    assert out.text == "What kind of cells start the heartbeat?"
    assert out.mode == "socratic"
    assert out.tone == "encouraging"
    assert out.input_tokens == 1500
    assert out.output_tokens == 60
    assert out.error is None


def test_draft_passes_prompt_to_client():
    client = MockClient()
    teacher = TeacherV2(client, model="sonnet")
    teacher.draft(_socratic_plan(), _inputs())
    sent = client.last_messages[0]["content"]
    assert "FORBIDDEN TERMS" in sent
    assert "Socratic anatomy tutor" in sent


def test_draft_handles_llm_exception_gracefully():
    client = MockClient(raise_exc=ConnectionError("Bedrock 503"))
    teacher = TeacherV2(client, model="sonnet")
    out = teacher.draft(_socratic_plan(), _inputs())
    assert out.text == ""
    assert "ConnectionError" in out.error
    assert out.mode == "socratic"


# ─────────────────────────────────────────────────────────────────────────────
# Retry feedback loop (L62 — preview, full retry orchestration in Track 4.6)
# ─────────────────────────────────────────────────────────────────────────────


def test_draft_appends_prior_attempts_to_prompt():
    client = MockClient()
    teacher = TeacherV2(client, model="sonnet")
    teacher.draft(
        _socratic_plan(),
        _inputs(),
        prior_attempts=["What's the SA node?", "Can you tell me about SA?"],
        prior_failures=[
            {"_check_name": "haiku_leak_check", "reason": "leaked SA node"},
            {"_check_name": "haiku_leak_check", "reason": "leaked SA"},
        ],
    )
    sent = client.last_messages[0]["content"]
    assert "PRIOR ATTEMPTS" in sent
    assert "Attempt 1: What's the SA node?" in sent
    assert "leaked SA node" in sent


def test_draft_no_prior_attempts_no_addendum():
    client = MockClient()
    teacher = TeacherV2(client, model="sonnet")
    teacher.draft(_socratic_plan(), _inputs())
    sent = client.last_messages[0]["content"]
    assert "PRIOR ATTEMPTS" not in sent


# ─────────────────────────────────────────────────────────────────────────────
# Tone orthogonality — Codex round-2 fix #3
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tone", ["encouraging", "firm", "neutral", "honest"])
def test_each_tone_renders_in_prompt(tone):
    plan = TurnPlan(scenario="x", hint_text="y", mode="socratic", tone=tone)
    p = build_teacher_prompt(plan, _inputs())
    assert f"TONE: {tone}" in p


@pytest.mark.parametrize("mode", ["socratic", "clinical", "rapport", "opt_in",
                                    "redirect", "nudge", "confirm_end", "honest_close"])
def test_each_mode_renders_without_crashing(mode):
    plan = TurnPlan(scenario="x", hint_text="y", mode=mode, tone="neutral")
    p = build_teacher_prompt(plan, _inputs())
    assert "Output ONLY" in p  # footer made it through
