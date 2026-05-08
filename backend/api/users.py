"""
User roster + login endpoint.

The user list is read from the SOKRATIC_AUTH_USERS env var (a comma-
separated list of `username:password` pairs). The username is the
stable id used as the mem0 namespace, the LangGraph student_id, and
the path parameter on /api/memory/{student_id}; never derive it from
email or display name, since two users sharing an id would also share
memory.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.auth import authenticate, configured_user_ids

router = APIRouter()

class LoginRequest(BaseModel):
    username: str
    password: str

def configured_users() -> list[dict[str, str]]:
    return [
        {"id": uid, "display_name": uid.replace("_", " ").title()}
        for uid in configured_user_ids()
    ]

def known_student_id(student_id: str) -> bool:
    """Returns True if the given id is a known user. Used by the session
 endpoint to reject random / spoofed ids before they create dangling
 mem0 namespaces."""
    return student_id in set(configured_user_ids())

@router.get("/users")
async def list_users():
    return configured_users()

@router.post("/auth/login")
async def login(req: LoginRequest):
    username = (req.username or "").strip().lower()
    token = authenticate(username, req.password or "")
    if not token or not known_student_id(username):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    user = next(u for u in configured_users() if u["id"] == username)
    return {"token": token, "user": user}
