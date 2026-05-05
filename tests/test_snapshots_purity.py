"""
tests/test_snapshots_purity.py
================================
BLOCK 5 verification — Safeguard #1: snapshot + history annotations
must NEVER include locked_answer / full_answer / aliases / chunks.

Tests:
  1. Snapshot helpers reject sensitive keys via _is_sensitive_key
  2. Rendered enriched history with locked_answer="pyruvate" never
     contains "pyruvate"
  3. System events with sensitive payload keys get them stripped
"""
from __future__ import annotations

from conversation.snapshots import (
    _is_sensitive_key,
    log_system_event,
    snapshot_student_turn,
    snapshot_tutor_turn,
)
from conversation.history_render import render_history


def test_sensitive_key_detection():
    sensitive = [
        "locked_answer", "full_answer", "locked_answer_aliases",
        "answer", "answers", "chunks", "retrieved_chunks",
        "answer_text", "correct_answer", "target_answer",
        "some_chunk", "subsection_chunk",
        "user_answer", "true_answer",
    ]
    for k in sensitive:
        assert _is_sensitive_key(k), f"{k!r} should be sensitive"

    safe = [
        "intent", "mode", "tone", "hint_level", "phase", "turn_index",
        "consecutive_low_effort", "help_abuse_count", "off_topic_count",
        "attempts", "evidence", "tutor_role", "from_phase", "to_phase",
    ]
    for k in safe:
        assert not _is_sensitive_key(k), f"{k!r} should be safe"


def test_student_snapshot_strips_sensitive_extras():
    state = {
        "messages": [{"role": "student", "content": "idk"}],
        "hint_level": 1,
        "consecutive_low_effort_count": 2,
        "help_abuse_count": 0,
        "off_topic_count": 0,
        "phase": "tutoring",
    }
    snapshot_student_turn(
        state,
        intent="low_effort",
        intent_evidence="idk",
        extras={
            "locked_answer": "pyruvate",      # SENSITIVE — must be stripped
            "full_answer": "the full answer", # SENSITIVE — must be stripped
            "consecutive_low_effort": 3,      # safe — kept
            "user_chunk": "chunk content",    # SENSITIVE — must be stripped
        },
    )
    snap = state["debug"]["per_turn_snapshots"][0]
    assert "locked_answer" not in snap
    assert "full_answer" not in snap
    assert "user_chunk" not in snap
    # Safe fields survive
    assert snap.get("intent") == "low_effort"
    assert "consecutive_low_effort" in snap
    # Sensitive content never appears in snapshot values either
    serialized = repr(snap)
    assert "pyruvate" not in serialized
    assert "the full answer" not in serialized
    assert "chunk content" not in serialized


def test_tutor_snapshot_strips_sensitive_extras():
    state = {
        "messages": [{"role": "tutor", "content": "What do you think?"}],
        "hint_level": 1,
        "phase": "tutoring",
    }
    snapshot_tutor_turn(
        state,
        mode="socratic",
        tone="encouraging",
        attempts=2,
        extras={
            "locked_answer": "pyruvate",
            "answer": "pyruvate",
            "chunk_count": 7,  # SENSITIVE — has 'chunk' in name
            "tone_override": "neutral",  # safe
        },
    )
    snap = state["debug"]["per_turn_snapshots"][0]
    assert "locked_answer" not in snap
    assert "answer" not in snap
    assert "chunk_count" not in snap
    assert snap.get("mode") == "socratic"
    serialized = repr(snap)
    assert "pyruvate" not in serialized


def test_system_event_strips_sensitive_payload():
    state = {"messages": [{"role": "tutor", "content": "test"}]}
    log_system_event(
        state,
        "topic_locked",
        subsection="Test Sub",       # safe
        locked_answer="pyruvate",    # SENSITIVE
        chunk_count=7,               # SENSITIVE (has 'chunk')
        chapter_num=3,               # safe
    )
    ev = state["debug"]["system_events"][0]
    payload = ev["payload"]
    assert "locked_answer" not in payload
    assert "chunk_count" not in payload
    assert payload.get("subsection") == "Test Sub"
    assert payload.get("chapter_num") == 3
    serialized = repr(ev)
    assert "pyruvate" not in serialized


def test_render_history_with_sensitive_state_no_leak():
    """End-to-end: render history with state where locked_answer='pyruvate'.

    Even if some metadata field somehow held 'pyruvate', the renderer
    should not emit it (because snapshots/events are pure by design,
    and the renderer doesn't read locked_answer directly).
    """
    messages = [
        {"role": "tutor", "content": "What is the end product?"},
        {"role": "student", "content": "I don't know"},
    ]
    snapshots = [
        {
            "turn_index": 0,
            "role": "tutor",
            "mode": "socratic",
            "tone": "encouraging",
            "attempts": 1,
            "hint_level": 0,
            "phase": "tutoring",
        },
        {
            "turn_index": 1,
            "role": "student",
            "intent": "low_effort",
            "consecutive_low_effort": 1,
            "phase": "tutoring",
            "intent_evidence": "I don't know",
        },
    ]
    rendered = render_history(messages, snapshots=snapshots, events=[])
    # The forbidden answer string must NEVER appear in the render
    assert "pyruvate" not in rendered.lower()
    # Annotations should appear
    assert "[mode=socratic" in rendered
    assert "[intent=low_effort" in rendered


def test_render_history_falls_back_when_no_snapshots():
    """Legacy calls without snapshots should still work."""
    messages = [
        {"role": "tutor", "content": "What is X?"},
        {"role": "student", "content": "not sure"},
    ]
    rendered = render_history(messages)
    assert "TUTOR: What is X?" in rendered
    assert "STUDENT: not sure" in rendered
    # No annotations rendered when no snapshots
    assert "[" not in rendered  # no [intent=...] or [mode=...] markers


def test_render_history_with_system_event():
    """System events render as inline SYSTEM_EVENT lines."""
    messages = [
        {"role": "tutor", "content": "Picking up where we left off."},
        {"role": "student", "content": "ok"},
    ]
    events = [
        {
            "after_turn": 0,
            "kind": "topic_locked",
            "payload": {"subsection": "Test Sub", "chapter_num": 3},
        },
    ]
    rendered = render_history(messages, snapshots=[], events=events)
    assert "SYSTEM_EVENT: topic_locked" in rendered
    assert "subsection=Test Sub" in rendered


def test_intent_evidence_truncated():
    """Long intent evidence is capped at 120 chars (no answer leak via evidence)."""
    state = {
        "messages": [{"role": "student", "content": "a very long message"}],
        "hint_level": 0,
        "phase": "tutoring",
    }
    long_evidence = "x" * 500
    snapshot_student_turn(state, intent="on_topic_engaged", intent_evidence=long_evidence)
    snap = state["debug"]["per_turn_snapshots"][0]
    assert len(snap["intent_evidence"]) <= 120
