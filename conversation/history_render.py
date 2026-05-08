"""
Renders conversation history for the LLM, weaving in per-turn
snapshots and system events.

Output looks like:

    TUTOR [mode=socratic, hint=1, tone=encouraging]: <text>
    STUDENT [intent=on_topic_engaged]: <text>
    SYSTEM_EVENT: hint_advance, from=1, to=2
    TUTOR [mode=redirect, hint=2, tone=neutral]: <text>

The renderer only reads safe snapshot keys, so answer / chunk content
never leaks into the formatted history. When snapshots are missing
it falls back to plain TUTOR / STUDENT lines.
"""
from __future__ import annotations

from typing import Any

from conversation.registry import (
    IntentVocabulary,
    ModalEventVocabulary,
    TeacherModeVocabulary,
)

def render_history(
    messages: list[dict],
    *,
    snapshots: list[dict] | None = None,
    events: list[dict] | None = None,
    max_turns: int = 50,
) -> str:
    """Render conversation history with optional system-state annotations.

 Args:
 messages: state["messages"] — raw conversation
 snapshots: state["debug"]["per_turn_snapshots"] — per-turn metadata
 events: state["debug"]["system_events"] — system events between turns
 max_turns: max student/tutor pairs to render (default 50)

 Returns:
 Multi-line string with annotated history (or plain history if
 no snapshots provided).
"""
    if not messages:
        return "(no history)"

    snapshots = snapshots or []
    events = events or []

    # Index snapshots by turn_index for fast lookup
    snap_by_idx: dict[int, dict] = {}
    for s in snapshots:
        idx = s.get("turn_index")
        if isinstance(idx, int):
            snap_by_idx[idx] = s

    # Index events by after_turn for sequential rendering
    events_by_after: dict[int, list[dict]] = {}
    for e in events:
        after = e.get("after_turn")
        if isinstance(after, int):
            events_by_after.setdefault(after, []).append(e)

    # Take last max_turns × 2 messages (student + tutor)
    cap = max_turns * 2
    if len(messages) > cap:
        # When truncating, also need to adjust which snapshots we render
        # (since their turn_index is absolute over messages)
        # We'll just emit the tail and snapshots whose turn_index lies in
        # the tail window.
        start_idx = len(messages) - cap
    else:
        start_idx = 0
    tail = messages[start_idx:]

    out_lines: list[str] = []
    # /C1: emit pre-conversation system events (after_turn = -1) BEFORE
    # the first message. anchor_pick_shown fires before any tutor turn —
    # and might accuse the student of "skipping the question" when they
    # clicked a chip.
    if start_idx == 0:
        for ev in events_by_after.get(-1, []):
            ev_line = _render_event_line(ev)
            if ev_line:
                out_lines.append(ev_line)
    for offset, m in enumerate(tail):
        absolute_idx = start_idx + offset
        role = m.get("role") or "?"
        content = (m.get("content") or "").strip()
        if not content and role != "system":
            continue
        snap = snap_by_idx.get(absolute_idx)
        line = _render_message_line(role, content, snap)
        if line:
            out_lines.append(line)
        # Emit any system events that fired AFTER this message
        for ev in events_by_after.get(absolute_idx, []):
            ev_line = _render_event_line(ev)
            if ev_line:
                out_lines.append(ev_line)

    return "\n\n".join(out_lines) or "(no history)"

def _render_message_line(
    role: str,
    content: str,
    snap: dict | None,
) -> str:
    """Render one tutor or student message with optional annotation."""
    if role == "tutor":
        prefix = "TUTOR"
        if snap:
            extras: dict[str, Any] = {}
            if "hint_level" in snap:
                extras["hint"] = snap["hint_level"]
            if "tone" in snap:
                extras["tone"] = snap["tone"]
            if "attempts" in snap:
                extras["attempts"] = snap["attempts"]
            ann = TeacherModeVocabulary.annotate(snap.get("mode", "socratic"), **extras)
            prefix = f"TUTOR {ann}"
        return f"{prefix}: {content}"
    elif role == "student":
        prefix = "STUDENT"
        if snap:
            extras = {}
            consecutive = snap.get("consecutive_low_effort", 0)
            if consecutive and consecutive >= 2:
                extras["consecutive_low_effort"] = consecutive
            help_count = snap.get("help_abuse_count", 0)
            if help_count and help_count >= 1:
                extras["help_abuse_count"] = help_count
            off_count = snap.get("off_topic_count", 0)
            if off_count and off_count >= 1:
                extras["off_topic_count"] = off_count
            ann = IntentVocabulary.annotate(snap.get("intent", "on_topic_engaged"), **extras)
            prefix = f"STUDENT {ann}"
        return f"{prefix}: {content}"
    elif role == "system":
        # System role messages (rare — used for error_card payloads)
        return f"SYSTEM: {content}" if content else ""
    else:
        return f"{role.upper()}: {content}"

def _render_event_line(ev: dict) -> str:
    """Render a system event line."""
    kind = ev.get("kind", "")
    payload = ev.get("payload") or {}
    if not kind:
        return ""
    return ModalEventVocabulary.annotate(kind, **payload)
