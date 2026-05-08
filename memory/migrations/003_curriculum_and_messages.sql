-- ────────────────────────────────────────────────────────────────────────────
-- 003_curriculum_and_messages.sql
--
-- Schema redesign — eliminates path-string keys, introduces integer FKs
-- across every join, and adds persistent chat messages.
--
-- Tables added:
--   chapters     — root of the curriculum hierarchy (one row per textbook chapter)
--   sections     — children of chapters
--   subsections  — children of sections; the actual leaf students study
--   messages     — per-turn chat content (was previously not persisted)
--
-- Tables rewritten so they FK against subsections by integer id:
--   sessions             (locked_subsection_id replaces locked_subsection_path)
--   subsection_mastery   (subsection_id replaces subsection_path)
--   observations         (subsection_id replaces subsection_path)
--
-- Migration safety:
--   Migration 003 assumes student-data tables are empty (we wipe before
--   running it). The new tables are created with NOT NULL FKs that the
--   old TEXT-path data couldn't satisfy without resolution; resolution
--   happens in seed scripts after this migration.
--
-- After this migration:
--   * Every "Chapter 24: ... > ... > ..." path is derivable via JOIN.
--   * No more prefix mismatch bugs (the issue that bit us in mastery_tree
--     and list_sessions earlier today).
--   * `messages` is the canonical store for chat history; `message_log_path`
--     is removed from sessions.
-- ────────────────────────────────────────────────────────────────────────────

PRAGMA foreign_keys = ON;

-- ── Curriculum hierarchy ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chapters (
    chapter_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_num   INTEGER NOT NULL UNIQUE,           -- 1..N matches textbook
    title         TEXT    NOT NULL                   -- e.g. "Metabolism and Nutrition"
);

CREATE TABLE IF NOT EXISTS sections (
    section_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id     INTEGER NOT NULL REFERENCES chapters(chapter_id),
    title          TEXT    NOT NULL,                 -- "Carbohydrate Metabolism"
    section_order  INTEGER NOT NULL DEFAULT 0,       -- preserves textbook order
    UNIQUE(chapter_id, title)
);

CREATE INDEX IF NOT EXISTS idx_sections_chapter ON sections(chapter_id);

CREATE TABLE IF NOT EXISTS subsections (
    subsection_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id        INTEGER NOT NULL REFERENCES sections(section_id),
    title             TEXT    NOT NULL,              -- "Oxidative Phosphorylation..."
    display_label     TEXT,                          -- shorter label for UI
    summary           TEXT,                          -- raptor summary if present
    subsection_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(section_id, title)
);

CREATE INDEX IF NOT EXISTS idx_subsections_section ON subsections(section_id);

-- ── Sessions (recreated with FKs) ─────────────────────────────────────────
DROP TABLE IF EXISTS sessions;
CREATE TABLE sessions (
    thread_id                TEXT    PRIMARY KEY,
    student_id               TEXT    NOT NULL REFERENCES students(student_id),
    started_at               TEXT    NOT NULL,
    ended_at                 TEXT,
    locked_subsection_id     INTEGER REFERENCES subsections(subsection_id),
                                                     -- FK replaces path TEXT
    locked_question          TEXT,
    locked_answer            TEXT,
    full_answer              TEXT,
    reach_status             INTEGER,
    mastery_tier             TEXT,
    core_mastery_tier        TEXT,
    clinical_mastery_tier    TEXT,
    core_score               REAL,
    clinical_score           REAL,
    hint_level_final         INTEGER,
    turn_count               INTEGER,
    status                   TEXT    NOT NULL DEFAULT 'in_progress',
    key_takeaways            TEXT,                   -- JSON
    image_path               TEXT,                   -- filesystem path to VLM input
    image_context            TEXT                    -- JSON: VLM output
);

CREATE INDEX IF NOT EXISTS idx_sessions_student_ended_at
    ON sessions(student_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_student_subsection
    ON sessions(student_id, locked_subsection_id);
CREATE INDEX IF NOT EXISTS idx_sessions_student_status
    ON sessions(student_id, status);

-- ── Subsection mastery (recreated with FK) ─────────────────────────────────
DROP TABLE IF EXISTS subsection_mastery;
CREATE TABLE subsection_mastery (
    student_id        TEXT    NOT NULL REFERENCES students(student_id),
    subsection_id     INTEGER NOT NULL REFERENCES subsections(subsection_id),
    ewma_score        REAL    NOT NULL,
    last_outcome      TEXT,                          -- reached / partial / not_reached
    last_session_at   TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (student_id, subsection_id)
);

CREATE INDEX IF NOT EXISTS idx_subsection_mastery_score
    ON subsection_mastery(student_id, ewma_score);

-- ── Observations (recreated with FK) ───────────────────────────────────────
DROP TABLE IF EXISTS observations;
CREATE TABLE observations (
    observation_id    TEXT    PRIMARY KEY,
    student_id        TEXT    NOT NULL REFERENCES students(student_id),
    thread_id         TEXT    REFERENCES sessions(thread_id),
    subsection_id     INTEGER REFERENCES subsections(subsection_id),
                                                     -- nullable for cross-topic style cues
    category          TEXT    NOT NULL,              -- 'misconception' | 'learning_style'
    text              TEXT    NOT NULL,
    evidence          TEXT,
    hash              TEXT,
    created_at        TEXT    NOT NULL,
    session_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_observations_student_created
    ON observations(student_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_student_subsection
    ON observations(student_id, subsection_id);
CREATE INDEX IF NOT EXISTS idx_observations_student_category
    ON observations(student_id, category);
CREATE INDEX IF NOT EXISTS idx_observations_thread
    ON observations(thread_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_observations_student_hash
    ON observations(student_id, hash) WHERE hash IS NOT NULL;

-- ── Messages (chat history persistence) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id     TEXT    NOT NULL REFERENCES sessions(thread_id),
    seq           INTEGER NOT NULL,                  -- 0-indexed turn order within thread
    role          TEXT    NOT NULL,                  -- 'student' | 'tutor' | 'system'
    content       TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    metadata      TEXT,                              -- JSON: {hint_level, phase, activity, ...}
    UNIQUE(thread_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, seq);

INSERT OR IGNORE INTO schema_version (version, applied_at)
    VALUES (3, datetime('now'));
