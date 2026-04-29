from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from backend.dependencies import get_graph, get_runtime_store
from backend.models.schemas import ClientMessage
from config import cfg
from conversation.teacher import set_stream_callback, reset_stream_callback

router = APIRouter()


def _extract_pending_choice(state: dict) -> dict | None:
    """
    Return the pending user choice from state, or None.
    Only nodes.py sets pending_user_choice — no fallback derivation here.
    """
    pending = state.get("pending_user_choice")
    if isinstance(pending, dict):
        kind = pending.get("kind")
        options = pending.get("options")
        if kind in {"opt_in", "topic"} and isinstance(options, list) and options:
            return {"kind": kind, "options": [str(x) for x in options]}
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
            token_queue: asyncio.Queue[str | None] = asyncio.Queue()

            def _on_token(text: str) -> None:
                # Cross-thread enqueue. This is safe because Queue is
                # thread-safe via the loop.call_soon_threadsafe shim.
                try:
                    loop.call_soon_threadsafe(token_queue.put_nowait, text)
                except Exception:
                    pass

            stream_token = set_stream_callback(_on_token)

            async def _drain_tokens() -> None:
                """Forward each token to the WS as a partial event.
                Stops when sentinel None arrives in the queue."""
                while True:
                    item = await token_queue.get()
                    if item is None:
                        return
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "token",
                            "content": item,
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
                # Sentinel ends the drain task. Always send it so the
                # task can finish even if graph.invoke raised.
                await token_queue.put(None)
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
            debug_payload["topic_selection"] = str(new_state.get("topic_selection", "") or "")
            debug_payload["locked_question"] = str(new_state.get("locked_question", "") or "")
            debug_payload["locked_answer"] = new_state.get("locked_answer", "")
            debug_payload["answer_locked"] = bool(str(new_state.get("locked_answer", "") or "").strip())
            debug_payload["domain"] = getattr(getattr(cfg, "domain", object()), "short", "")

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
