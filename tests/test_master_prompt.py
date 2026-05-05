"""
tests/test_master_prompt.py
============================
BLOCK 2 verification — master system prompt purity + structure.

Safeguard #3: master prompt must reference state fields by NAME never
by VALUE. No locked_answer, full_answer, or topic-specific text.
"""
from __future__ import annotations

from conversation.master_prompt import build_master_prompt, _MASTER_SYSTEM_PROMPT


# Topic-specific terms that must NEVER appear in master prompt.
# Same list as registry purity test — extended with answer values.
FORBIDDEN_TERMS = [
    # Anatomy / biology terms
    "thyroid", "glycolysis", "pyruvate", "metabolic hormones",
    "serous", "pleural", "pericardium", "visceral", "parietal",
    "skeletal muscle", "phospholipid", "cytoplasm",
    "cushion and reduce friction", "endomembrane",
    # Specific answer-revealing patterns
    "the answer is", "locked_answer is", "answer for this session",
    # Sample student-id / personal info that shouldn't leak
    "nidhi", "arun",
]


def test_master_prompt_purity_no_topic_terms():
    """Master prompt must contain no topic-specific text (Safeguard #3)."""
    rendered = build_master_prompt(domain_name="Human Anatomy & Physiology").lower()
    violations = [t for t in FORBIDDEN_TERMS if t.lower() in rendered]
    assert not violations, (
        f"Master prompt contains forbidden terms: {violations}\n"
        f"Master prompt should reference fields by NAME never by VALUE."
    )


def test_master_prompt_references_state_fields_by_name():
    """Master prompt must explain state field NAMES (not their values)."""
    rendered = build_master_prompt()
    required_field_names = [
        "locked_subsection", "locked_question", "locked_answer",
        "hint_level", "max_hints",
        "help_abuse_count", "off_topic_count", "consecutive_low_effort",
        "turn_count", "max_turns", "phase",
        "clinical_opt_in", "clinical_turn_count",
        "exit_intent_pending", "cancel_modal_pending", "session_ended",
    ]
    missing = [f for f in required_field_names if f not in rendered]
    assert not missing, f"Master prompt missing required field references: {missing}"


def test_master_prompt_describes_4_phases():
    """All 4 conversation phases must be described in master prompt."""
    rendered = build_master_prompt().upper()
    for phase in ("RAPPORT", "TUTORING", "ASSESSMENT", "MEMORY_UPDATE"):
        assert phase in rendered, f"Master prompt missing phase: {phase}"


def test_master_prompt_describes_4_agents():
    """The 4 agent roles must be described in master prompt."""
    rendered = build_master_prompt().upper()
    for agent in ("PREFLIGHT", "DEAN", "TEACHER", "VERIFIER QUARTET"):
        assert agent in rendered, f"Master prompt missing agent role: {agent}"


def test_master_prompt_contains_safety_contracts():
    """The 5 safety contracts must be stated in master prompt."""
    rendered = build_master_prompt().lower()
    contracts = [
        "never reveal the locked_answer",
        "letter",  # part of leak hint forbids
        "empty praise",
        "exactly one question",
        "never repeat",
    ]
    missing = [c for c in contracts if c not in rendered]
    assert not missing, f"Master prompt missing safety contracts: {missing}"


def test_master_prompt_includes_all_vocabulary_blocks():
    """All 6 registry vocabularies must appear in the rendered master prompt."""
    rendered = build_master_prompt()
    headers = [
        "INTENT VERDICTS",
        "TEACHER MODES",
        "TONE TIERS",
        "CONVERSATION PHASES",
        "SYSTEM EVENTS",
        "HINT TRANSITIONS",
    ]
    missing = [h for h in headers if h not in rendered]
    assert not missing, f"Master prompt missing vocabulary blocks: {missing}"


def test_master_prompt_deterministic():
    """Repeated builds with same args must return byte-identical output."""
    first = build_master_prompt("Human Anatomy & Physiology")
    for _ in range(5):
        assert build_master_prompt("Human Anatomy & Physiology") == first


def test_master_prompt_template_has_no_topic_terms():
    """The static template (before format-substitution) must also be pure."""
    template_lower = _MASTER_SYSTEM_PROMPT.lower()
    violations = [t for t in FORBIDDEN_TERMS if t.lower() in template_lower]
    assert not violations, (
        f"Master prompt template contains forbidden terms: {violations}"
    )
