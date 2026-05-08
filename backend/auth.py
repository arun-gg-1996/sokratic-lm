from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

AUTH_USERS_ENV = "SOKRATIC_AUTH_USERS"
AUTH_SECRET_ENV = "SOKRATIC_AUTH_SECRET"
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 14

def _secret() -> bytes:
    raw = os.environ.get(AUTH_SECRET_ENV, "").strip()
    if not raw and configured_passwords():
        raise RuntimeError(f"{AUTH_SECRET_ENV} must be set when demo auth is enabled")
    if not raw:
        raw = "sokratic-local-dev-secret"
    return raw.encode("utf-8")

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))

def configured_passwords() -> dict[str, str]:
    raw = os.environ.get(AUTH_USERS_ENV, "").strip()
    users: dict[str, str] = {}
    for pair in raw.split(","):
        if not pair.strip() or ":" not in pair:
            continue
        username, password = pair.split(":", 1)
        username = username.strip().lower()
        password = password.strip()
        if username and password:
            users[username] = password
    return users

def configured_user_ids() -> list[str]:
    raw = os.environ.get(AUTH_USERS_ENV, "").strip()
    ids: list[str] = []
    seen: set[str] = set()
    for pair in raw.split(","):
        if not pair.strip() or ":" not in pair:
            continue
        username, _password = pair.split(":", 1)
        username = username.strip().lower()
        if username and username not in seen:
            ids.append(username)
            seen.add(username)
    return ids

def create_token(username: str) -> str:
    payload = {
        "sub": username.lower().strip(),
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(sig)}"

def verify_token(token: str) -> str | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload: dict[str, Any] = json.loads(_b64decode(body))
    except Exception:
        return None
    exp = int(payload.get("exp", 0) or 0)
    username = str(payload.get("sub", "") or "").strip().lower()
    if not username or exp < int(time.time()):
        return None
    if username not in configured_passwords():
        return None
    return username

def authenticate(username: str, password: str) -> str | None:
    users = configured_passwords()
    expected = users.get(username.lower().strip())
    if not expected:
        return None
    if not hmac.compare_digest(password, expected):
        return None
    return create_token(username)

def bearer_user(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required")
    username = verify_token(token.strip())
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return username

async def require_api_auth(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and path not in {"/api/auth/login", "/api/users"}:
        try:
            auth_user = bearer_user(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        request.state.auth_user = auth_user
        parts = [p for p in path.split("/") if p]
        forbidden = False
        if len(parts) >= 3 and parts[:2] == ["api", "memory"]:
            forbidden = parts[2] != auth_user
        elif len(parts) >= 3 and parts[:2] == ["api", "students"]:
            forbidden = parts[2] != auth_user
        elif len(parts) >= 3 and parts[:2] == ["api", "session"]:
            forbidden = parts[2] != "start" and not parts[2].startswith(f"{auth_user}_")
        elif len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            forbidden = not parts[2].startswith(f"{auth_user}_")
        elif len(parts) >= 3 and parts[:2] == ["api", "mastery"]:
            if parts[2] == "v2" and len(parts) >= 4:
                if parts[3] == "session" and len(parts) >= 5:
                    forbidden = not parts[4].startswith(f"{auth_user}_")
                else:
                    forbidden = parts[3] != auth_user
            elif parts[2] != "v2":
                forbidden = parts[2] != auth_user
        if forbidden:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)

async def websocket_auth(websocket: WebSocket) -> str | None:
    return verify_token(websocket.query_params.get("token", ""))
