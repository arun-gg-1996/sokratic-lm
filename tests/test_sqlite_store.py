"""
tests/test_sqlite_store.py
──────────────────────────
Unit + integration tests for memory/sqlite_store.py.

Coverage:
  * Schema migration (idempotent, version table)
  * Student lifecycle (ensure / get)
  * Session lifecycle per L21 (start → in_progress → end → completed/etc;
                               browser-close stays in_progress)
  * EWMA upsert per L3 (first touch, blend math, attempt counter)
  * Status enum validation
  * student_stats counters per L21 (total / completed / unfinished /
                                    abandoned-mid-session 1h grace / by_tier)
  * mastery_tree rollup per L3 (mean of touched children, color thresholds)
  * Pre-lock-terminated session row pattern (per L21 + L22)
  * JSON-encoded field roundtrip (key_takeaways, image_context)

Runs with `pytest -xvs tests/test_sqlite_store.py` — uses tmp_path fixture
so each test gets a fresh DB.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from memory.sqlite_store import (
    COLOR_GREEN_MIN,
    COLOR_YELLOW_MIN,
    SESSION_STATUSES,
    SQLiteStore,
    score_to_color,
    score_to_tier,
    utc_now,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    """Fresh SQLite DB per test (explicit db_path bypasses domain resolution)."""
    db = tmp_path / "test.sqlite3"
    s = SQLiteStore(db_path=db)
    yield s
    s.close()


@pytest.fixture
def topic_index_min() -> list[dict]:
    """Minimal topic_index for mastery_tree tests — 2 chapters, mixed coverage."""
    return [
        {"chapter": "Muscle Tissue", "chapter_num": 10,
         "section": "Skeletal Muscle", "subsection": "Sarcomere Structure",
         "display_label": "Sarcomere Structure"},
        {"chapter": "Muscle Tissue", "chapter_num": 10,
         "section": "Skeletal Muscle", "subsection": "Sliding Filament Theory",
         "display_label": "Sliding Filaments"},
        {"chapter": "Muscle Tissue", "chapter_num": 10,
         "section": "Skeletal Muscle", "subsection": "Motor Units",
         "display_label": "Motor Units"},
        {"chapter": "Muscle Tissue", "chapter_num": 10,
         "section": "Cardiac Muscle", "subsection": "Pacemaker Cells",
         "display_label": "Pacemaker Cells"},
        {"chapter": "The Nervous System and Nervous Tissue", "chapter_num": 12,
         "section": "Nervous Tissue", "subsection": "Neurons",
         "display_label": "Neurons"},
        {"chapter": "The Nervous System and Nervous Tissue", "chapter_num": 12,
         "section": "Nervous Tissue", "subsection": "Glial Cells",
         "display_label": "Glial Cells"},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Schema + migrations
# ─────────────────────────────────────────────────────────────────────────────

def test_migration_creates_three_tables(tmp_path):
    db = tmp_path / "x.sqlite3"
    s = SQLiteStore(db_path=db)
    conn = s._conn()
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row["name"] for row in cur.fetchall()}
    assert {"students", "sessions", "subsection_mastery", "schema_version"} <= tables
    s.close()


def test_migration_idempotent(tmp_path):
    db = tmp_path / "x.sqlite3"
    SQLiteStore(db_path=db).close()
    # Re-open same file in same process — must not duplicate version row.
    SQLiteStore(db_path=db).close()
    s = SQLiteStore(db_path=db)
    cur = s._conn().execute("SELECT version FROM schema_version")
    versions = [r["version"] for r in cur.fetchall()]
    assert versions == [1]
    s.close()


def test_two_dbs_in_same_process_both_get_migrations(tmp_path):
    """Regression: the migrations cache is per-DB-path; opening a second DB
    file in the same process must run migrations on it, not skip because the
    first DB already had them applied."""
    a = tmp_path / "a.sqlite3"
    b = tmp_path / "b.sqlite3"
    sa = SQLiteStore(db_path=a)
    sb = SQLiteStore(db_path=b)
    for s in (sa, sb):
        cur = s._conn().execute("SELECT version FROM schema_version")
        assert {r["version"] for r in cur.fetchall()} == {1}
    sa.close(); sb.close()


def test_foreign_keys_enforced(store):
    """sessions.student_id → students.student_id must be enforced."""
    with pytest.raises(sqlite3.IntegrityError):
        store._conn().execute(
            "INSERT INTO sessions(thread_id, student_id, started_at) "
            "VALUES ('t1', 'NO_SUCH_STUDENT', ?)",
            (utc_now(),),
        )
        store._conn().commit()


# ─────────────────────────────────────────────────────────────────────────────
# Students
# ─────────────────────────────────────────────────────────────────────────────

def test_ensure_student_idempotent(store):
    a = store.ensure_student("alice")
    b = store.ensure_student("alice")
    assert a["student_id"] == b["student_id"] == "alice"
    # created_at must not change on second call
    assert a["created_at"] == b["created_at"]


def test_ensure_student_updates_display_name(store):
    store.ensure_student("alice")
    s = store.ensure_student("alice", display_name="Alice Z.")
    assert s["display_name"] == "Alice Z."


# ─────────────────────────────────────────────────────────────────────────────
# Session lifecycle (L21)
# ─────────────────────────────────────────────────────────────────────────────

def test_start_session_inserts_in_progress(store):
    row = store.start_session("t1", "alice")
    assert row["status"] == "in_progress"
    assert row["ended_at"] is None
    assert row["started_at"] is not None
    assert row["thread_id"] == "t1"
    assert row["student_id"] == "alice"


def test_start_session_creates_student_lazily(store):
    store.start_session("t1", "fresh_user")
    assert store.get_student("fresh_user") is not None


def test_update_session_partial(store):
    store.start_session("t1", "alice")
    updated = store.update_session(
        "t1",
        locked_topic_path="Muscle Tissue > Skeletal Muscle",
        locked_subsection_path="Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
        reach_status=True,
        turn_count=12,
    )
    assert updated["locked_topic_path"] == "Muscle Tissue > Skeletal Muscle"
    assert updated["reach_status"] == 1  # bool stored as int
    assert updated["turn_count"] == 12
    # Status untouched
    assert updated["status"] == "in_progress"


def test_update_session_rejects_unknown_column(store):
    store.start_session("t1", "alice")
    with pytest.raises(ValueError, match="Unknown session column"):
        store.update_session("t1", evil_column=42)


def test_update_session_rejects_invalid_status(store):
    store.start_session("t1", "alice")
    with pytest.raises(ValueError, match="Invalid status"):
        store.update_session("t1", status="rocketship")


def test_end_session_completes(store):
    store.start_session("t1", "alice")
    ended = store.end_session("t1", status="completed", mastery_tier="proficient",
                              core_score=0.85, hint_level_final=1, turn_count=22)
    assert ended["status"] == "completed"
    assert ended["ended_at"] is not None
    assert ended["mastery_tier"] == "proficient"


def test_end_session_pre_lock_pattern(store):
    """Per L21: pre-lock terminated row uses 'abandoned_no_lock' + nulls."""
    store.start_session("t1", "alice")
    ended = store.end_session(
        "t1",
        status="abandoned_no_lock",
        mastery_tier="not_assessed",
        core_mastery_tier="not_assessed",
        clinical_mastery_tier="not_assessed",
        turn_count=0,
    )
    assert ended["status"] == "abandoned_no_lock"
    assert ended["locked_topic_path"] is None
    assert ended["locked_subsection_path"] is None
    assert ended["reach_status"] is None
    assert ended["mastery_tier"] == "not_assessed"


def test_browser_close_stays_in_progress(store):
    """Per L21 + L35: no auto-cleanup; abandoned row stays in_progress
    forever, ended_at stays NULL. Stat queries derive the abandoned-mid-
    session signal with a 1-hour grace window."""
    store.start_session("t1", "alice")
    # Simulate "browser close" — never call end_session
    row = store.get_session("t1")
    assert row["status"] == "in_progress"
    assert row["ended_at"] is None


def test_json_field_roundtrip(store):
    store.start_session("t1", "alice")
    take = {"what_demonstrated": "good Q", "what_needs_work": "blah"}
    img_ctx = {"identified_structures": ["heart", "lungs"], "best_topic_guess": "Cardiovascular"}
    store.update_session("t1", key_takeaways=take, image_context=img_ctx)
    row = store.get_session("t1")
    assert row["key_takeaways"] == take
    assert row["image_context"] == img_ctx


def test_list_sessions_completed_only(store):
    store.start_session("a1", "alice")
    store.end_session("a1", status="completed")
    store.start_session("a2", "alice")  # left in_progress
    completed = store.list_sessions("alice", completed_only=True)
    assert {s["thread_id"] for s in completed} == {"a1"}
    all_ = store.list_sessions("alice")
    assert {s["thread_id"] for s in all_} == {"a1", "a2"}


# ─────────────────────────────────────────────────────────────────────────────
# Subsection mastery (EWMA per L3)
# ─────────────────────────────────────────────────────────────────────────────

def test_first_touch_inserts_with_fresh_score(store):
    store.ensure_student("alice")
    out = store.upsert_subsection_mastery(
        "alice", "Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
        fresh_score=0.7, outcome="reached",
    )
    assert out["ewma_score"] == 0.7
    assert out["attempt_count"] == 1
    assert out["last_outcome"] == "reached"


def test_ewma_blend_math(store):
    """EWMA per L3: new = 0.6*fresh + 0.4*prior."""
    store.ensure_student("alice")
    p = "Ch1 > Sec > Sub"
    store.upsert_subsection_mastery("alice", p, fresh_score=0.4, outcome="not_reached")
    out = store.upsert_subsection_mastery("alice", p, fresh_score=0.9, outcome="reached")
    expected = 0.6 * 0.9 + 0.4 * 0.4   # = 0.54 + 0.16 = 0.70
    assert math.isclose(out["ewma_score"], expected, abs_tol=1e-9)
    assert out["attempt_count"] == 2
    assert out["last_outcome"] == "reached"


def test_invalid_outcome_rejected(store):
    store.ensure_student("alice")
    with pytest.raises(ValueError, match="Invalid outcome"):
        store.upsert_subsection_mastery(
            "alice", "X > Y > Z", fresh_score=0.5, outcome="bogus"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Stats counters per L21
# ─────────────────────────────────────────────────────────────────────────────

def test_stats_counts_total_completed_unfinished(store):
    # 2 completed (one proficient, one developing)
    store.start_session("t1", "alice")
    store.end_session("t1", status="completed", mastery_tier="proficient")
    store.start_session("t2", "alice")
    store.end_session("t2", status="completed", mastery_tier="developing")
    # 1 ended_off_domain (unfinished)
    store.start_session("t3", "alice")
    store.end_session("t3", status="ended_off_domain", mastery_tier="not_assessed")
    # 1 abandoned_no_lock
    store.start_session("t4", "alice")
    store.end_session("t4", status="abandoned_no_lock", mastery_tier="not_assessed")
    # 1 left in_progress (browser close)
    store.start_session("t5", "alice")

    stats = store.student_stats("alice")
    assert stats["total_sessions"] == 5
    assert stats["completed_sessions"] == 2
    # unfinished = ended_at NULL OR status in (abandoned_no_lock, ended_off_domain, ended_turn_limit)
    # = t3 + t4 + t5 = 3
    assert stats["unfinished_count"] == 3
    assert stats["by_tier"]["proficient"] == 1
    assert stats["by_tier"]["developing"] == 1
    assert stats["strong_count"] == 1
    assert stats["weak_count"] == 0  # no needs_review here


def test_stats_abandoned_grace_window(store):
    """Sessions left in_progress for less than the grace window should NOT
    count as abandoned (could be the user's actively-running tab)."""
    store.ensure_student("alice")

    # Insert an "old" in_progress session by manually setting started_at
    # to 2 hours ago.
    old_ts = (datetime.utcnow() - timedelta(hours=2)).replace(microsecond=0).isoformat() + "Z"
    fresh_ts = utc_now()
    store._conn().execute(
        "INSERT INTO sessions(thread_id, student_id, started_at, status) "
        "VALUES (?, ?, ?, 'in_progress')",
        ("old", "alice", old_ts),
    )
    store._conn().execute(
        "INSERT INTO sessions(thread_id, student_id, started_at, status) "
        "VALUES (?, ?, ?, 'in_progress')",
        ("fresh", "alice", fresh_ts),
    )
    store._conn().commit()

    stats = store.student_stats("alice", abandoned_grace_hours=1)
    assert stats["abandoned_mid_session"] == 1  # only 'old' qualifies


# ─────────────────────────────────────────────────────────────────────────────
# Mastery tree rollup per L3
# ─────────────────────────────────────────────────────────────────────────────

def test_mastery_tree_excludes_untouched_from_mean(store, topic_index_min):
    """Per L3: mean of TOUCHED children only; untouched are excluded from
    score but still counted in `total`."""
    student_id = "alice"
    store.ensure_student(student_id)
    # Touch 2 of 3 subsections in "Skeletal Muscle" section
    store.upsert_subsection_mastery(
        student_id, "Muscle Tissue > Skeletal Muscle > Sarcomere Structure",
        fresh_score=0.80, outcome="reached",
    )
    store.upsert_subsection_mastery(
        student_id, "Muscle Tissue > Skeletal Muscle > Sliding Filament Theory",
        fresh_score=0.40, outcome="not_reached",
    )
    # Leave "Motor Units" + the entire Cardiac Muscle section + Ch12 untouched

    tree = store.mastery_tree(student_id, topic_index_min)
    chapters = {c["chapter"]: c for c in tree["chapters"]}

    # Skeletal Muscle section: mean of (0.80, 0.40) = 0.60 → yellow / developing
    skm = next(s for s in chapters["Muscle Tissue"]["sections"] if s["section"] == "Skeletal Muscle")
    assert math.isclose(skm["score"], 0.60, abs_tol=1e-9)
    assert skm["touched"] == 2
    assert skm["total"] == 3
    assert skm["color"] == "yellow"
    assert skm["tier"] == "developing"

    # Cardiac Muscle section: untouched
    cm = next(s for s in chapters["Muscle Tissue"]["sections"] if s["section"] == "Cardiac Muscle")
    assert cm["score"] is None
    assert cm["color"] == "grey"
    assert cm["touched"] == 0
    assert cm["total"] == 1

    # Ch10 chapter: mean of touched sections = mean of (Skeletal=0.60) since
    # Cardiac is untouched (excluded). So chapter score = 0.60.
    assert math.isclose(chapters["Muscle Tissue"]["score"], 0.60, abs_tol=1e-9)
    assert chapters["Muscle Tissue"]["touched"] == 2
    assert chapters["Muscle Tissue"]["total"] == 4

    # Ch12 entirely untouched
    assert chapters["The Nervous System and Nervous Tissue"]["score"] is None
    assert chapters["The Nervous System and Nervous Tissue"]["color"] == "grey"


def test_mastery_tree_color_thresholds(store, topic_index_min):
    student_id = "alice"
    store.ensure_student(student_id)
    cases = [
        (0.90, "green",  "proficient"),
        (0.75, "green",  "proficient"),
        (0.74, "yellow", "developing"),
        (0.50, "yellow", "developing"),
        (0.49, "red",    "needs_review"),
        (0.00, "red",    "needs_review"),
    ]
    for score, color, tier in cases:
        assert score_to_color(score) == color, score
        assert score_to_tier(score) == tier, score
    assert score_to_color(None) == "grey"
    assert score_to_tier(None) == "not_assessed"


# ─────────────────────────────────────────────────────────────────────────────
# Status enum
# ─────────────────────────────────────────────────────────────────────────────

def test_status_enum_matches_l21_spec():
    assert SESSION_STATUSES == {
        "in_progress",
        "completed",
        "ended_off_domain",
        "ended_by_student",
        "ended_turn_limit",
        "abandoned_no_lock",
    }


def test_color_threshold_constants():
    assert COLOR_GREEN_MIN == 0.75
    assert COLOR_YELLOW_MIN == 0.50


# ─────────────────────────────────────────────────────────────────────────────
# Cross-domain isolation (per L1 — separate DB file per domain)
# ─────────────────────────────────────────────────────────────────────────────

def test_default_db_path_is_per_domain():
    from memory.sqlite_store import default_db_path
    assert default_db_path("openstax_anatomy").name == "sokratic_openstax_anatomy.sqlite3"
    assert default_db_path("physics").name == "sokratic_physics.sqlite3"
    # Different domains → different file paths (cross-contamination impossible)
    assert default_db_path("openstax_anatomy") != default_db_path("physics")


def test_two_domain_dbs_isolated(tmp_path):
    """Same student_id, two domain DBs, no cross-read.

    Models the real-world scenario: a student studies both anatomy and
    physics. Their progress in each must be independent.
    """
    db_anat = tmp_path / "sokratic_openstax_anatomy.sqlite3"
    db_phys = tmp_path / "sokratic_physics.sqlite3"
    s_anat = SQLiteStore(db_path=db_anat)
    s_phys = SQLiteStore(db_path=db_phys)

    # Same student_id in both domains
    s_anat.start_session("t-anat-1", "alice")
    s_anat.end_session("t-anat-1", status="completed", mastery_tier="proficient",
                       locked_subsection_path="The Cardiovascular System: ... > Sec > Sub")
    s_anat.upsert_subsection_mastery(
        "alice", "Anatomy > Sec > Sub", fresh_score=0.9, outcome="reached"
    )

    s_phys.start_session("t-phys-1", "alice")
    s_phys.end_session("t-phys-1", status="completed", mastery_tier="developing")
    s_phys.upsert_subsection_mastery(
        "alice", "Physics > Sec > Sub", fresh_score=0.6, outcome="partial"
    )

    # Anatomy DB sees only the anatomy session + mastery
    assert {s["thread_id"] for s in s_anat.list_sessions("alice")} == {"t-anat-1"}
    anat_paths = {m["subsection_path"] for m in s_anat.list_subsection_mastery("alice")}
    assert anat_paths == {"Anatomy > Sec > Sub"}

    # Physics DB sees only the physics session + mastery
    assert {s["thread_id"] for s in s_phys.list_sessions("alice")} == {"t-phys-1"}
    phys_paths = {m["subsection_path"] for m in s_phys.list_subsection_mastery("alice")}
    assert phys_paths == {"Physics > Sec > Sub"}

    # Stats counters never leak across domains
    assert s_anat.student_stats("alice")["completed_sessions"] == 1
    assert s_phys.student_stats("alice")["completed_sessions"] == 1
    s_anat.close(); s_phys.close()


def test_no_domain_no_db_path_raises(monkeypatch):
    """Production safety: refusing to open a domain-blind store."""
    # Force cfg.domain.retrieval_domain to be empty/missing
    import config
    monkeypatch.setattr(config.cfg.domain, "retrieval_domain", "", raising=False)
    with pytest.raises(ValueError, match="non-empty domain"):
        SQLiteStore()


def test_explicit_domain_resolves_canonical_path(tmp_path, monkeypatch):
    """Passing domain= alone must land at the canonical per-domain file."""
    # Sandbox the data dir so we don't write to real student_state during tests
    from memory import sqlite_store as _ss
    monkeypatch.setattr(_ss, "REPO", tmp_path)
    s = SQLiteStore(domain="testdomain")
    expected = tmp_path / "data" / "student_state" / "sokratic_testdomain.sqlite3"
    assert s.db_path == expected
    assert s.domain == "testdomain"
    s.close()


# ─────────────────────────────────────────────────────────────────────────────
# update_session — explicit-NULL semantics (Refinement R3)
# ─────────────────────────────────────────────────────────────────────────────

def test_explicit_none_writes_null(store):
    """Passing a kwarg with value=None should set the column to SQL NULL."""
    store.start_session("t1", "alice")
    store.update_session("t1", locked_topic_path="Some Topic", turn_count=5)
    assert store.get_session("t1")["locked_topic_path"] == "Some Topic"

    store.update_session("t1", locked_topic_path=None)
    assert store.get_session("t1")["locked_topic_path"] is None
    # Other columns must not have been touched
    assert store.get_session("t1")["turn_count"] == 5


def test_status_cannot_be_none(store):
    """status is NOT NULL — must reject explicit None."""
    store.start_session("t1", "alice")
    with pytest.raises(ValueError, match="status is NOT NULL"):
        store.update_session("t1", status=None)


def test_omit_kwarg_skips_column(store):
    """If you don't pass a column at all, it stays unchanged (vs explicit None)."""
    store.start_session("t1", "alice")
    store.update_session("t1", locked_topic_path="X", turn_count=10)
    store.update_session("t1", turn_count=11)  # locked_topic_path NOT passed
    row = store.get_session("t1")
    assert row["locked_topic_path"] == "X"  # still there
    assert row["turn_count"] == 11
