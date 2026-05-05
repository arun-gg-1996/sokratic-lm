"""
conversation/streaming.py
─────────────────────────
Streaming + activity callback registry.

When set (typically by the WebSocket handler before graph.invoke),
streaming callbacks pipe Anthropic SDK token deltas through to a sink
(WebSocket, event log, stdout). Activity callbacks emit short
user-facing labels ("Reading your message", "Searching textbook")
that the WS handler forwards to the frontend as activity events,
which renders a Claude-Code-style live log.

Implemented as contextvars (not thread/module globals) so concurrent
sessions under asyncio.gather don't bleed callbacks across each other.
The WS handler sets it on its own task; other tasks see None.

Ported from conversation/teacher.py during D1 (V1 → V2 consolidation).
Behavior preserved verbatim — all callsites that previously imported
these names from `conversation.teacher` now import them from here.
"""
from __future__ import annotations

import contextvars
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────
# Stream token delta callback — invoked from inside Teacher draft with
# each text delta as the LLM streams. After the stream completes the
# Teacher returns the full aggregated text exactly as the non-streaming
# path would; the callback is purely additive.
# ─────────────────────────────────────────────────────────────────────
_stream_callback: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("teacher_stream_callback", default=None)
)

# Companion to _stream_callback. Fired when a streamed first draft is
# rejected and substituted with a revised draft. The WS handler uses
# this to send a stream_reset event so the frontend can clear the
# (now-stale) streaming buffer before the final message_complete event
# arrives. Without this, the user sees content X stream in, then
# abruptly get replaced by content Y in the final bubble.
_stream_invalidate_callback: contextvars.ContextVar[Optional[Callable[[], None]]] = (
    contextvars.ContextVar("teacher_stream_invalidate_callback", default=None)
)

# UX hook for surfacing what the system is doing during a turn. Each
# step (setup, retrieval, classification, draft, QC, memory write)
# fires a short user-facing label via this callback. The WS handler
# forwards these to the frontend as "activity" events, which renders a
# Claude-Code-style live log so the user sees
#   "Reading your message → Searching textbook → Drafting response..."
# instead of an opaque "thinking..." spinner. No-op if no callback
# installed (eval harness, batch scripts).
_activity_callback: contextvars.ContextVar[Optional[Callable[..., None]]] = (
    contextvars.ContextVar("teacher_activity_callback", default=None)
)


# ─────────────────────────────────────────────────────────────────────
# Stream callback API
# ─────────────────────────────────────────────────────────────────────


def set_stream_callback(cb: Optional[Callable[[str], None]]) -> contextvars.Token:
    """Install a per-task streaming callback for Teacher draft. Returns
    a token; pass it to reset_stream_callback() to remove the callback.

    The callback is invoked from inside the Teacher draft with each
    text delta as the LLM streams. After the stream completes the
    Teacher returns the full aggregated text exactly as the
    non-streaming path would; the callback is purely additive — no
    behavior changes for callers that don't read tokens via the callback.
    """
    return _stream_callback.set(cb)


def reset_stream_callback(token: contextvars.Token) -> None:
    _stream_callback.reset(token)


def get_stream_callback() -> Optional[Callable[[str], None]]:
    """Read the currently-installed callback (None if not set).

    Used by Teacher draft to decide between streaming and non-streaming
    code paths. Internal API — most callers should use set/reset.
    """
    return _stream_callback.get()


# ─────────────────────────────────────────────────────────────────────
# Stream-invalidate callback API
# ─────────────────────────────────────────────────────────────────────


def set_stream_invalidate_callback(
    cb: Optional[Callable[[], None]]
) -> contextvars.Token:
    """Companion to set_stream_callback. The dean calls the resulting
    callback when it discards a streamed draft in favor of a revised
    one — the frontend can then clear its streaming buffer cleanly
    instead of showing an abrupt content swap.
    """
    return _stream_invalidate_callback.set(cb)


def reset_stream_invalidate_callback(token: contextvars.Token) -> None:
    _stream_invalidate_callback.reset(token)


def fire_stream_invalidate() -> None:
    """Public hook for non-Teacher modules. Fires the contextvar
    callback if one is installed; no-op otherwise. Safe to call from
    any thread — the underlying callback uses loop.call_soon_threadsafe
    to enqueue.
    """
    cb = _stream_invalidate_callback.get()
    if cb is None:
        return
    try:
        cb()
    except Exception:
        # Mirror set_stream_callback's swallow-all policy: a callback
        # failure must not break the LLM call.
        pass


# ─────────────────────────────────────────────────────────────────────
# Activity callback API
# ─────────────────────────────────────────────────────────────────────


def set_activity_callback(
    cb: Optional[Callable[[str], None]]
) -> contextvars.Token:
    """Install a callback that receives short user-facing activity
    labels (e.g. "Searching textbook", "Reviewing draft for accuracy").
    The WS handler uses this to drive a live activity log in the UI.
    """
    return _activity_callback.set(cb)


def reset_activity_callback(token: contextvars.Token) -> None:
    _activity_callback.reset(token)


def fire_activity(label: str, detail: Optional[str] = None) -> None:
    """Emit a user-facing activity label, optionally with a detail
    tooltip ('why this is happening'). The frontend renders detail as
    a hover tooltip on the activity row, so demo viewers can see the
    system-level reason for a step (e.g. 'Attempt 1 was rejected by
    the leak verifier — locked answer leaked. Teacher is rewriting.').

    No-op when no callback installed (eval harness, batch scripts) so
    call sites can sprinkle these freely without conditional checks.
    Errors swallowed so a bad callback never breaks the actual work.
    """
    cb = _activity_callback.get()
    if cb is None:
        return
    try:
        # Backward compat: callbacks installed pre-2026-05-05 took only
        # `label`. Try the 2-arg form first; fall back to 1-arg if the
        # callback raises TypeError on the extra argument.
        try:
            cb(label, detail)
        except TypeError:
            cb(label)
    except Exception:
        pass
