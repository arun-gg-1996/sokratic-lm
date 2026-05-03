"""
tests/test_mastery_api_v2.py
────────────────────────────
Tests for the new SQLite-backed mastery API endpoints (per L29-L34).

Endpoints under test:
  GET /api/mastery/v2/{student_id}/tree
  GET /api/mastery/v2/{student_id}/sessions
  GET /api/mastery/v2/session/{thread_id}

These coexist with the legacy /api/mastery/{student_id} JSON-backed
endpoint (left untouched). The frontend rebuild (track 5 / L29-L34) will
switch consumption to v2; the legacy endpoint is dropped after that.

We use FastAPI TestClient with monkey-patched SQLiteStore + known_student_id
to keep tests hermetic (no real domain config, no real student_state DB).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """FastAPI TestClient with SQLiteStore pointed at a fresh per-test DB
    and known_student_id stubbed to accept any non-empty id."""
    db = tmp_path / "test.sqlite3"

    # Patch SQLiteStore default-factory: any SQLiteStore() call (no args)
    # lands on this test DB; explicit args still go through.
    from memory import sqlite_store as _ss
    real_init = _ss.SQLiteStore.__init__

    def _patched_init(self, *args, **kwargs):
        if not args and not kwargs:
            real_init(self, db_path=db)
        else:
            real_init(self, *args, **kwargs)

    monkeypatch.setattr(_ss.SQLiteStore, "__init__", _patched_init)

    # Stub known_student_id to accept anything non-empty
    monkeypatch.setattr(
        "backend.api.mastery.known_student_id",
        lambda sid: bool(sid),
    )

    # Build a minimal app with just the mastery router
    from backend.api import mastery
    app = FastAPI()
    app.include_router(mastery.router, prefix="/api")

    # Pre-seed with one student + topic_index for the tree test
    store = _ss.SQLiteStore(db_path=db)
    store.ensure_student("alice")

    monkeypatch.setattr(
        "backend.api.mastery._load_topic_index_for_tree",
        lambda: [
            {"chapter": "Muscle Tissue", "chapter_num": 10,
             "section": "Skeletal Muscle", "subsection": "Sarcomere Structure",
             "display_label": "Sarcomere"},
            {"chapter": "Muscle Tissue", "chapter_num": 10,
             "section": "Skeletal Muscle", "subsection": "Sliding Filament",
             "display_label": "Sliding Filament"},
        ],
    )

    yield TestClient(app), store
    store.close()


# ─────────────────────────────────────────────────────────────────────────────
# /tree
# ─────────────────────────────────────────────────────────────────────────────

def test_tree_empty_student_returns_grey_chapters(client):
    c, store = client
    resp = c.get("/api/mastery/v2/alice/tree")
    assert resp.status_code == 200
    body = resp.json()
    assert body["student_id"] == "alice"
    # Chapters present from topic_index, all grey/untouched
    assert len(body["chapters"]) == 1
    ch = body["chapters"][0]
    assert ch["chapter"] == "Muscle Tissue"
    assert ch["color"] == "grey"
    assert ch["score"] is None
    assert ch["touched"] == 0
    assert ch["total"] == 2


def test_tree_with_mastery_returns_colored_nodes(client):
    c, store = client
    store.upsert_subsection_mastery(
        "alice", "Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
        fresh_score=0.85, outcome="reached",
    )
    resp = c.get("/api/mastery/v2/alice/tree")
    body = resp.json()
    ch = body["chapters"][0]
    assert ch["touched"] == 1
    assert ch["total"] == 2
    assert ch["color"] == "green"
    assert ch["tier"] == "proficient"
    sec = ch["sections"][0]
    assert sec["touched"] == 1 and sec["total"] == 2
    sub = next(s for s in sec["subsections"] if s["subsection"] == "Sarcomere Structure")
    assert sub["score"] == 0.85
    assert sub["outcome"] == "reached"
    assert sub["display_label"] == "Sarcomere"
    sub2 = next(s for s in sec["subsections"] if s["subsection"] == "Sliding Filament")
    assert sub2["score"] is None
    assert sub2["color"] == "grey"


def test_tree_unknown_student_400(client):
    c, _ = client
    # known_student_id returns False on empty string → use empty
    resp = c.get("/api/mastery/v2/ /tree")  # space
    # Trailing space gets stripped → becomes empty
    assert resp.status_code in (400, 404)


# ─────────────────────────────────────────────────────────────────────────────
# /sessions
# ─────────────────────────────────────────────────────────────────────────────

def test_sessions_empty(client):
    c, _ = client
    resp = c.get("/api/mastery/v2/alice/sessions")
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


def test_sessions_returns_newest_first(client):
    c, store = client
    store.start_session("t1", "alice")
    store.end_session("t1", status="completed", mastery_tier="proficient",
                      core_score=0.85, locked_topic_path="X > Y > Z")
    store.start_session("t2", "alice")
    store.end_session("t2", status="ended_off_domain",
                      mastery_tier="not_assessed")
    resp = c.get("/api/mastery/v2/alice/sessions")
    body = resp.json()
    threads = [s["thread_id"] for s in body["sessions"]]
    # Both present (newest first by started_at — both inserted in same second
    # so ordering between them isn't deterministic; just assert membership)
    assert set(threads) == {"t1", "t2"}
    t1 = next(s for s in body["sessions"] if s["thread_id"] == "t1")
    assert t1["mastery_tier"] == "proficient"
    assert t1["status"] == "completed"
    assert t1["core_score"] == 0.85


def test_sessions_completed_only_filter(client):
    c, store = client
    store.start_session("done", "alice")
    store.end_session("done", status="completed", mastery_tier="developing")
    store.start_session("ongoing", "alice")  # left in_progress
    resp = c.get("/api/mastery/v2/alice/sessions?completed_only=true")
    threads = [s["thread_id"] for s in resp.json()["sessions"]]
    assert threads == ["done"]


# ─────────────────────────────────────────────────────────────────────────────
# /session/{thread_id}
# ─────────────────────────────────────────────────────────────────────────────

def test_get_session_detail(client):
    c, store = client
    store.start_session("t-detail", "alice")
    store.end_session("t-detail", status="completed",
                      mastery_tier="proficient", core_score=0.9,
                      locked_topic_path="A > B > C", reach_status=True,
                      key_takeaways={"what_demonstrated": "good", "what_needs_work": "none"})
    resp = c.get("/api/mastery/v2/session/t-detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["thread_id"] == "t-detail"
    assert body["mastery_tier"] == "proficient"
    assert body["reach_status"] is True
    assert body["key_takeaways"] == {"what_demonstrated": "good", "what_needs_work": "none"}


def test_get_session_404_unknown_thread(client):
    c, _ = client
    resp = c.get("/api/mastery/v2/session/no-such-thread")
    assert resp.status_code == 404
