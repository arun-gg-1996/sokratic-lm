"""
conversation/mem0_inject.py
───────────────────────────
Track 4.7f: implicit mem0 read injection points (L6 #1 and #2).

Per L6, mem0 is queried at TWO deterministic points and the result is
injected into Dean's plan() call as `carryover_notes`. Dean then decides
whether to surface them in TurnPlan.carryover_notes (which Teacher reads).

  Injection #1 — At topic-lock time (once per session):
    query = locked_question + " " + subsection
    filter = {category in (misconception, learning_style),
              subsection_path = locked.path}
    top_k = 2
    Used by: topic_lock_v2._lock_topic (after anchors lock succeeds)
    Stored on: state["mem0_carryover_notes"] (consumed by next dean.plan)

  Injection #2 — At hint-advance time (every level bump 1→2 or 2→3):
    query = locked_question + " what worked when stuck"
    filter = {category = learning_style}
    top_k = 1
    Used by: dean_node_v2 when hint_level just advanced
    Combined with any existing topic-lock carryover and passed to dean.plan

Both injections use safe_mem0_read so they NEVER raise — empty results
mean Teacher continues with anchor + history context only.
"""
from __future__ import annotations

from typing import Any, Optional

from memory.mem0_safe import safe_mem0_read


# Cap any single carryover string at this length to keep prompts bounded.
# 2 misconception cues + 1 style cue × ~150 chars each ≈ 450 chars max
# expected, but a few outlier mem0 entries can be longer.
MAX_CARRYOVER_CHARS = 800


def read_topic_lock_carryover(
    state: dict,
    persistent: Any,
    locked_topic: dict,
) -> str:
    """L6 injection #1 — fired ONCE per session right after topic locks.

    Returns a formatted carryover_notes string (possibly empty). Caller
    is expected to stash on state['mem0_carryover_notes'] so the next
    dean.plan() picks it up.
    """
    if persistent is None:
        return ""
    student_id = str(state.get("student_id", "") or "")
    if not student_id:
        return ""

    locked_path = str((locked_topic or {}).get("path", "") or "")
    locked_question = str(state.get("locked_question", "") or "")
    subsection = str((locked_topic or {}).get("subsection", "") or "")
    if not locked_path:
        return ""

    query_terms = " ".join([t for t in (locked_question, subsection) if t])
    if not query_terms:
        return ""

    filters = {
        "category": ["misconception", "learning_style"],
        "subsection_path": locked_path,
    }
    hits = safe_mem0_read(
        persistent,
        student_id=student_id,
        query=query_terms,
        filters=filters,
        top_k=2,
        state=state,
    )

    return _format_hits(
        hits,
        header="PRIOR-SESSION CONTEXT (from mem0 — use sparingly):",
        category_labels={
            "misconception": "Past misconception",
            "learning_style": "Learning-style cue",
        },
    )


def read_hint_advance_carryover(
    state: dict,
    persistent: Any,
    locked_topic: dict,
) -> str:
    """L6 injection #2 — fired on EVERY hint-advance (1→2 or 2→3).

    Returns a formatted style-cue string (possibly empty). Caller passes
    to dean.plan(carryover_notes=...) for THIS planning call only — does
    not need to be stashed on state.
    """
    if persistent is None:
        return ""
    student_id = str(state.get("student_id", "") or "")
    if not student_id:
        return ""

    locked_question = str(state.get("locked_question", "") or "")
    if not locked_question:
        return ""

    query = f"{locked_question} what worked when stuck"
    filters = {"category": "learning_style"}
    hits = safe_mem0_read(
        persistent,
        student_id=student_id,
        query=query,
        filters=filters,
        top_k=1,
        state=state,
    )

    return _format_hits(
        hits,
        header="STYLE CUE (what worked for this student before — adapt the next hint):",
        category_labels={"learning_style": "Style cue"},
    )


def combine_carryover(*parts: str) -> str:
    """Stack multiple carryover blocks; drop empties; clip overall length."""
    blocks = [p.strip() for p in parts if (p or "").strip()]
    if not blocks:
        return ""
    out = "\n\n".join(blocks)
    if len(out) > MAX_CARRYOVER_CHARS:
        out = out[:MAX_CARRYOVER_CHARS - 3] + "..."
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


def _format_hits(
    hits: list[dict],
    *,
    header: str,
    category_labels: dict[str, str],
) -> str:
    """Render mem0 hits as a compact text block. Empty hits → empty string.

    Each hit becomes a line:
      - <CategoryLabel>: <text>

    Truncates each hit to 200 chars to stay within MAX_CARRYOVER_CHARS.
    """
    if not hits:
        return ""
    lines: list[str] = [header]
    for h in hits:
        if not isinstance(h, dict):
            continue
        text = str(h.get("text") or h.get("memory") or "").strip()
        if not text:
            continue
        if len(text) > 200:
            text = text[:197] + "..."
        meta = h.get("metadata") if isinstance(h.get("metadata"), dict) else {}
        category = str(meta.get("category", "") or "").strip()
        label = category_labels.get(category) or category.replace("_", " ").title() or "Note"
        lines.append(f"  - {label}: {text}")
    if len(lines) == 1:
        # Header only, no actual hits — return empty to keep prompts clean.
        return ""
    return "\n".join(lines)
