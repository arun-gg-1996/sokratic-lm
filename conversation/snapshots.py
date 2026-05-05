"""
conversation/snapshots.py
==========================
BLOCK 5 (REAL-Q5) — per-turn snapshot + system event helpers.

Writes structured records to `state["debug"]["per_turn_snapshots"]`
and `state["debug"]["system_events"]` so the LLM can SEE system
provenance in conversation history (via _format_history rendering).

Snapshot layout:

  per_turn_snapshots: [
    {
      "turn_index": 0,            # index into state["messages"]
      "role": "student" | "tutor",
      # Student-turn fields (None for tutor):
      "intent": "low_effort",
      "intent_evidence": "idk",
      # Tutor-turn fields (None for student):
      "mode": "redirect",
      "tone": "neutral",
      "attempts": 1,
      # Counter snapshot (both roles):
      "hint_level": 1,
      "consecutive_low_effort": 2,
      "help_abuse_count": 1,
      "off_topic_count": 0,
      "phase": "tutoring",
    },
    ...
  ]

  system_events: [
    {
      "after_turn": 3,             # event fired right after this message index
      "kind": "anchor_pick_shown",
      "payload": {"options_count": 3},
    },
    ...
  ]

SAFETY CONTRACT (Safeguard #1): snapshot fields must NEVER include
locked_answer / full_answer / aliases / chunk text. Only generic
counters, intent verdicts, mode names, etc.
Enforced by tests/test_snapshots_purity.py.
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
    """Snapshot generic counters that apply to both student and tutor turns."""
    return {
        "hint_level": int(state.get("hint_level", 0) or 0),
        "consecutive_low_effort": int(state.get("consecutive_low_effort_count", 0) or 0),
        "help_abuse_count": int(state.get("help_abuse_count", 0) or 0),
        "off_topic_count": int(state.get("off_topic_count", 0) or 0),
        "phase": str(state.get("phase", "") or ""),
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
      log_system_event(state, "phase_change",
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
