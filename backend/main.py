"""
backend/main.py
----------------
FastAPI app for the Sokratic tutor.

Startup pre-warming
-------------------
On uvicorn boot we eagerly construct the retriever + memory manager +
LangGraph. This pays the ~10-15 s lazy-load cost (chunks JSONL, BM25
pickle, spacy NLP, scikit-learn cross-encoder, mem0 client) ONCE at
server startup rather than on the first /api/session/start request
the user makes from the browser. Net: first page-load feels fast.

Cost: server boot is slower by the same amount, but you only pay it
when restarting uvicorn (not on every page reload). The lifespan
context guarantees the warm-up runs before the server accepts
requests.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from backend.api import chat, mastery, memory, session, users
from backend.dependencies import get_graph, get_memory_manager, get_retriever


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the heavy singletons. Each get_*() is lru_cache'd, so
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(mastery.router, prefix="/api")
app.include_router(chat.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
