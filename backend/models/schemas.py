from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class StartSessionRequest(BaseModel):
    student_id: str
    # Optional toggle. When False, rapport_node skips memory_manager.load
    # and the session opens as a fresh-student greeting regardless of
    # whether the student has prior mem0 entries. Used by the demo UI's
    # "Memory enabled" switch — defaults to True for the normal experience.
    memory_enabled: bool = True
    # D.6b-5: client-local hour (0-23) so the rapport greeting picks
    # morning/afternoon/evening from the USER's clock, not the server's.
    # Required for any deployment where the FastAPI process isn't in
    # the user's tz. Falls back to server-time if omitted (legacy clients
    # / curl tests). Range-validated downstream.
    client_hour: int | None = None
    # Pre-lock the session to a specific TOC subsection, skipping the
    # dean's free-text topic-resolution flow. Set when the user clicks
    # "Revisit" on a /mastery card or sessions log entry — we already
    # know which subsection they want, so running topic resolution
    # again risks mis-locking on a short query (the bug seen on 4/29
    # where "Conduction System of the Heart" mis-resolved to "Aging
    # and Muscle Tissue"). Format: "ChN|section|subsection" — same
    # path the mastery store uses. Backend looks up the parts and
    # pre-fills state["locked_topic"], state["topic_confirmed"]=True,
    # then eagerly calls dean._retrieve_on_topic_lock and
    # dean._lock_anchors_call so the anchor question is ready before
    # the first student message.
    prelocked_topic: str | None = None
    # L77 — VLM JSON from a prior /api/vlm/upload call. When present,
    # the backend stashes it on state["image_context"] AND uses
    # `description` as the first student message (auto-routed through
    # the v2 topic mapper) so the session opens with image-driven
    # topic resolution. Schema mirrors vlm/extract.py output:
    #   {identified_structures: [...], image_type, description,
    #    best_topic_guess, confidence}
    image_context: dict | None = None


class StartSessionResponse(BaseModel):
    thread_id: str
    initial_message: str
    # Set ONLY for pre-locked (revisit) sessions. When the topic was
    # pre-locked via _apply_prelock, the backend builds the dean's
    # topic-acknowledgement message inline so the user lands directly
    # on "Got it — let's work on X. Question?" instead of having to
    # send a kickstarter message. Frontend renders it as a second
    # tutor turn right after the rapport greeting.
    initial_topic_ack: str | None = None
    initial_debug: dict[str, Any] | None = None
    # M4 — initial pending_user_choice (anchor_pick cards). Set when
    # _apply_prelock generated anchor variations. Frontend reads this
    # and renders the cards immediately after rapport.
    initial_pending_choice: dict | None = None


class PendingChoice(BaseModel):
    kind: Literal["opt_in", "topic", "confirm_topic", "anchor_pick"]
    options: list[str]
    allow_custom: bool | None = None
    end_session_label: str | None = None
    end_session_value: str | None = None


class ServerMessage(BaseModel):
    type: Literal["message_complete", "error"]
    content: str | None = None
    pending_choice: PendingChoice | None = None
    topic_confirmed: bool | None = None
    phase: str | None = None
    debug: dict[str, Any] | None = None


class ClientMessage(BaseModel):
    type: Literal["student_message"]
    content: str


class UserSummary(BaseModel):
    id: str
    display_name: str


class StudentOverviewResponse(BaseModel):
    student_id: str
    weak_topics: list[dict[str, Any]]
    strong_topics: list[dict[str, Any]]
