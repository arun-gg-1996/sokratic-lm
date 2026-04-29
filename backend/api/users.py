"""
backend/api/users.py
--------------------
Hardcoded local user roster for the demo.

student_id contract (DO NOT VIOLATE)
-------------------------------------
Each entry's `id` field IS the student_id used everywhere downstream:
  - mem0/Qdrant user_id namespace (memory isolation per user)
  - LangGraph state["student_id"]
  - GET/DELETE /api/memory/{student_id} routing
  - The frontend's `useUserStore.studentId` is set ONLY from this list
    (via UserPicker → setStudentId(u.id)).

NEVER derive student_id from email, name, or any other field. Two
demo users with the same id share memory — that's the same hazard
real auth has if you key by email instead of a stable user id.

Adding users
------------
Append to USERS with a unique, stable, lowercase slug `id`. Do NOT
reuse old ids — mem0 keys by id, so reusing reattaches stale memory
to the new person.

Future: a "create local account" admin page will append here. Until
then this file is the source of truth.
"""
from fastapi import APIRouter

router = APIRouter()

USERS = [
    {"id": "arun", "display_name": "Arun"},
    {"id": "nidhi", "display_name": "Nidhi"},
]


def known_student_id(student_id: str) -> bool:
    """Returns True if the given id is a known user. Used by the session
    endpoint to reject random / spoofed ids before they create dangling
    mem0 namespaces."""
    return any(u["id"] == student_id for u in USERS)


@router.get("/users")
async def list_users():
    return USERS
