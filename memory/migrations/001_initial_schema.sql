-- ────────────────────────────────────────────────────────────────────────────
-- 001_initial_schema.sql
--
-- Initial SQLite schema for the post-paper data layer split (per
-- docs/AUDIT_2026-05-02.md L1, L2, L21).
--
-- Three tables:
--   students            — user identity (one row per student)
--   sessions            — per-conversation metadata (one row per thread)
--   subsection_mastery  — per-(student, subsection) EWMA mastery score
--
-- Conventions:
--   * All timestamp columns are ISO-8601 UTC strings (TEXT). SQLite has no
--     native datetime; storing as ISO strings keeps queries portable and
--     human-readable. Use datetime.utcnow().isoformat() at write time.
--   * BOOLEAN is stored as INTEGER (0/1) per SQLite convention.
--   * subsection_path uses the canonical "Chapter N > Section > Subsection"
--     format documented in L4 (terminology fixed by Codex round-1 #4).
--   * Foreign keys are enforced; PRAGMA foreign_keys=ON is set on every
--     connection by SQLiteStore.connect().
-- ────────────────────────────────────────────────────────────────────────────

PRAGMA foreign_keys = ON;

-- ────────────────────────────────────────────────────────────────────────────
-- students — one row per user. Identity table, lookup target for FKs.
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS students (
    student_id      TEXT    PRIMARY KEY,
    created_at      TEXT    NOT NULL,                         -- ISO-8601 UTC
    display_name    TEXT
);

-- ────────────────────────────────────────────────────────────────────────────
-- sessions — one row per chat thread. Inserted at rapport_node entry per L21
-- with status='in_progress', updated at memory_update_node with the final
-- status + outcomes.
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    thread_id                TEXT    PRIMARY KEY,
    student_id               TEXT    NOT NULL REFERENCES students(student_id),
    started_at               TEXT    NOT NULL,                -- inserted at rapport entry
    ended_at                 TEXT,                            -- NULL = still in_progress
    locked_topic_path        TEXT,                            -- e.g. "Ch11 > Pectoral Girdle"
    locked_subsection_path   TEXT,                            -- denorm leaf for fast filter
    locked_question          TEXT,
    locked_answer            TEXT,
    full_answer              TEXT,
    reach_status             INTEGER,                         -- 0 / 1 / NULL
    -- Mastery tier breakdown per L68/L69 (Codex round-1 fix #3)
    mastery_tier             TEXT,                            -- proficient / developing / needs_review / not_assessed
    core_mastery_tier        TEXT,                            -- tutoring outcome (same enum)
    clinical_mastery_tier    TEXT,                            -- clinical outcome ("not_assessed" if opted out)
    core_score               REAL,                            -- 0.0 - 1.0
    clinical_score           REAL,                            -- 0.0 - 1.0, NULL if opted out
    hint_level_final         INTEGER,
    turn_count               INTEGER,
    status                   TEXT    NOT NULL DEFAULT 'in_progress',
                                     -- in_progress / completed / ended_off_domain /
                                     -- ended_by_student / ended_turn_limit / abandoned_no_lock
    key_takeaways            TEXT,                            -- JSON: {what_demonstrated, what_needs_work}
    message_log_path         TEXT,                            -- e.g. data/student_state/sessions/{thread_id}.json
    image_path               TEXT,                            -- VLM input image path (per L77), NULL if none
    image_context            TEXT                             -- JSON: VLM output (identified_structures, etc.)
);

CREATE INDEX IF NOT EXISTS idx_sessions_student_ended_at
    ON sessions(student_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_student_subsection
    ON sessions(student_id, locked_subsection_path);
CREATE INDEX IF NOT EXISTS idx_sessions_student_tier
    ON sessions(student_id, mastery_tier);
CREATE INDEX IF NOT EXISTS idx_sessions_student_status
    ON sessions(student_id, status);

-- ────────────────────────────────────────────────────────────────────────────
-- subsection_mastery — EWMA score per (student, subsection). Composite PK
-- ensures one row per pair; every session-end may update or insert here.
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subsection_mastery (
    student_id        TEXT    NOT NULL REFERENCES students(student_id),
    subsection_path   TEXT    NOT NULL,
    ewma_score        REAL    NOT NULL,                       -- 0.0 - 1.0
    last_outcome      TEXT,                                   -- reached / partial / not_reached
    last_session_at   TEXT,                                   -- ISO-8601 UTC
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (student_id, subsection_path)
);

CREATE INDEX IF NOT EXISTS idx_subsection_mastery_score
    ON subsection_mastery(student_id, ewma_score);

-- ────────────────────────────────────────────────────────────────────────────
-- schema_version — single-row table tracking migrations applied.
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
    VALUES (1, datetime('now'));
