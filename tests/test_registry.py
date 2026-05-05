"""
tests/test_registry.py
======================
BLOCK 1 verification — vocabulary registry purity + structure.

Three test categories:
  1. Structure: every class has expected dict + methods
  2. Purity (Safeguard #2): no topic-specific terms in any value
  3. Determinism: system_prompt_block() returns identical output on
     repeated calls (cache safety)
"""
from __future__ import annotations

from conversation.registry import (
    HintTransitionVocabulary,
    IntentVocabulary,
    ModalEventVocabulary,
    PhaseVocabulary,
    TeacherModeVocabulary,
    ToneTierVocabulary,
    all_vocabulary_blocks,
)


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------

ALL_REGISTRIES = [
    IntentVocabulary,
    TeacherModeVocabulary,
    ToneTierVocabulary,
    PhaseVocabulary,
    ModalEventVocabulary,
    HintTransitionVocabulary,
]


def test_each_registry_has_dict_and_prompt_block():
    """Every registry class exposes a dict + system_prompt_block() method."""
    expected_dict_attrs = {
        "IntentVocabulary": "INTENTS",
        "TeacherModeVocabulary": "MODES",
        "ToneTierVocabulary": "TONES",
        "PhaseVocabulary": "PHASES",
        "ModalEventVocabulary": "EVENTS",
        "HintTransitionVocabulary": "TRANSITIONS",
    }
    for reg in ALL_REGISTRIES:
        attr = expected_dict_attrs[reg.__name__]
        assert hasattr(reg, attr), f"{reg.__name__} missing dict attr {attr}"
        d = getattr(reg, attr)
        assert isinstance(d, dict) and len(d) > 0, f"{reg.__name__}.{attr} is empty"
        assert hasattr(reg, "system_prompt_block"), f"{reg.__name__} missing system_prompt_block()"
        block = reg.system_prompt_block()
        assert isinstance(block, str) and block.strip(), f"{reg.__name__}.system_prompt_block() empty"


def test_intent_vocabulary_has_required_verdicts():
    """The 8 intent verdicts that the system depends on must be present."""
    required = {
        "on_topic_engaged", "low_effort", "help_abuse", "off_domain",
        "deflection", "opt_in_yes", "opt_in_no", "opt_in_ambiguous",
    }
    actual = set(IntentVocabulary.INTENTS.keys())
    missing = required - actual
    assert not missing, f"IntentVocabulary missing required verdicts: {missing}"


def test_teacher_mode_vocabulary_has_required_modes():
    """The Teacher modes that node code dispatches on must all be present."""
    required = {
        "socratic", "clinical", "rapport", "opt_in", "redirect", "nudge",
        "confirm_end", "honest_close", "reach_close", "clinical_natural_close",
        "close", "soft_reset",
    }
    actual = set(TeacherModeVocabulary.MODES.keys())
    missing = required - actual
    assert not missing, f"TeacherModeVocabulary missing required modes: {missing}"


def test_phase_vocabulary_has_4_phases():
    required = {"rapport", "tutoring", "assessment", "memory_update"}
    actual = set(PhaseVocabulary.PHASES.keys())
    assert actual == required, f"PhaseVocabulary mismatch: expected {required}, got {actual}"


def test_tone_vocabulary_has_4_tones():
    required = {"encouraging", "neutral", "firm", "honest"}
    actual = set(ToneTierVocabulary.TONES.keys())
    assert actual == required, f"ToneTierVocabulary mismatch: expected {required}, got {actual}"


# ---------------------------------------------------------------------------
# Purity tests (Safeguard #2)
# ---------------------------------------------------------------------------

# Topic-specific terms that must NEVER appear in registry values.
FORBIDDEN_TOPIC_TERMS = [
    "thyroid", "glycolysis", "pyruvate", "metabolic hormones",
    "serous", "pleural", "pericardium", "visceral", "parietal",
    "skeletal muscle", "phospholipid", "cytoplasm",
    "cushion and reduce friction", "endomembrane",
    "patient", "diagnosis",
]


def test_registry_purity_no_topic_terms():
    """No registry value should contain anatomy/medical/topic-specific text."""
    violations: list[tuple[str, str, str]] = []
    for reg in ALL_REGISTRIES:
        for attr_name in ("INTENTS", "MODES", "TONES", "PHASES", "EVENTS", "TRANSITIONS"):
            d = getattr(reg, attr_name, None)
            if d is None:
                continue
            for k, v in d.items():
                v_lower = v.lower()
                for term in FORBIDDEN_TOPIC_TERMS:
                    if term.lower() in v_lower:
                        violations.append((reg.__name__, k, term))
            break
    assert not violations, (
        "Registry purity violated:\n"
        + "\n".join(f"  {r}.{k} contains '{t}'" for r, k, t in violations)
    )


def test_system_prompt_blocks_have_no_topic_terms():
    violations: list[tuple[str, str]] = []
    for reg in ALL_REGISTRIES:
        block = reg.system_prompt_block().lower()
        for term in FORBIDDEN_TOPIC_TERMS:
            if term.lower() in block:
                violations.append((reg.__name__, term))
    assert not violations, (
        "Rendered prompt blocks contain forbidden topic terms:\n"
        + "\n".join(f"  {r} contains '{t}'" for r, t in violations)
    )


def test_combined_blocks_purity():
    combined = all_vocabulary_blocks().lower()
    violations = [t for t in FORBIDDEN_TOPIC_TERMS if t.lower() in combined]
    assert not violations, (
        f"all_vocabulary_blocks() contains forbidden topic terms: {violations}"
    )


# ---------------------------------------------------------------------------
# Determinism tests (cache safety)
# ---------------------------------------------------------------------------

def test_system_prompt_blocks_deterministic():
    """Repeated calls must return byte-identical output (cache safety)."""
    for reg in ALL_REGISTRIES:
        first = reg.system_prompt_block()
        for _ in range(5):
            assert reg.system_prompt_block() == first, (
                f"{reg.__name__}.system_prompt_block() not deterministic"
            )


def test_all_vocabulary_blocks_deterministic():
    first = all_vocabulary_blocks()
    for _ in range(5):
        assert all_vocabulary_blocks() == first


# ---------------------------------------------------------------------------
# Annotation tests
# ---------------------------------------------------------------------------

def test_intent_annotate_basic():
    assert IntentVocabulary.annotate("low_effort") == "[intent=low_effort]"


def test_intent_annotate_with_extras():
    out = IntentVocabulary.annotate("low_effort", consecutive_low_effort=3, evidence="idk")
    assert "intent=low_effort" in out
    assert "consecutive_low_effort=3" in out
    assert "evidence=idk" in out
    assert IntentVocabulary.annotate("low_effort", consecutive_low_effort=3, evidence="idk") == out


def test_intent_annotate_unknown_falls_back():
    out = IntentVocabulary.annotate("not_a_real_verdict")
    assert "intent=on_topic_engaged" in out


def test_teacher_mode_annotate():
    out = TeacherModeVocabulary.annotate("redirect", tone="firm", attempts=2)
    assert "mode=redirect" in out
    assert "tone=firm" in out
    assert "attempts=2" in out


def test_modal_event_annotate():
    assert ModalEventVocabulary.annotate("exit_modal_canceled") == "SYSTEM_EVENT: exit_modal_canceled"
    out2 = ModalEventVocabulary.annotate("phase_change", from_phase="rapport", to_phase="tutoring")
    assert "SYSTEM_EVENT: phase_change" in out2
    assert "from_phase=rapport" in out2
    assert "to_phase=tutoring" in out2


def test_phase_annotate():
    assert PhaseVocabulary.annotate("tutoring") == "phase=tutoring"
    assert PhaseVocabulary.annotate("not_a_phase") == "phase=tutoring"
