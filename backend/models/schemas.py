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


class StartSessionResponse(BaseModel):
    thread_id: str
    initial_message: str
    initial_debug: dict[str, Any] | None = None


class PendingChoice(BaseModel):
    kind: Literal["opt_in", "topic"]
    options: list[str]


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
