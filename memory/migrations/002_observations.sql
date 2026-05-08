-- ────────────────────────────────────────────────────────────────────────────
-- 002_observations.sql
--
-- Replaces mem0/Qdrant for narrative student observations. Stores the same
-- per-student "misconception" and "learning_style" atoms that previously
-- lived as Qdrant payloads under the `sokratic_memory` collection, but as
-- a normal SQLite table with explicit FKs.
--
-- Why drop mem0:
--   * Reads are filter-by-(student_id, category, subsection_path) — exact
--     match queries SQL handles natively. We weren't using mem0's semantic
--     search ranking meaningfully at our scale (5–15 atoms per student).
--   * Writes don't need mem0's ADD/UPDATE/DELETE/NOOP arbitration — that
--     LLM-driven decision wasn't constrained by our domain model and
--     produced opaque merges. We never query the resolution outcome.
--   * Deduplication is handled here by a UNIQUE(student_id, hash) constraint
--     instead of mem0's hash-comparison-with-LLM-second-guess.
--   * One join target everywhere: thread_id FKs cleanly into sessions,
--     student_id FKs into students. Hard to FK against mem0's UUID points.
--
-- Migration of existing data is done by scripts/migrate_mem0_to_sql.py.
-- ────────────────────────────────────────────────────────────────────────────

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS observations (
    observation_id    TEXT    PRIMARY KEY,        -- uuid4; preserves the mem0 id on migration
    student_id        TEXT    NOT NULL REFERENCES students(student_id),
    thread_id         TEXT    REFERENCES sessions(thread_id),
                                                  -- nullable for legacy rows that lost their session
    category          TEXT    NOT NULL,           -- 'misconception' | 'learning_style'
    subsection_path   TEXT,                       -- canonical "Chapter N: ... > Section > Subsection"
    section_path      TEXT,                       -- denormalized parent path for fast section-scoped reads
    chapter_num       INTEGER,                    -- 0/NULL allowed for legacy rows
    text              TEXT    NOT NULL,           -- the observation sentence
    evidence          TEXT,                       -- optional excerpt from the transcript
    hash              TEXT,                       -- content hash for dedup (md5 of normalized text)
    created_at        TEXT    NOT NULL,           -- ISO-8601 UTC
    session_at        TEXT                        -- ISO-8601 UTC of the session moment (matches sessions.ended_at)
);

-- Read patterns: rapport (all by student), topic-lock (by student+subsection),
-- hint-advance (by student+category=learning_style). Index for each.
CREATE INDEX IF NOT EXISTS idx_observations_student_created
    ON observations(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_student_subsection
    ON observations(student_id, subsection_path);
CREATE INDEX IF NOT EXISTS idx_observations_student_category
    ON observations(student_id, category);
CREATE INDEX IF NOT EXISTS idx_observations_thread
    ON observations(thread_id);

-- Dedup: a given student should never have two observations with the same
-- normalized hash. mem0 enforced this via its hash field; we enforce it
-- with a unique constraint so duplicate session-end flushes are safe.
CREATE UNIQUE INDEX IF NOT EXISTS uq_observations_student_hash
    ON observations(student_id, hash) WHERE hash IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version, applied_at)
    VALUES (2, datetime('now'));
