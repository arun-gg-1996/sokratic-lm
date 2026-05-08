"""
tests/test_sqlite_session_paths.py
──────────────────────────────────
M-T1 Tier 1 (SQL side) — Unit tests for SQLiteStore session-row
lifecycle and subsection_mastery EWMA writes.

Per M-T1, asserts:

  Session lifecycle:
    - start_session creates row with status=in_progress, started_at set
    - end_session sets status (valid enum) + ended_at
    - update_session whitelists columns (rejects unknown)
    - locked_topic_path / locked_subsection_path / locked_question /
      locked_answer persist on save AND on no-save (F9 fix)
    - key_takeaways serializes JSON correctly
    - status validated against enum

  EWMA mastery:
    - First touch: stored as fresh score, attempt_count=1
    - Repeat touch: 0.7 * fresh + 0.3 * prior (F14 alpha)
    - attempt_count increments
    - last_outcome / last_session_at update

  Edges:
    - Concurrent sessions on different subsections write independent rows
    - status NOT NULL — None rejected
"""
from __future__ import annotations

import json
import math
import pytest

from memory.sqlite_store import SQLiteStore, SESSION_STATUSES

# ─── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    """Real SQLiteStore on a tmp DB. No mocks — exercises the actual
    SQL DDL + upserts."""
    db = tmp_path / "test.sqlite3"
    s = SQLiteStore(db_path=db)
    s.ensure_student("alice")
    s.ensure_student("bob")
    return s

SUBSECTION_A = "Chapter 21 > B-lymphocytes > B Cell Differentiation"
SUBSECTION_B = "Chapter 22 > Respiratory > Respiratory Zone"

# ─── Session lifecycle ────────────────────────────────────────

def test_start_session_creates_in_progress_row(store):
    row = store.start_session("t1", "alice")
    assert row is not None
    assert row["thread_id"] == "t1"
    assert row["student_id"] == "alice"
    assert row["status"] == "in_progress"
    assert row["started_at"] is not None
    assert row["ended_at"] is None
    assert row["locked_subsection_path"] is None  # not yet locked

def test_end_session_terminates_with_status_and_timestamp(store):
    store.start_session("t1", "alice")
    out = store.end_session("t1", status="completed")
    assert out["status"] == "completed"
    assert out["ended_at"] is not None
    assert out["thread_id"] == "t1"

def test_end_session_rejects_invalid_status(store):
    store.start_session("t1", "alice")
    with pytest.raises(ValueError):
        store.end_session("t1", status="invented_status")

def test_status_none_rejected(store):
    """status is NOT NULL in schema — passing None must raise."""
    store.start_session("t1", "alice")
    with pytest.raises(ValueError):
        store.update_session("t1", status=None)

def test_update_session_whitelist_rejects_unknown_columns(store):
    store.start_session("t1", "alice")
    with pytest.raises(ValueError):
        store.update_session("t1", invented_field="foo")

# ─── F9 verification — locked_subsection_path on no-save closes ────

def test_no_save_close_persists_locked_subsection_path(store):
    """F9 (): a no-save close (e.g. exit_intent
    while a topic was locked) must still record the locked_subsection_path
    so the analysis page can find the session under the subsection.

    This test mimics what conversation/lifecycle_v2.py memory_update_node
    does in the no-save branch after the F9 fix."""
    store.start_session("t1", "alice")
    store.end_session(
        "t1",
        status="ended_by_student",
        locked_topic_path=SUBSECTION_A,
        locked_subsection_path=SUBSECTION_A,
        locked_question="What does B cell differentiate into?",
        locked_answer="plasma cells",
        key_takeaways={"close_reason": "exit_intent"},
    )
    row = store.get_session("t1")
    assert row["status"] == "ended_by_student"
    assert row["locked_subsection_path"] == SUBSECTION_A
    assert row["locked_topic_path"] == SUBSECTION_A
    assert row["locked_question"] == "What does B cell differentiate into?"
    assert row["locked_answer"] == "plasma cells"
    # key_takeaways is stored as JSON — get_session deserializes
    assert isinstance(row["key_takeaways"], dict)
    assert row["key_takeaways"]["close_reason"] == "exit_intent"

def test_off_domain_strike_close_uses_correct_status(store):
    """F1 (): off-topic 4-strike close should map
    to status=ended_off_domain in SQLite."""
    store.start_session("t1", "alice")
    store.end_session(
        "t1",
        status="ended_off_domain",
        locked_subsection_path=SUBSECTION_B,
        key_takeaways={"close_reason": "off_domain_strike"},
    )
    row = store.get_session("t1")
    assert row["status"] == "ended_off_domain"
    assert row["locked_subsection_path"] == SUBSECTION_B

def test_save_bucket_close_with_clinical_correct(store):
    """F4 (): a clinical_completed + clinical_state=correct
    session must persist clinical_mastery_tier!='not_assessed' and a
    non-null clinical_score. This is what the lifecycle_v2 caller
    populates after the F4 derivation fix."""
    store.start_session("t1", "alice")
    # Mimic what lifecycle_v2._persist_session_end_to_sqlite does after F4:
    # clinical_state="correct" → clinical_score=0.85, tier=proficient
    store.end_session(
        "t1",
        status="completed",
        locked_subsection_path=SUBSECTION_A,
        reach_status=True,
        mastery_tier="developing",
        core_mastery_tier="developing",
        clinical_mastery_tier="proficient",  # NOT not_assessed
        core_score=0.5,
        clinical_score=0.85,                  # NOT None
    )
    row = store.get_session("t1")
    assert row["clinical_mastery_tier"] == "proficient"
    assert row["clinical_score"] == 0.85
    assert row["clinical_mastery_tier"] != "not_assessed"

def test_save_bucket_close_with_clinical_skipped(store):
    """When student opts out of clinical (reach_skipped), clinical
    fields should still be not_assessed / None — only when clinical
    actually ran do we get scores."""
    store.start_session("t1", "alice")
    store.end_session(
        "t1",
        status="completed",
        locked_subsection_path=SUBSECTION_A,
        reach_status=True,
        mastery_tier="developing",
        core_mastery_tier="developing",
        clinical_mastery_tier="not_assessed",
        core_score=0.5,
        clinical_score=None,
    )
    row = store.get_session("t1")
    assert row["clinical_mastery_tier"] == "not_assessed"
    assert row["clinical_score"] is None

def test_save_bucket_close_persists_full_metadata(store):
    """Save-bucket close (reach_full): full mastery score + key_takeaways
    populated, status=completed."""
    store.start_session("t1", "alice")
    store.end_session(
        "t1",
        status="completed",
        locked_subsection_path=SUBSECTION_A,
        locked_topic_path=SUBSECTION_A,
        locked_question="Q?",
        locked_answer="A",
        reach_status=True,
        mastery_tier="developing",
        core_mastery_tier="proficient",
        clinical_mastery_tier="developing",
        core_score=0.85,
        clinical_score=0.5,
        hint_level_final=1,
        turn_count=8,
        key_takeaways={
            "demonstrated": "cross-bridge mechanics",
            "needs_work": "calcium release timing",
            "close_reason": "reach_full",
        },
    )
    row = store.get_session("t1")
    assert row["status"] == "completed"
    assert row["reach_status"] == 1  # bool → INTEGER per schema
    assert row["core_score"] == 0.85
    assert row["clinical_score"] == 0.5
    assert row["mastery_tier"] == "developing"
    assert row["clinical_mastery_tier"] == "developing"
    assert row["key_takeaways"]["demonstrated"] == "cross-bridge mechanics"

def test_session_status_enum_complete(store):
    """All valid status values schema."""
    expected = {
        "in_progress",
        "completed",
        "ended_off_domain",
        "ended_by_student",
        "ended_turn_limit",
        "abandoned_no_lock",
    }
    assert SESSION_STATUSES == expected

# ─── EWMA mastery (F14 verification) ───────────────────────────

def test_ewma_first_touch_stores_fresh_score(store):
    """First attempt on a subsection: ewma_score = fresh_score
    (no blend), attempt_count = 1."""
    out = store.upsert_subsection_mastery(
        "alice", SUBSECTION_A,
        fresh_score=0.4, outcome="not_reached",
    )
    assert out["ewma_score"] == 0.4
    assert out["attempt_count"] == 1
    assert out["last_outcome"] == "not_reached"

def test_ewma_repeat_blend_uses_alpha_0_7(store):
    """F14: alpha=0.7 default. new = 0.7 * fresh + 0.3 * prior."""
    store.upsert_subsection_mastery(
        "alice", SUBSECTION_A, fresh_score=0.4, outcome="not_reached",
    )
    out = store.upsert_subsection_mastery(
        "alice", SUBSECTION_A, fresh_score=0.9, outcome="reached",
    )
    expected = 0.7 * 0.9 + 0.3 * 0.4  # = 0.63 + 0.12 = 0.75
    assert math.isclose(out["ewma_score"], expected, abs_tol=1e-9)
    assert out["attempt_count"] == 2
    assert out["last_outcome"] == "reached"

def test_ewma_explicit_alpha_overrides_default(store):
    """Caller can override alpha (e.g., test code or future tuning)."""
    store.upsert_subsection_mastery(
        "alice", SUBSECTION_A, fresh_score=0.4, outcome="not_reached",
    )
    out = store.upsert_subsection_mastery(
        "alice", SUBSECTION_A, fresh_score=0.9, outcome="reached",
        alpha=0.5,  # explicit override
    )
    expected = 0.5 * 0.9 + 0.5 * 0.4  # = 0.65
    assert math.isclose(out["ewma_score"], expected, abs_tol=1e-9)

def test_ewma_invalid_outcome_rejected(store):
    with pytest.raises(ValueError):
        store.upsert_subsection_mastery(
            "alice", SUBSECTION_A, fresh_score=0.4, outcome="garbage",
        )

def test_ewma_independent_per_student_subsection(store):
    """Two students × two subsections = four independent EWMA rows.
    Ensures upserts don't trample each other."""
    store.upsert_subsection_mastery("alice", SUBSECTION_A, 0.6, "reached")
    store.upsert_subsection_mastery("alice", SUBSECTION_B, 0.3, "not_reached")
    store.upsert_subsection_mastery("bob", SUBSECTION_A, 0.9, "reached")
    store.upsert_subsection_mastery("bob", SUBSECTION_B, 0.5, "partial")

    a_a = store.get_subsection_mastery("alice", SUBSECTION_A)
    a_b = store.get_subsection_mastery("alice", SUBSECTION_B)
    b_a = store.get_subsection_mastery("bob", SUBSECTION_A)
    b_b = store.get_subsection_mastery("bob", SUBSECTION_B)

    assert a_a["ewma_score"] == 0.6
    assert a_b["ewma_score"] == 0.3
    assert b_a["ewma_score"] == 0.9
    assert b_b["ewma_score"] == 0.5
    for row in (a_a, a_b, b_a, b_b):
        assert row["attempt_count"] == 1

# ─── list_sessions filtering (M5 + F11 helpers) ────────────────

def test_list_sessions_filter_by_subsection_path(store):
    """list_sessions(subsection_path=...) filters the M5 inline list +
    feeds F7 prior-locked-questions helper."""
    store.start_session("t1", "alice")
    store.end_session("t1", status="completed",
                      locked_subsection_path=SUBSECTION_A,
                      locked_question="Q1")
    store.start_session("t2", "alice")
    store.end_session("t2", status="completed",
                      locked_subsection_path=SUBSECTION_A,
                      locked_question="Q2")
    store.start_session("t3", "alice")
    store.end_session("t3", status="completed",
                      locked_subsection_path=SUBSECTION_B,
                      locked_question="Q3")

    a_sessions = store.list_sessions("alice", subsection_path=SUBSECTION_A)
    assert len(a_sessions) == 2
    assert {s["locked_question"] for s in a_sessions} == {"Q1", "Q2"}

    b_sessions = store.list_sessions("alice", subsection_path=SUBSECTION_B)
    assert len(b_sessions) == 1
    assert b_sessions[0]["locked_question"] == "Q3"

def test_list_sessions_completed_only(store):
    store.start_session("t1", "alice")  # in_progress
    store.start_session("t2", "alice")
    store.end_session("t2", status="completed", locked_subsection_path=SUBSECTION_A)

    completed = store.list_sessions("alice", completed_only=True)
    assert len(completed) == 1
    assert completed[0]["thread_id"] == "t2"
