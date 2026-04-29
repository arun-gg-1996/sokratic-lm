from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from backend.api.users import known_student_id
from backend.dependencies import get_graph, get_memory_manager, get_runtime_store
from backend.models.schemas import (
    StartSessionRequest,
    StartSessionResponse,
    StudentOverviewResponse,
)
from config import cfg
from conversation.state import initial_state

router = APIRouter()


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
    graph = get_graph()
    runtime = get_runtime_store()

    state = initial_state(sid, cfg)
    # Apply per-session memory toggle from the request. Default True is set
    # by initial_state; we only override when the client passes False so
    # demo mode can show fresh-student behavior on demand.
    state["memory_enabled"] = bool(req.memory_enabled)
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

    runtime.set(thread_id, state)
    greeting = _latest_tutor_message(state)
    debug_payload = dict(state.get("debug", {}) or {})
    debug_payload["phase"] = state.get("phase")
    debug_payload["turn_count"] = int(state.get("turn_count", 0) or 0)
    debug_payload["max_turns"] = int(state.get("max_turns", 25) or 25)
    debug_payload["assessment_turn"] = int(state.get("assessment_turn", 0) or 0)
    debug_payload["student_state"] = state.get("student_state")
    debug_payload["hint_level"] = int(state.get("hint_level", 0) or 0)
    debug_payload["max_hints"] = int(state.get("max_hints", 3) or 3)
    debug_payload["topic_confirmed"] = bool(state.get("topic_confirmed", False))
    debug_payload["topic_selection"] = str(state.get("topic_selection", "") or "")
    debug_payload["locked_question"] = str(state.get("locked_question", "") or "")
    debug_payload["locked_answer"] = state.get("locked_answer", "")
    debug_payload["answer_locked"] = bool(str(state.get("locked_answer", "") or "").strip())
    debug_payload["domain"] = getattr(getattr(cfg, "domain", object()), "short", "")
    return StartSessionResponse(thread_id=thread_id, initial_message=greeting, initial_debug=debug_payload)


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
