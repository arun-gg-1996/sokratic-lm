-- ────────────────────────────────────────────────────────────────────────────
-- 004_summaries_and_abbreviations.sql
--
-- Final consolidation step. Moves the last static data still living as flat
-- files into SQL so the SQLite database is the single source of truth for
-- all non-vector content. After this migration + seed, the runtime no longer
-- reads any of:
--   data/raptor_subsection_summaries.jsonl
--   data/raptor_section_summaries.jsonl
--   data/curated_abbrevs_*.json
--
-- (data/topic_index.json and data/textbook_structure.json become seed-input
-- artifacts only — used by scripts/seed_curriculum.py and the new summary
-- backfill, not read by the running app.)
--
-- New columns + table:
--   sections.summary       — RAPTOR section-level summary (170 entries in OT)
--   topic_abbreviations    — alias → canonical phrase pairs used by the
--                            topic_mapper Haiku call to expand student
--                            shorthand ("OT" → "Occupational Therapy" etc.)
-- ────────────────────────────────────────────────────────────────────────────

PRAGMA foreign_keys = ON;

-- Add the section-level summary column. SQLite supports ALTER TABLE ADD
-- COLUMN; subsections.summary already exists from migration 003.
ALTER TABLE sections ADD COLUMN summary TEXT;

-- Topic abbreviations / aliases — many-to-one mapping from a short or
-- alternate phrase to a canonical subject phrase. Used at topic-mapping
-- time so a student writing "knee" gets matched against the subsection
-- "Articulations of the Knee Joint" via an explicit alias rather than
-- relying solely on Haiku's surface-form matching.
CREATE TABLE IF NOT EXISTS topic_abbreviations (
    abbreviation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    alias            TEXT    NOT NULL,                    -- "OT", "knee", etc.
    canonical        TEXT    NOT NULL,                    -- expanded phrase
    notes            TEXT,                                -- optional context
    UNIQUE(alias, canonical)
);

CREATE INDEX IF NOT EXISTS idx_topic_abbreviations_alias
    ON topic_abbreviations(alias);

INSERT OR IGNORE INTO schema_version (version, applied_at)
    VALUES (4, datetime('now'));
