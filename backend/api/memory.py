"""
backend/api/memory.py
---------------------
Endpoints for inspecting and clearing a student's cross-session memory.

Why these exist
---------------
The Sokratic tutor persists session highlights to mem0 (Qdrant) so a
returning student gets a personalized rapport message that may reference
one prior topic or open thread. To make that loop *demoable* and
*controllable* in the UI, the frontend needs to:

  1. Show the user what the tutor remembers about them
     → GET /api/memory/{student_id}
  2. Let the user wipe their own memory ("forget me")
     → DELETE /api/memory/{student_id}

These endpoints intentionally operate on a SINGLE student. The global
"clear everything" path (memory_manager.clear_namespace) is reserved
for eval scripts and is NOT exposed here, because surfacing it to the
UI would let one user delete everyone's memories.

student_id authorization
------------------------
At this stage we trust the path parameter — there's no auth layer yet.
When auth lands, this should pivot to using the authenticated user_id
from the request session and ignore any path override that doesn't
match. See plan: "Pin student_id derivation rule: student_id =
auth_user_id only".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.users import known_student_id
from backend.dependencies import get_memory_manager
from memory.memory_manager import MemoryManager

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryEntry(BaseModel):
    id: str | None = None
    text: str
    created_at: str | None = None
    score: float | None = None
    # Structured metadata payload (the same dict mem0 stores in the
    # Qdrant entry's payload — see memory/memory_manager.py:_topic_metadata).
    # Used by the frontend to GROUP entries by session and label them
    # by category. Empty dict on legacy pre-metadata entries.
    metadata: dict = {}


class MemoryListResponse(BaseModel):
    student_id: str
    available: bool
    count: int
    entries: list[MemoryEntry]


class MemoryDeleteResponse(BaseModel):
    student_id: str
    deleted: int
    available: bool


def _normalize_entry(raw: dict) -> MemoryEntry:
    """mem0 result dicts vary slightly between versions/fields. Coerce to
    a stable shape the frontend can render."""
    text = (
        raw.get("memory")
        or raw.get("data")
        or raw.get("text")
        or ""
    )
    meta = raw.get("metadata") or {}
    return MemoryEntry(
        id=str(raw.get("id")) if raw.get("id") is not None else None,
        text=str(text),
        created_at=raw.get("created_at"),
        score=raw.get("score"),
        metadata=meta if isinstance(meta, dict) else {},
    )


@router.get("/{student_id}", response_model=MemoryListResponse)
async def list_memories(
    student_id: str,
    mm: MemoryManager = Depends(get_memory_manager),
) -> MemoryListResponse:
    """Return all memories the tutor has for this student.

    Returns an empty list (not a 404) for new students — that's the
    expected case and the frontend treats it as "no history yet".

    If mem0/Qdrant is unavailable, returns available=False with an
    empty entries list rather than 5xx — the rest of the app still
    works, the panel just shows nothing.
    """
    sid = (student_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="student_id required")
    if not known_student_id(sid):
        raise HTTPException(
            status_code=400,
            detail=f"unknown student_id: {sid!r}",
        )

    available = bool(mm.persistent.available)
    if not available:
        return MemoryListResponse(
            student_id=sid, available=False, count=0, entries=[]
        )

    try:
        raw = mm.load(
            sid,
            query="topics covered, misconceptions, and outcomes from past sessions",
        )
    except Exception:
        raw = []
    entries = [_normalize_entry(r) for r in (raw or []) if isinstance(r, dict)]
    return MemoryListResponse(
        student_id=sid,
        available=True,
        count=len(entries),
        entries=entries,
    )


@router.delete("/{student_id}", response_model=MemoryDeleteResponse)
async def forget_memories(
    student_id: str,
    mm: MemoryManager = Depends(get_memory_manager),
) -> MemoryDeleteResponse:
    """Delete all mem0 entries for a single student.

    Per-user only — does NOT touch other students' memories. This is the
    "Forget me" / privacy-reset action.

    Returns the number of memories that were present before the delete
    (best-effort — mem0's pre-delete count is what we report). 0 means
    the student had no history to wipe; -1 means mem0/Qdrant was
    unavailable and nothing happened.
    """
    sid = (student_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="student_id required")
    if not known_student_id(sid):
        raise HTTPException(
            status_code=400,
            detail=f"unknown student_id: {sid!r}",
        )

    available = bool(mm.persistent.available)
    if not available:
        return MemoryDeleteResponse(
            student_id=sid, deleted=0, available=False
        )

    deleted = mm.forget(sid)
    return MemoryDeleteResponse(
        student_id=sid, deleted=int(deleted), available=True
    )
