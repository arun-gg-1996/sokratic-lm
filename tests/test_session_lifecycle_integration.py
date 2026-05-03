"""
tests/test_session_lifecycle_integration.py
───────────────────────────────────────────
End-to-end tests for the L21 session-lifecycle hooks wired in track 1.8:

  * SQLite session row INSERTed at session start (start_session via the
    backend session.py endpoint — exercised here at the helper level).
  * SQLite session row UPDATEd + subsection_mastery upserted at
    memory_update_node end (via _persist_session_end_to_sqlite).

These tests bypass the LangGraph runtime + LLM and exercise the data
layer directly with synthetic state, so they're hermetic and fast.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memory.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path):
    s = SQLiteStore(db_path=tmp_path / "test.sqlite3")
    yield s
    s.close()


def test_full_lifecycle_completed_session(store, monkeypatch, tmp_path):
    """Reach=True session: insert at start, end with status='completed',
    mastery_tier derived from judgment, subsection_mastery upserted."""
    # Patch the helper's SQLiteStore() default-factory to land on our test DB
    from conversation import nodes as _nodes
    from memory import sqlite_store as _ss
    monkeypatch.setattr(
        _ss.SQLiteStore, "__init__",
        lambda self, *a, **kw: _ss.SQLiteStore.__init__.__wrapped__(
            self, db_path=tmp_path / "test.sqlite3"
        ) if False else None,
    )
    # Simpler: directly invoke the SQLite path.
    store.start_session("t1", "alice")

    # Simulate end-of-session state
    state = {
        "thread_id": "t1",
        "student_id": "alice",
        "locked_topic": {"path": "Muscle Tissue > Skeletal Muscle > Sarcomere Structure"},
        "locked_question": "What contracts in a sarcomere?",
        "locked_answer": "actin and myosin",
        "full_answer": "Actin thin filaments slide past myosin thick filaments...",
        "student_reached_answer": True,
        "student_reach_coverage": 1.0,
        "hint_level": 1,
        "messages": [
            {"role": "tutor", "content": "Good morning"},
            {"role": "student", "content": "ready"},
            {"role": "tutor", "content": "Q1?"},
            {"role": "student", "content": "answer"},
        ],
        "debug": {"turn_trace": []},
    }
    judgment = {"mastery": 0.85, "confidence": 0.7, "rationale": "Solid answer"}

    # Monkey-patch the helper's SQLiteStore() to point at our test DB
    monkeypatch.setattr(
        "memory.sqlite_store.SQLiteStore",
        lambda *a, **kw: store if not (a or kw) else _ss.SQLiteStore(*a, **kw),
    )

    status = _nodes._persist_session_end_to_sqlite(state, judgment)
    assert "ok status=completed" in status, status

    row = store.get_session("t1")
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    assert row["locked_topic_path"] == "Muscle Tissue > Skeletal Muscle > Sarcomere Structure"
    assert row["locked_question"] == "What contracts in a sarcomere?"
    assert row["reach_status"] == 1
    assert row["mastery_tier"] == "proficient"
    assert row["core_score"] == 0.85
    assert row["clinical_mastery_tier"] == "not_assessed"
    assert row["clinical_score"] is None
    assert row["turn_count"] == 2  # student messages

    mastery = store.get_subsection_mastery(
        "alice", "Muscle Tissue > Skeletal Muscle > Sarcomere Structure"
    )
    assert mastery is not None
    assert mastery["ewma_score"] == 0.85
    assert mastery["last_outcome"] == "reached"
    assert mastery["attempt_count"] == 1


def test_full_lifecycle_pre_lock_termination(store, monkeypatch, tmp_path):
    """No locked topic → status='abandoned_no_lock', no mastery row written."""
    from conversation import nodes as _nodes
    from memory import sqlite_store as _ss
    store.start_session("t2", "bob")

    state = {
        "thread_id": "t2",
        "student_id": "bob",
        "locked_topic": {},
        "messages": [{"role": "tutor", "content": "Good morning"}],
        "debug": {"turn_trace": []},
    }

    monkeypatch.setattr(
        "memory.sqlite_store.SQLiteStore",
        lambda *a, **kw: store if not (a or kw) else _ss.SQLiteStore(*a, **kw),
    )

    status = _nodes._persist_session_end_to_sqlite(state, judgment=None)
    assert "abandoned_no_lock" in status, status

    row = store.get_session("t2")
    assert row["status"] == "abandoned_no_lock"
    assert row["ended_at"] is not None
    assert row["locked_topic_path"] is None
    assert row["mastery_tier"] == "not_assessed"
    assert store.list_subsection_mastery("bob") == []


def test_full_lifecycle_legacy_path_normalized(store, monkeypatch, tmp_path):
    """Legacy 'Ch20|Section|Subsection' path gets normalized to canonical
    'Full Title > Section > Subsection' before SQLite write."""
    from conversation import nodes as _nodes
    from memory import sqlite_store as _ss
    store.start_session("t3", "carol")

    # Force chapter lookup so test doesn't depend on real textbook_structure.json
    monkeypatch.setattr(
        _ss, "_CHAPTER_LOOKUP_CACHE",
        {20: "The Cardiovascular System: Blood Vessels and Circulation"},
    )

    state = {
        "thread_id": "t3",
        "student_id": "carol",
        "locked_topic": {"path": "Ch20|Capillary Exchange|Bulk Flow"},
        "student_reached_answer": False,
        "hint_level": 2,
        "messages": [
            {"role": "student", "content": "..."} for _ in range(8)
        ],
        "debug": {"turn_trace": []},
    }
    judgment = {"mastery": 0.45, "confidence": 0.5}

    monkeypatch.setattr(
        "memory.sqlite_store.SQLiteStore",
        lambda *a, **kw: store if not (a or kw) else _ss.SQLiteStore(*a, **kw),
    )

    status = _nodes._persist_session_end_to_sqlite(state, judgment)
    assert "ok" in status

    row = store.get_session("t3")
    expected_path = (
        "The Cardiovascular System: Blood Vessels and Circulation > "
        "Capillary Exchange > Bulk Flow"
    )
    assert row["locked_topic_path"] == expected_path
    assert row["mastery_tier"] == "needs_review"  # 0.45 < 0.50

    # subsection_mastery row keyed on the canonical path
    mastery = store.get_subsection_mastery("carol", expected_path)
    assert mastery is not None
    assert mastery["last_outcome"] == "not_reached"


def test_helper_handles_missing_thread_id_gracefully(store, monkeypatch):
    """Defensive path: state with no thread_id should report a clear status,
    not raise."""
    from conversation import nodes as _nodes
    state = {"student_id": "x", "messages": [], "debug": {}}
    status = _nodes._persist_session_end_to_sqlite(state, judgment=None)
    assert "skipped" in status


def test_dual_write_does_not_corrupt_on_sqlite_error(monkeypatch):
    """If SQLite raises mid-write, helper returns an error string but the
    caller (memory_update_node) continues."""
    from conversation import nodes as _nodes

    class BoomStore:
        def update_session(self, *a, **kw):
            raise RuntimeError("simulated disk full")
        def end_session(self, *a, **kw):
            raise RuntimeError("simulated disk full")
        def upsert_subsection_mastery(self, *a, **kw):
            raise RuntimeError("simulated disk full")

    monkeypatch.setattr("memory.sqlite_store.SQLiteStore", lambda *a, **kw: BoomStore())
    state = {
        "thread_id": "t1",
        "student_id": "alice",
        "locked_topic": {"path": "X > Y > Z"},
        "messages": [],
        "debug": {},
    }
    status = _nodes._persist_session_end_to_sqlite(state, judgment={"mastery": 0.5, "confidence": 0.5})
    assert "sqlite_write_error" in status, status
