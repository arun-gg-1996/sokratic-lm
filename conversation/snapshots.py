"""
Per-turn snapshot + system-event helpers.

Each turn writes a small record into
`state["debug"]["per_turn_snapshots"]` and `state["debug"]["system_events"]`
so the LLM can see system provenance in the rendered conversation
history (turn index, intent, mode, tone, counter values). System
events are sparse markers like `topic_locked`, `anchor_pick_shown`,
or `exit_modal_canceled`.

Snapshots NEVER include answer content (locked_answer, full_answer,
aliases, chunk text). The purity contract is enforced by
tests/test_snapshots_purity.py.
"""
from __future__ import annotations

from typing import Any

def _ensure_debug_lists(state: dict) -> tuple[list[dict], list[dict]]:
    """Ensure state.debug has the snapshot + event lists. Returns refs."""
    debug = state.setdefault("debug", {})
    snapshots = debug.setdefault("per_turn_snapshots", [])
    events = debug.setdefault("system_events", [])
    return snapshots, events

def _common_counter_snapshot(state: dict) -> dict[str, Any]:
    """Snapshot generic counters that apply to both student and tutor turns.

 : clinical-phase counters AND
 cumulative totals were missing from per-turn snapshots, so the
 JSON export couldn't show whether they ticked. Surfaces all six
 consecutive + cumulative fields plus the clinical mirror set.
"""
    return {
        "hint_level": int(state.get("hint_level", 0) or 0),
        "consecutive_low_effort": int(state.get("consecutive_low_effort_count", 0) or 0),
        "help_abuse_count": int(state.get("help_abuse_count", 0) or 0),
        "off_topic_count": int(state.get("off_topic_count", 0) or 0),
        # cumulative (non-resetting) totals
        "total_low_effort_turns": int(state.get("total_low_effort_turns", 0) or 0),
        "total_off_topic_turns": int(state.get("total_off_topic_turns", 0) or 0),
        "total_help_abuse_turns": int(state.get("total_help_abuse_turns", 0) or 0),
        # clinical-phase mirror counters
        "clinical_help_abuse_count": int(state.get("clinical_help_abuse_count", 0) or 0),
        "clinical_off_topic_count": int(state.get("clinical_off_topic_count", 0) or 0),
        "clinical_low_effort_count": int(state.get("clinical_low_effort_count", 0) or 0),
        "total_clinical_help_abuse_turns": int(state.get("total_clinical_help_abuse_turns", 0) or 0),
        "total_clinical_off_topic_turns": int(state.get("total_clinical_off_topic_turns", 0) or 0),
        "total_clinical_low_effort_turns": int(state.get("total_clinical_low_effort_turns", 0) or 0),
        "phase": str(state.get("phase", "") or ""),
        "assessment_turn": int(state.get("assessment_turn", 0) or 0),
    }

def snapshot_student_turn(
    state: dict,
    *,
    intent: str = "on_topic_engaged",
    intent_evidence: str = "",
    extras: dict[str, Any] | None = None,
) -> None:
    """Record a snapshot for the latest student turn.

 Call this AFTER preflight runs (when intent is known) and AFTER
 counters have been updated for this turn.
"""
    snapshots, _ = _ensure_debug_lists(state)
    msgs = state.get("messages", []) or []
    # The student turn we're snapshotting is the latest message
    turn_index = len(msgs) - 1
    if turn_index < 0 or msgs[turn_index].get("role") != "student":
        # Defensive: only snapshot when latest msg is student
        return
    snap: dict[str, Any] = {
        "turn_index": turn_index,
        "role": "student",
        "intent": str(intent or "on_topic_engaged"),
        "intent_evidence": str(intent_evidence or "")[:120],
    }
    snap.update(_common_counter_snapshot(state))
    if extras:
        snap.update({k: v for k, v in extras.items() if not _is_sensitive_key(k)})
    snapshots.append(snap)

def snapshot_tutor_turn(
    state: dict,
    *,
    mode: str = "socratic",
    tone: str = "neutral",
    attempts: int = 1,
    extras: dict[str, Any] | None = None,
) -> None:
    """Record a snapshot for the latest tutor turn.

 Call this AFTER Teacher draft + verifier complete and the tutor
 message has been appended to state["messages"].
"""
    snapshots, _ = _ensure_debug_lists(state)
    msgs = state.get("messages", []) or []
    turn_index = len(msgs) - 1
    if turn_index < 0 or msgs[turn_index].get("role") != "tutor":
        return
    snap: dict[str, Any] = {
        "turn_index": turn_index,
        "role": "tutor",
        "mode": str(mode or "socratic"),
        "tone": str(tone or "neutral"),
        "attempts": int(attempts or 1),
    }
    snap.update(_common_counter_snapshot(state))
    if extras:
        snap.update({k: v for k, v in extras.items() if not _is_sensitive_key(k)})
    snapshots.append(snap)

def log_system_event(
    state: dict,
    kind: str,
    **payload: Any,
) -> None:
    """Record a system event that occurred between turns.

 Examples:
 log_system_event(state, "anchor_pick_shown", options_count=3)
 log_system_event(state, "exit_modal_canceled")
 log_system_event(state, "phase_change"
 from_phase="rapport", to_phase="tutoring")
"""
    _, events = _ensure_debug_lists(state)
    msgs = state.get("messages", []) or []
    after_turn = len(msgs) - 1
    safe_payload = {k: v for k, v in payload.items() if not _is_sensitive_key(k)}
    events.append({
        "after_turn": after_turn,
        "kind": str(kind),
        "payload": safe_payload,
    })

# ---------------------------------------------------------------------------
# Safety: keys that must NEVER appear in snapshots or event payloads
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = frozenset({
    "locked_answer", "full_answer", "locked_answer_aliases",
    "answer", "answers", "chunks", "retrieved_chunks",
    # explicit stems that might leak content
    "answer_text", "correct_answer", "target_answer",
})

def _is_sensitive_key(key: str) -> bool:
    """Check if a key name suggests it carries answer/chunk content."""
    if not isinstance(key, str):
        return True  # defensive: reject non-string keys
    k = key.lower()
    if k in _SENSITIVE_KEYS:
        return True
    # Heuristic: any key ending in _answer or containing 'chunk'
    if k.endswith("_answer") or k.endswith("answer_text"):
        return True
    if "chunk" in k:
        return True
    return False

def get_snapshots(state: dict) -> list[dict]:
    """Read snapshots (returns empty list if none)."""
    return list((state.get("debug") or {}).get("per_turn_snapshots", []) or [])

def get_system_events(state: dict) -> list[dict]:
    """Read system events (returns empty list if none)."""
    return list((state.get("debug") or {}).get("system_events", []) or [])
