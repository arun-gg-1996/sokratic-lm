from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from backend.api.users import known_student_id
from backend.dependencies import (
    get_dean,
    get_graph,
    get_memory_manager,
    get_runtime_store,
)
from backend.models.schemas import (
    StartSessionRequest,
    StartSessionResponse,
    StudentOverviewResponse,
)
from config import cfg
from conversation.state import initial_state
from retrieval.topic_matcher import get_topic_matcher

router = APIRouter()


def _apply_prelock(state: dict, path: str) -> None:
    """Pre-fill state with a TOC-locked topic, skipping the dean's
    free-text topic resolution. Called at session-start when the
    frontend's Revisit button knows the exact subsection.

    The path is the SAME format the dean writes to
    state["locked_topic"]["path"] and the mastery store uses:
        "Ch{N}|{section_title}|{subsection_title}"
    So we parse it directly here rather than looking up via the
    topic matcher (which uses a different "Chapter N: ... > ..."
    path format derived from textbook_structure.json).

    Steps:
      1. Parse the path → chapter_num, section, subsection.
      2. Reject if the format is wrong (raises — caller falls
         back to normal flow).
      3. Populate state["locked_topic"] / topic_confirmed /
         topic_selection in the same shape post-resolution does.
      4. Eagerly run dean._retrieve_on_topic_lock(state) so
         retrieved_chunks is populated. If the subsection has no
         chunks (bad path), this fails the coverage gate
         downstream — caller's except block catches and falls
         back to free-text resolution.
      5. Eagerly run dean._lock_anchors_call(state) so
         locked_question + locked_answer are set before the first
         student message.
      6. Stamp state["debug"]["locked_topic_snapshot"] — the sticky
         record memory_update_node + memory_manager fall back to
         when state["locked_topic"] gets cleared by partial-merge
         artifacts later.

    Raises any error encountered. The caller in start_session
    catches and falls back to the normal free-text flow.
    """
    parts = path.split("|", 2)
    if len(parts) != 3 or not parts[0].startswith("Ch") or not parts[0][2:].isdigit():
        raise ValueError(f"prelocked_topic path malformed: {path!r}")
    chapter_num = int(parts[0][2:])
    section = parts[1].strip()
    subsection = parts[2].strip()
    if not subsection:
        raise ValueError(f"prelocked_topic missing subsection: {path!r}")

    # Construct locked_topic in the same shape post-lock writes. The
    # `chapter` field is conventionally the chapter title — we don't
    # have it from the path alone, so we use the section as a
    # human-readable stand-in (matches what the dean does when its
    # vote-based resolver also lacks chapter_title metadata).
    state["locked_topic"] = {
        "path": path,
        "chapter": section,    # best-effort; real chapter title not in the path
        "section": section,
        "subsection": subsection,
        "difficulty": "moderate",
        "chunk_count": 0,       # dean's coverage gate populates this from real retrieval
        "limited": False,
        "score": 1.0,
    }
    state["topic_confirmed"] = True
    state["topic_selection"] = subsection
    state["topic_options"] = []
    state["topic_question"] = ""
    state["pending_user_choice"] = {}

    # Sticky snapshot. memory_manager._topic_metadata and
    # memory_update_node both fall back to this when
    # state["locked_topic"] is None at session-end.
    state.setdefault("debug", {})
    state["debug"]["locked_topic_snapshot"] = dict(state["locked_topic"])

    # Eagerly retrieve + lock anchors so the first tutoring turn has
    # everything ready. Both are idempotent on state — they read
    # state["locked_topic"] and write retrieved_chunks /
    # locked_question / locked_answer. Failures here propagate up to
    # the caller's except block which falls back to free-text flow.
    dean = get_dean()
    dean._retrieve_on_topic_lock(state)
    if not state.get("retrieved_chunks"):
        raise ValueError(
            f"prelocked_topic {path!r} returned 0 chunks — bad path or corpus mismatch"
        )
    anchors = dean._lock_anchors_call(state)
    state["locked_question"] = str(anchors.get("locked_question", "") or "").strip()
    state["locked_answer"] = str(anchors.get("locked_answer", "") or "").strip()
    raw_aliases_pre = anchors.get("locked_answer_aliases", []) or []
    state["locked_answer_aliases"] = (
        [str(a) for a in raw_aliases_pre if isinstance(a, str) and str(a).strip()]
        if isinstance(raw_aliases_pre, list) else []
    )
    # Two-tier (Change 2026-04-30): full_answer for grading layer.
    state["full_answer"] = str(anchors.get("full_answer", "") or "").strip() or state["locked_answer"]
    if not state["locked_question"] or not state["locked_answer"]:
        raise ValueError(
            f"prelocked_topic {path!r} anchor lock returned empty question/answer"
        )
    # Change 2 (2026-04-29): mark topic as just-locked so the FIRST
    # dean_node call (after rapport's greeting) emits a deterministic
    # ack message stating the question, instead of jumping into a
    # paraphrased hint. The dean's ack-emit branch consumes this flag.
    state["topic_just_locked"] = True
    state["debug"].setdefault("turn_trace", []).append({
        "wrapper": "session.prelock_applied",
        "path": path,
        "subsection": subsection,
        "anchor_set": True,
        "chunk_count": len(state.get("retrieved_chunks") or []),
    })


def _latest_tutor_message(state: dict) -> str:
    for msg in reversed(state.get("messages", [])):
        if msg.get("role") == "tutor":
            return str(msg.get("content", ""))
    return ""


def _strip_internal(value: Any) -> Any:
    """Recursively drop underscore-prefixed internal debug/runtime fields."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and key.startswith("_"):
                continue
            cleaned[key] = _strip_internal(val)
        return cleaned
    if isinstance(value, list):
        return [_strip_internal(v) for v in value]
    return value


@router.post("/session/start", response_model=StartSessionResponse)
async def start_session(req: StartSessionRequest):
    # Defensive: reject empty / whitespace / unknown student_ids before
    # they reach mem0 and create a dangling namespace. The frontend
    # always sets student_id from listUsers().id, so a value here that
    # isn't in USERS means a client bug or a tampered request — both
    # worth a 400 rather than silently mingling memory under a bogus key.
    sid = (req.student_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="student_id required")
    if not known_student_id(sid):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown student_id: {sid!r}. Valid ids come from "
                "GET /api/users."
            ),
        )

    thread_id = f"{sid}_{uuid.uuid4().hex[:8]}"
    # L21: insert session row at session start (status='in_progress',
    # ended_at=NULL). Best-effort — never block session creation on a
    # SQLite failure. The row gets UPDATEd at session end by
    # memory_update_node with the final status + mastery breakdown.
    try:
        from memory.sqlite_store import SQLiteStore
        SQLiteStore().start_session(thread_id, sid)
    except Exception:
        pass

    graph = get_graph()
    runtime = get_runtime_store()

    state = initial_state(sid, cfg)
    # Stash thread_id on state so downstream nodes (memory_update_node)
    # can update the L21 SQLite session row at end without needing the
    # LangGraph RunnableConfig threaded through.
    state["thread_id"] = thread_id

    # Apply per-session memory toggle from the request. Default True is set
    # by initial_state; we only override when the client passes False so
    # demo mode can show fresh-student behavior on demand.
    state["memory_enabled"] = bool(req.memory_enabled)
    # D.6b-5: stash the client's local hour on state so rapport_node can
    # pass it to draft_rapport. None falls through to server-time.
    state["client_hour"] = req.client_hour if req.client_hour is not None else None

    # Revisit pre-lock: when the frontend's "Revisit" button knows the
    # exact subsection the user wants, skip the dean's free-text topic
    # resolution entirely. Pre-fill state with the locked topic, then
    # eagerly call dean._retrieve_on_topic_lock and _lock_anchors_call
    # so the anchor question is ready before the first student message.
    # On any failure we fall through to the normal flow — the user
    # types and the dean resolves topic the usual way.
    prelocked = (req.prelocked_topic or "").strip()
    if prelocked:
        try:
            _apply_prelock(state, prelocked)
        except Exception:
            # Defensive: if prelock fails (bad path, retrieval error,
            # anchor LLM failure), wipe the half-applied state and
            # fall back. The user just gets a normal session.
            state["topic_confirmed"] = False
            state["locked_topic"] = None
            state["topic_selection"] = ""
            state["locked_question"] = ""
            state["locked_answer"] = ""

    config = {"configurable": {"thread_id": thread_id}}
    state = graph.invoke(state, config=config)
    state.setdefault("debug", {}).setdefault("all_turn_traces", [])
    rapport_trace = list(state.get("debug", {}).get("turn_trace", []))
    if rapport_trace:
        state["debug"]["all_turn_traces"].append(
            {
                "turn": 0,
                "phase": state.get("phase", "rapport"),
                "assessment_turn": int(state.get("assessment_turn", 0) or 0),
                "student_message": "",
                "tutor_message": _latest_tutor_message(state),
                "trace": rapport_trace,
            }
        )

    # Change 2 (2026-04-29): for prelocked sessions, build the dean's
    # topic-acknowledgement message inline and send it back to the
    # client as a second tutor message. This avoids the prior workaround
    # of having the frontend auto-send "Let's begin..." just to trigger
    # the dean → ack flow. We append the ack to state["messages"] AND
    # return it as `initial_topic_ack` so the frontend can render it as
    # a distinct second message. The flag is consumed here so the
    # student's first real message goes through the normal teacher flow.
    initial_topic_ack: str | None = None
    if state.get("topic_just_locked", False):
        try:
            initial_topic_ack = dean._build_topic_ack_message(state)
            if initial_topic_ack:
                state["messages"].append({
                    "role": "tutor",
                    "content": initial_topic_ack,
                    "phase": "tutoring",
                })
                state["topic_just_locked"] = False
                state["debug"].setdefault("turn_trace", []).append({
                    "wrapper": "session.topic_ack_inline",
                    "result": "ack_emitted_during_bootstrap",
                })
        except Exception as e:
            # Defensive: if ack-build fails for any reason, fall through
            # to the legacy auto-send path. The state still has
            # topic_just_locked=True so the next dean_node call will
            # fire the ack on the student's first message.
            state["debug"].setdefault("turn_trace", []).append({
                "wrapper": "session.topic_ack_inline_failed",
                "error": str(e)[:200],
            })
            initial_topic_ack = None

    runtime.set(thread_id, state)
    greeting = _latest_tutor_message(state)
    # _latest_tutor_message returns the LAST tutor msg — for prelocked
    # sessions where we just appended the ack, that'd be the ack and
    # the rapport greeting would be lost from initial_message. Fix by
    # walking back: greeting is the EARLIEST rapport message, ack is
    # what we appended (if any).
    if initial_topic_ack:
        # Find the rapport greeting (the tutor message before the ack).
        for _msg in state.get("messages", [])[:-1]:
            if (_msg or {}).get("role") == "tutor":
                greeting = str(_msg.get("content", "") or "")
                break
    debug_payload = dict(state.get("debug", {}) or {})
    debug_payload["phase"] = state.get("phase")
    debug_payload["turn_count"] = int(state.get("turn_count", 0) or 0)
    debug_payload["max_turns"] = int(state.get("max_turns", 25) or 25)
    debug_payload["assessment_turn"] = int(state.get("assessment_turn", 0) or 0)
    debug_payload["student_state"] = state.get("student_state")
    debug_payload["hint_level"] = int(state.get("hint_level", 0) or 0)
    debug_payload["max_hints"] = int(state.get("max_hints", 3) or 3)
    debug_payload["topic_confirmed"] = bool(state.get("topic_confirmed", False))
    debug_payload["prelock_loop_count"] = int(state.get("prelock_loop_count", 0) or 0)
    debug_payload["topic_selection"] = str(state.get("topic_selection", "") or "")
    # Send the full locked_topic dict so the sidebar can show chapter +
    # section + subsection in the collapsible details (Phase 1, 2026-04-30).
    debug_payload["locked_topic"] = state.get("locked_topic") or None
    debug_payload["locked_question"] = str(state.get("locked_question", "") or "")
    debug_payload["locked_answer"] = state.get("locked_answer", "")
    debug_payload["answer_locked"] = bool(str(state.get("locked_answer", "") or "").strip())
    debug_payload["domain"] = getattr(getattr(cfg, "domain", object()), "short", "")
    # Change 4 / 5.1: surface counters for sidebar debug pills
    debug_payload["help_abuse_count"] = int(state.get("help_abuse_count", 0) or 0)
    debug_payload["help_abuse_threshold"] = int(getattr(cfg.dean, "help_abuse_threshold", 4))
    debug_payload["off_topic_count"] = int(state.get("off_topic_count", 0) or 0)
    debug_payload["off_topic_threshold"] = int(getattr(cfg.dean, "off_topic_threshold", 4))
    debug_payload["total_low_effort_turns"] = int(state.get("total_low_effort_turns", 0) or 0)
    debug_payload["total_off_topic_turns"] = int(state.get("total_off_topic_turns", 0) or 0)
    debug_payload["clinical_low_effort_count"] = int(state.get("clinical_low_effort_count", 0) or 0)
    debug_payload["clinical_off_topic_count"] = int(state.get("clinical_off_topic_count", 0) or 0)
    debug_payload["clinical_strike_threshold"] = int(getattr(cfg.dean, "clinical_strike_threshold", 2))
    return StartSessionResponse(
        thread_id=thread_id,
        initial_message=greeting,
        initial_topic_ack=initial_topic_ack,
        initial_debug=debug_payload,
    )


@router.get("/session/{thread_id}/state")
async def get_state(thread_id: str):
    runtime = get_runtime_store()
    state = runtime.get(thread_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown thread_id")
    return {"values": jsonable_encoder(state)}


@router.get("/session/{thread_id}/export")
async def export_state(thread_id: str):
    runtime = get_runtime_store()
    state = runtime.get(thread_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown thread_id")

    debug_clean = _strip_internal(state.get("debug", {}))
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "thread_id": thread_id,
        "student_id": state.get("student_id"),
        "domain": {
            "name": getattr(getattr(cfg, "domain", object()), "name", ""),
            "short": getattr(getattr(cfg, "domain", object()), "short", ""),
            "retrieval_domain": getattr(getattr(cfg, "domain", object()), "retrieval_domain", ""),
            "kb_collection": getattr(getattr(cfg, "domain", object()), "kb_collection", ""),
        },
        "phase": state.get("phase"),
        "locked_question": state.get("locked_question", ""),
        "locked_answer": state.get("locked_answer", ""),
        "messages": state.get("messages", []),
        "debug": debug_clean,
        "all_turn_traces": debug_clean.get("all_turn_traces", []),
        "current_turn_trace": debug_clean.get("turn_trace", []),
        "retrieved_chunks": state.get("retrieved_chunks", []),
        "weak_topics": state.get("weak_topics", []),
        "core_mastery_tier": state.get("core_mastery_tier"),
        "clinical_mastery_tier": state.get("clinical_mastery_tier"),
        "mastery_tier": state.get("mastery_tier"),
        "grading_rationale": state.get("grading_rationale", ""),
        "session_memory_summary": state.get("session_memory_summary", ""),
    }
    return jsonable_encoder(payload)


@router.get("/students/{student_id}/overview", response_model=StudentOverviewResponse)
async def get_student_overview(student_id: str):
    memory_manager = get_memory_manager()
    weak_topics = memory_manager.load(student_id)
    return StudentOverviewResponse(
        student_id=student_id,
        weak_topics=weak_topics,
        strong_topics=[],
    )
