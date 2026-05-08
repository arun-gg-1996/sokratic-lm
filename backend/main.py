"""
FastAPI entry point for the Sokratic tutor.

Loads the retriever, memory manager, and LangGraph once at startup so
the chunks JSONL, BM25 index, spaCy, cross-encoder, and mem0 client
don't lazy-load on the first request. Wires the auth middleware, the
REST routers, and the chat WebSocket.
"""
from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from backend.auth import require_api_auth
from backend.api import chat, mastery, memory, session, sessions, users, vlm
from backend.dependencies import get_graph, get_memory_manager, get_retriever

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the heavy singletons. Each get_* is lru_cache'd, so
    # calling them here primes the cache and the first request hits
    # the warm path. Order matters: retriever loads chunks; then the
    # graph reads the retriever; mem0 manager attaches to Qdrant.
    print("[startup] warming retriever, memory manager, graph...", flush=True)
    get_retriever()
    get_memory_manager()
    get_graph()
    print("[startup] warm-up complete; ready to serve.", flush=True)
    yield
    # No teardown needed — Python process exit cleans connections.

app = FastAPI(title="Sokratic Backend", version="0.1", lifespan=lifespan)

cors_origins_raw = os.environ.get("SOKRATIC_CORS_ORIGINS", "").strip()
if not cors_origins_raw:
    cors_origins_raw = "http://localhost:5173"
cors_origins = [
    origin.strip()
    for origin in cors_origins_raw.split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(require_api_auth)

app.include_router(users.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(mastery.router, prefix="/api")
# analysis view (transcript + analysis chat + regenerate takeaways)
app.include_router(sessions.router, prefix="/api")
# VLM upload endpoint (gated by cfg.domain.vlm.enabled).
app.include_router(vlm.router)
app.include_router(chat.router)

@app.get("/health")
async def health():
    return {"status": "ok"}
