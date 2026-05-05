from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from backend.dependencies import get_graph, get_runtime_store
from backend.models.schemas import ClientMessage
from config import cfg
from conversation.streaming import (
    reset_activity_callback,
    reset_stream_callback,
    reset_stream_invalidate_callback,
    set_activity_callback,
    set_stream_callback,
    set_stream_invalidate_callback,
)

router = APIRouter()


def _extract_pending_choice(state: dict) -> dict | None:
    """
    Return the pending user choice from state, or None.
    Only graph nodes set pending_user_choice — no fallback derivation here.
    """
    pending = state.get("pending_user_choice")
    if isinstance(pending, dict):
        kind = pending.get("kind")
        options = pending.get("options")
        if kind in {"opt_in", "topic", "confirm_topic"} and isinstance(options, list) and options:
            out = {"kind": kind, "options": [str(x) for x in options]}
            if "allow_custom" in pending:
                out["allow_custom"] = bool(pending.get("allow_custom"))
            if pending.get("end_session_label"):
                out["end_session_label"] = str(pending.get("end_session_label"))
            if pending.get("end_session_value"):
                out["end_session_value"] = str(pending.get("end_session_value"))
            return out
    return None


def _latest_tutor_message(state: dict) -> str:
    for msg in reversed(state.get("messages", [])):
        if msg.get("role") == "tutor":
            return str(msg.get("content", ""))
    return ""


def _append_full_turn_trace(state: dict, student_message: str, tutor_message: str) -> None:
    """
    Persist full per-turn traces for export/debug while keeping turn_trace scoped
    to the current student message.
    """
    debug = state.setdefault("debug", {})
    all_turn_traces = list(debug.get("all_turn_traces", []))
    turn_trace = list(debug.get("turn_trace", []))
    if not turn_trace:
        debug["all_turn_traces"] = all_turn_traces
        return
    all_turn_traces.append(
        {
            "turn": int(state.get("turn_count", 0)),
            "phase": state.get("phase", ""),
            "assessment_turn": int(state.get("assessment_turn", 0) or 0),
            "student_message": student_message,
            "tutor_message": tutor_message,
            "trace": turn_trace,
        }
    )
    debug["all_turn_traces"] = all_turn_traces


@router.websocket("/ws/chat/{thread_id}")
async def chat_ws(websocket: WebSocket, thread_id: str):
    await websocket.accept()
    graph = get_graph()
    runtime = get_runtime_store()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                client_msg = ClientMessage.model_validate_json(raw)
            except Exception:
                await websocket.send_text(
                    json.dumps({"type": "error", "content": "Invalid message payload."})
                )
                continue

            if client_msg.type != "student_message":
                await websocket.send_text(
                    json.dumps({"type": "error", "content": "Unsupported message type."})
                )
                continue

            state = runtime.get(thread_id)
            if state is None:
                await websocket.send_text(
                    json.dumps({"type": "error", "content": "Unknown thread. Start a session first."})
                )
                continue

            # M1 — explicit-exit sentinel from frontend ([End session] button
            # OR exit modal confirm). Stamp exit_intent_pending=True and route
            # straight to memory_update so close fires with reason=exit_intent.
            if client_msg.content == "__exit_session__":
                state["exit_intent_pending"] = True
                state["close_reason"] = "exit_intent"
                state["phase"] = "memory_update"
                # Don't append the sentinel to messages — modal already
                # gave the student visual feedback; transcript stays clean.
                state.setdefault("debug", {}).setdefault("turn_trace", [])
                state["debug"]["turn_trace"] = []
            elif client_msg.content == "__cancel_exit__":
                # BLOCK 9 (S3) — student clicked Cancel on exit modal.
                # Clear exit_intent_pending and stamp cancel_modal_pending
                # so Dean produces a soft_reset bridging message on this
                # invocation. No student message appended (modal lifecycle
                # is invisible to transcript). Log system event for history
                # annotation.
                state["exit_intent_pending"] = False
                state["cancel_modal_pending"] = True
                state["recent_cancel_at_turn"] = int(state.get("turn_count", 0) or 0)
                state.setdefault("debug", {}).setdefault("turn_trace", [])
                state["debug"]["turn_trace"] = []
                from conversation.snapshots import log_system_event
                log_system_event(state, "exit_modal_canceled")
            else:
                messages = list(state.get("messages", []))
                messages.append({"role": "student", "content": client_msg.content})
                state["messages"] = messages
                state.setdefault("debug", {}).setdefault("turn_trace", [])
                state["debug"]["turn_trace"] = []

            # D.6a: install a streaming callback before invoking the
            # graph. The teacher's draft_socratic checks this contextvar
            # and uses Anthropic's streaming API when set, calling the
            # callback with each text delta as the LLM generates. We
            # buffer deltas onto the asyncio event loop and forward via
            # the websocket from this coroutine — sending from inside
            # the sync teacher callback would require run_coroutine_
            # threadsafe across loops, which is fragile.
            #
            # The callback runs on a worker thread (LangGraph schedules
            # sync nodes on a thread pool when graph.ainvoke is used,
            # or directly on the calling thread for graph.invoke). It
            # only enqueues bytes; the WS write happens here in the
            # event loop.
            loop = asyncio.get_running_loop()
            # Queue items are dicts with at minimum a "kind" field. The
            # drain task switches on kind. Possible kinds:
            #   {"kind":"token","text":"..."}     — streaming partial
            #   {"kind":"stream_reset"}           — dean rewrote draft
            #   {"kind":"activity","label":"..."} — backend activity log
            #   {"kind":"end"}                    — finalize sentinel
            # Earlier this used (str|None|"") sentinels; the dict shape
            # is clearer and extends without re-meaning conventions.
            token_queue: asyncio.Queue[dict] = asyncio.Queue()

            def _on_token(text: str) -> None:
                # Cross-thread enqueue. Queue is thread-safe via
                # loop.call_soon_threadsafe.
                try:
                    loop.call_soon_threadsafe(
                        token_queue.put_nowait, {"kind": "token", "text": text}
                    )
                except Exception:
                    pass

            def _on_stream_invalidate() -> None:
                try:
                    loop.call_soon_threadsafe(
                        token_queue.put_nowait, {"kind": "stream_reset"}
                    )
                except Exception:
                    pass

            def _on_activity(label: str, detail: str | None = None) -> None:
                try:
                    loop.call_soon_threadsafe(
                        token_queue.put_nowait,
                        {"kind": "activity", "label": label, "detail": detail or ""},
                    )
                except Exception:
                    pass

            stream_token = set_stream_callback(_on_token)
            invalidate_token = set_stream_invalidate_callback(_on_stream_invalidate)
            activity_token = set_activity_callback(_on_activity)

            async def _drain_tokens() -> None:
                """Forward each queue item to the WS. Stops on the
                "end" sentinel. Each kind maps to a distinct WS event
                type so the frontend can route them independently."""
                while True:
                    item = await token_queue.get()
                    kind = item.get("kind")
                    if kind == "end":
                        return
                    try:
                        if kind == "token":
                            await websocket.send_text(json.dumps({
                                "type": "token",
                                "content": item.get("text", ""),
                            }))
                        elif kind == "stream_reset":
                            await websocket.send_text(json.dumps({
                                "type": "stream_reset",
                            }))
                        elif kind == "activity":
                            await websocket.send_text(json.dumps({
                                "type": "activity",
                                "content": item.get("label", ""),
                                "detail": item.get("detail", ""),
                            }))
                    except Exception:
                        return

            drain_task = asyncio.create_task(_drain_tokens())

            try:
                new_state = await asyncio.to_thread(
                    graph.invoke, state, config
                )
            finally:
                reset_stream_callback(stream_token)
                reset_stream_invalidate_callback(invalidate_token)
                reset_activity_callback(activity_token)
                # Sentinel ends the drain task. Always send it so the
                # task can finish even if graph.invoke raised.
                await token_queue.put({"kind": "end"})
                await drain_task
            tutor_message = _latest_tutor_message(new_state)
            _append_full_turn_trace(new_state, client_msg.content, tutor_message)
            runtime.set(thread_id, new_state)
            debug_payload = dict(new_state.get("debug", {}) or {})
            debug_payload["phase"] = new_state.get("phase")
            debug_payload["turn_count"] = int(new_state.get("turn_count", 0) or 0)
            debug_payload["max_turns"] = int(new_state.get("max_turns", 25) or 25)
            debug_payload["assessment_turn"] = int(new_state.get("assessment_turn", 0) or 0)
            debug_payload["student_state"] = new_state.get("student_state")
            debug_payload["hint_level"] = int(new_state.get("hint_level", 0) or 0)
            debug_payload["max_hints"] = int(new_state.get("max_hints", 3) or 3)
            debug_payload["topic_confirmed"] = bool(new_state.get("topic_confirmed", False))
            debug_payload["prelock_loop_count"] = int(new_state.get("prelock_loop_count", 0) or 0)
            debug_payload["topic_selection"] = str(new_state.get("topic_selection", "") or "")
            # Send the full locked_topic dict so the sidebar can show
            # chapter + section + subsection in the collapsible details.
            debug_payload["locked_topic"] = new_state.get("locked_topic") or None
            debug_payload["locked_question"] = str(new_state.get("locked_question", "") or "")
            debug_payload["locked_answer"] = new_state.get("locked_answer", "")
            debug_payload["answer_locked"] = bool(str(new_state.get("locked_answer", "") or "").strip())
            debug_payload["domain"] = getattr(getattr(cfg, "domain", object()), "short", "")
            # Change 4 / 5.1: surface counters for sidebar debug pills
            debug_payload["help_abuse_count"] = int(new_state.get("help_abuse_count", 0) or 0)
            debug_payload["help_abuse_threshold"] = int(getattr(cfg.dean, "help_abuse_threshold", 4))
            debug_payload["off_topic_count"] = int(new_state.get("off_topic_count", 0) or 0)
            debug_payload["off_topic_threshold"] = int(getattr(cfg.dean, "off_topic_threshold", 4))
            debug_payload["total_low_effort_turns"] = int(new_state.get("total_low_effort_turns", 0) or 0)
            debug_payload["total_off_topic_turns"] = int(new_state.get("total_off_topic_turns", 0) or 0)
            # N2: surface consecutive_low_effort_count for sidebar — was already
            # tracked in state but not exposed in the WS payload.
            debug_payload["consecutive_low_effort_count"] = int(new_state.get("consecutive_low_effort_count", 0) or 0)
            debug_payload["low_effort_threshold"] = 4  # preflight L55 strike-4 force-hint-advance            debug_payload["clinical_low_effort_count"] = int(new_state.get("clinical_low_effort_count", 0) or 0)
            debug_payload["clinical_off_topic_count"] = int(new_state.get("clinical_off_topic_count", 0) or 0)
            debug_payload["clinical_strike_threshold"] = int(getattr(cfg.dean, "clinical_strike_threshold", 2))
            # L80.a — clinical phase turn counter (separate from tutoring's
            # turn_count per L67). Surfaced so the sidebar can render
            # phase-contextual counters during the clinical loop.
            debug_payload["clinical_turn_count"] = int(new_state.get("clinical_turn_count", 0) or 0)
            debug_payload["clinical_max_turns"] = int(new_state.get("clinical_max_turns", 7) or 7)
            # M1 — surface lifecycle flags so the frontend can pop the
            # ExitConfirmModal on deflection and render the session-ended
            # banner on close. Without these fields the WS payload would
            # leave the frontend's exitIntentPending/sessionEnded false
            # even when the backend mutated state.
            debug_payload["exit_intent_pending"] = bool(new_state.get("exit_intent_pending", False))
            debug_payload["session_ended"] = bool(new_state.get("session_ended", False))
            debug_payload["close_reason"] = str(new_state.get("close_reason", "") or "")

            payload = {
                "type": "message_complete",
                "content": tutor_message,
                "pending_choice": _extract_pending_choice(new_state),
                "topic_confirmed": new_state.get("topic_confirmed", False),
                "phase": new_state.get("phase", ""),
                "debug": debug_payload,
            }
            await websocket.send_text(json.dumps(jsonable_encoder(payload)))

    except WebSocketDisconnect:
        return
