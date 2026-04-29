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
