#!/usr/bin/env python3
"""
End-to-end verification of the post-migration database.

Runs a self-contained suite of checks against the SQLite database for the
active domain. Prints PASS/FAIL per check and exits non-zero on any
failure so this can run in CI.

Categories of checks:
    1. Migration state    — every expected migration applied
    2. Tables             — all expected tables exist with expected columns
    3. Indexes            — performance-critical indexes are in place
    4. Foreign keys       — schema declares FKs and SQLite enforces them
    5. Curriculum seed    — chapters/sections/subsections counts and uniqueness
    6. Static content     — summaries + abbreviations populated as expected
    7. Referential check  — zero orphan rows across every FK relationship
    8. User-data tables   — empty after the wipe (clean slate guarantee)
    9. Resolver round-trip — subsection_path ↔ subsection_id is consistent
   10. Functional smoke   — mastery_tree, TopicSuggester, TopicMatcher,
                            and the topic-mapper TOC block all build from SQL

Usage:
    .venv/bin/python scripts/verify_db.py
    .venv/bin/python scripts/verify_db.py -v       # verbose: list each check

Exit codes:
    0 — all checks passed
    1 — at least one check failed (details printed to stderr)
"""
from __future__ import annotations

import argparse
import sys
import sqlite3
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ── Tiny test framework — keeps the script self-contained, no pytest needed.

class Checks:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.failures: list[str] = []
        self.passed = 0
        self.section: str = ""

    def start(self, section: str) -> None:
        self.section = section
        print(f"\n── {section}")

    def check(self, name: str, predicate: Callable[[], bool], detail: str = "") -> None:
        try:
            ok = predicate()
            err = ""
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"  ✓ {name}")
        else:
            msg = f"{self.section} :: {name}"
            if detail:
                msg += f"  [{detail}]"
            if err:
                msg += f"  ({err})"
            self.failures.append(msg)
            print(f"  ✗ {name}" + (f"  ← {detail}" if detail else "") + (f"  ({err})" if err else ""))

    def equal(self, name: str, actual, expected, detail: str = "") -> None:
        if not detail:
            detail = f"expected={expected!r} actual={actual!r}"
        self.check(name, lambda a=actual, e=expected: a == e, detail)

    def at_least(self, name: str, actual: int, minimum: int) -> None:
        self.check(name, lambda a=actual, m=minimum: a >= m,
                   f"expected ≥ {minimum}, got {actual}")

    def report(self) -> int:
        total = self.passed + len(self.failures)
        print(f"\n{'═' * 64}")
        if not self.failures:
            print(f"PASS: {self.passed}/{total} checks ok")
            return 0
        print(f"FAIL: {len(self.failures)}/{total} checks failed")
        for f in self.failures:
            print(f"  ✗ {f}", file=sys.stderr)
        return 1


# ── Connection helper — uses the same path the app uses.

def open_db() -> sqlite3.Connection:
    from config import cfg
    domain = cfg.domain.retrieval_domain
    db_path = REPO / "data" / "student_state" / f"sokratic_{domain}.sqlite3"
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


# ── Expected schema (the source of truth for the verification).

EXPECTED_TABLES: dict[str, set[str]] = {
    "schema_version": {"version", "applied_at"},
    "students": {"student_id", "created_at", "display_name"},
    "chapters": {"chapter_id", "chapter_num", "title"},
    "sections": {"section_id", "chapter_id", "title", "section_order", "summary"},
    "subsections": {
        "subsection_id", "section_id", "title", "display_label",
        "summary", "subsection_order",
    },
    "sessions": {
        "thread_id", "student_id", "started_at", "ended_at",
        "locked_subsection_id", "locked_question", "locked_answer",
        "full_answer", "reach_status", "mastery_tier",
        "core_mastery_tier", "clinical_mastery_tier",
        "core_score", "clinical_score", "hint_level_final", "turn_count",
        "status", "key_takeaways", "image_path", "image_context",
    },
    "subsection_mastery": {
        "student_id", "subsection_id", "ewma_score",
        "last_outcome", "last_session_at", "attempt_count",
    },
    "observations": {
        "observation_id", "student_id", "thread_id", "subsection_id",
        "category", "text", "evidence", "hash",
        "created_at", "session_at",
    },
    "messages": {
        "message_id", "thread_id", "seq", "role", "content",
        "created_at", "metadata",
    },
    "topic_abbreviations": {"abbreviation_id", "alias", "canonical", "notes"},
}

EXPECTED_INDEXES = {
    "idx_sessions_student_ended_at",
    "idx_sessions_student_subsection",
    "idx_sessions_student_status",
    "idx_subsection_mastery_score",
    "idx_observations_student_created",
    "idx_observations_student_subsection",
    "idx_observations_student_category",
    "idx_observations_thread",
    "uq_observations_student_hash",
    "idx_messages_thread",
    "idx_sections_chapter",
    "idx_subsections_section",
    "idx_topic_abbreviations_alias",
}

# Foreign key relationships we expect to exist (from_table → (col, to_table, to_col))
EXPECTED_FKS = {
    "sections":            ("chapter_id", "chapters", "chapter_id"),
    "subsections":         ("section_id", "sections", "section_id"),
    "sessions":            ("student_id", "students", "student_id"),
    "subsection_mastery":  ("student_id", "students", "student_id"),
    "observations":        ("student_id", "students", "student_id"),
    "messages":            ("thread_id", "sessions", "thread_id"),
}


# ──────────────────────────────────────────────────────────────────────────
# Checks
# ──────────────────────────────────────────────────────────────────────────

def check_migrations(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Migrations applied")
    rows = con.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    versions = [r["version"] for r in rows]
    for expected in (1, 2, 3, 4):
        c.check(f"migration {expected:03d}", lambda v=expected: v in versions,
                f"versions in DB: {versions}")


def check_tables(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Table schema")
    actual_tables = {
        r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for tbl, expected_cols in EXPECTED_TABLES.items():
        c.check(f"table `{tbl}` exists", lambda t=tbl: t in actual_tables)
        if tbl in actual_tables:
            actual_cols = {
                r["name"] for r in con.execute(f"PRAGMA table_info({tbl})")
            }
            missing = expected_cols - actual_cols
            extra = actual_cols - expected_cols
            c.check(f"`{tbl}` columns",
                    lambda m=missing, e=extra: not m,
                    f"missing={sorted(missing)} extra={sorted(extra)}")


def check_indexes(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Indexes")
    actual = {
        r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = EXPECTED_INDEXES - actual
    for idx in sorted(EXPECTED_INDEXES):
        c.check(f"index `{idx}`", lambda i=idx: i in actual)
    if missing:
        c.check("all expected indexes present",
                lambda: not missing, f"missing: {sorted(missing)}")


def check_foreign_keys(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Foreign-key declarations")
    # SQLite enforcement
    fk_on = con.execute("PRAGMA foreign_keys").fetchone()[0]
    c.check("PRAGMA foreign_keys = ON", lambda v=fk_on: v == 1)

    for tbl, (col, ref_tbl, ref_col) in EXPECTED_FKS.items():
        rows = con.execute(f"PRAGMA foreign_key_list({tbl})").fetchall()
        match = any(
            r["from"] == col and r["table"] == ref_tbl and r["to"] == ref_col
            for r in rows
        )
        c.check(f"`{tbl}.{col}` → `{ref_tbl}.{ref_col}`", lambda m=match: m)


def check_referential_integrity(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Referential integrity (no orphans)")
    # SQLite's built-in checker — returns rows for every violation.
    rows = con.execute("PRAGMA foreign_key_check").fetchall()
    c.check("foreign_key_check returns 0 violations",
            lambda: len(rows) == 0,
            f"violations: {[dict(r) for r in rows]}" if rows else "")


def check_curriculum_counts(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Curriculum seed (chapters / sections / subsections)")
    n_ch = con.execute("SELECT COUNT(*) AS n FROM chapters").fetchone()["n"]
    n_sec = con.execute("SELECT COUNT(*) AS n FROM sections").fetchone()["n"]
    n_sub = con.execute("SELECT COUNT(*) AS n FROM subsections").fetchone()["n"]
    print(f"  → chapters={n_ch}  sections={n_sec}  subsections={n_sub}")

    # Counts within reasonable bounds for the OT corpus (27 chapters expected).
    c.equal("chapter count", n_ch, 27)
    c.at_least("section count", n_sec, 100)
    c.at_least("subsection count", n_sub, 300)

    # Every chapter has a non-null title and a non-zero chapter_num.
    bad = con.execute(
        "SELECT COUNT(*) AS n FROM chapters "
        "WHERE chapter_num IS NULL OR chapter_num <= 0 OR title IS NULL OR title = ''"
    ).fetchone()["n"]
    c.equal("chapter rows have chapter_num + title", bad, 0)

    # chapter_num uniqueness (the schema's UNIQUE constraint).
    dupes = con.execute(
        "SELECT chapter_num, COUNT(*) AS n FROM chapters "
        "GROUP BY chapter_num HAVING n > 1"
    ).fetchall()
    c.equal("chapter_num uniqueness", len(dupes), 0)

    # Every section's chapter_id is real.
    bad = con.execute(
        "SELECT COUNT(*) AS n FROM sections s "
        "LEFT JOIN chapters c ON c.chapter_id = s.chapter_id "
        "WHERE c.chapter_id IS NULL"
    ).fetchone()["n"]
    c.equal("sections.chapter_id resolves", bad, 0)

    # Every subsection's section_id is real.
    bad = con.execute(
        "SELECT COUNT(*) AS n FROM subsections sub "
        "LEFT JOIN sections s ON s.section_id = sub.section_id "
        "WHERE s.section_id IS NULL"
    ).fetchone()["n"]
    c.equal("subsections.section_id resolves", bad, 0)


def check_static_content(con: sqlite3.Connection, c: Checks) -> None:
    c.start("Summaries + abbreviations")
    n_sub = con.execute("SELECT COUNT(*) FROM subsections").fetchone()[0]
    n_sub_summary = con.execute(
        "SELECT COUNT(*) FROM subsections WHERE summary IS NOT NULL AND summary != ''"
    ).fetchone()[0]
    print(f"  → subsections with summary: {n_sub_summary}/{n_sub}")
    # Allow up to 5 missing summaries (orphan subsections from ingestion drift).
    coverage = n_sub_summary / n_sub if n_sub else 0
    c.check("subsection summary coverage ≥ 95%",
            lambda v=coverage: v >= 0.95,
            f"coverage = {coverage:.1%}")

    n_sec = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    n_sec_summary = con.execute(
        "SELECT COUNT(*) FROM sections WHERE summary IS NOT NULL AND summary != ''"
    ).fetchone()[0]
    print(f"  → sections with summary:    {n_sec_summary}/{n_sec}")
    coverage_s = n_sec_summary / n_sec if n_sec else 0
    c.check("section summary coverage ≥ 95%",
            lambda v=coverage_s: v >= 0.95,
            f"coverage = {coverage_s:.1%}")

    n_abbr = con.execute("SELECT COUNT(*) FROM topic_abbreviations").fetchone()[0]
    print(f"  → topic abbreviations:      {n_abbr}")
    c.at_least("topic_abbreviations rows ≥ 20", n_abbr, 20)


def check_user_data_clean(con: sqlite3.Connection, c: Checks) -> None:
    c.start("User-data tables (clean-slate guarantee)")
    for tbl in ("students", "sessions", "subsection_mastery", "observations", "messages"):
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        c.equal(f"`{tbl}` row count", n, 0)


def check_resolver_round_trip(c: Checks) -> None:
    c.start("Path ↔ FK resolver round-trip")
    from memory.sqlite_store import SQLiteStore
    store = SQLiteStore()

    # Pick a known subsection and round-trip through both helpers.
    cur = store._conn().execute(
        """
        SELECT
            'Chapter ' || c.chapter_num || ': ' || c.title || ' > ' ||
            s.title || ' > ' || sub.title AS path,
            sub.subsection_id
        FROM subsections sub
        JOIN sections s ON s.section_id = sub.section_id
        JOIN chapters c ON c.chapter_id = s.chapter_id
        ORDER BY c.chapter_num, s.section_order, sub.subsection_order
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        c.check("at least one subsection exists", lambda: False)
        return

    canonical = row["path"]
    expected_id = row["subsection_id"]
    stripped = canonical.split(": ", 1)[1] if ": " in canonical else canonical
    # Drop the "Chapter N: " prefix; resolver must accept both forms.
    stripped = stripped.split(" > ", 1)[1] if " > " in stripped else stripped
    # Reconstruct the prefix-free 3-segment form: chapter > section > subsection
    parts = canonical.split(" > ")
    prefix_free = parts[0].split(": ", 1)[1] + " > " + parts[1] + " > " + parts[2]

    sid_full = store.resolve_subsection_id(canonical)
    sid_stripped = store.resolve_subsection_id(prefix_free)

    c.equal("resolve_subsection_id (with prefix)", sid_full, expected_id)
    c.equal("resolve_subsection_id (no prefix)", sid_stripped, expected_id)
    c.equal("subsection_path_for_id round-trip",
            store.subsection_path_for_id(expected_id), canonical)

    # Unknown path returns None instead of crashing.
    c.equal("resolver returns None for unknown",
            store.resolve_subsection_id("Made Up Chapter > Made Up Section > Made Up Sub"),
            None)


def check_functional_loaders(c: Checks) -> None:
    c.start("Loaders read from SQL (no JSON file dependency at runtime)")

    from memory.sqlite_store import SQLiteStore, load_chapter_title_lookup
    from retrieval.topic_suggester import TopicSuggester
    from retrieval.topic_matcher import TopicMatcher
    from retrieval.topic_mapper_llm import (
        build_toc_block,
        build_toc_block_compact,
        build_abbreviations_block,
    )

    # mastery_tree without topic_index argument.
    tree = SQLiteStore().mastery_tree("nonexistent_student")
    n_chapters = len(tree["chapters"])
    n_subs = sum(len(s["subsections"]) for c2 in tree["chapters"] for s in c2["sections"])
    print(f"  → mastery_tree: {n_chapters} chapters, {n_subs} subsections")
    c.equal("mastery_tree chapter count", n_chapters, 27)
    c.at_least("mastery_tree subsection count", n_subs, 300)

    # Chapter title lookup
    lookup = load_chapter_title_lookup()
    print(f"  → chapter_title_lookup: {len(lookup)} entries")
    c.equal("chapter_title_lookup size", len(lookup), 27)

    # TopicSuggester
    leaves = TopicSuggester().all_leaf_topics()
    print(f"  → TopicSuggester leaves:    {len(leaves)}")
    c.at_least("TopicSuggester leaves", len(leaves), 300)

    # TopicMatcher
    tm = TopicMatcher()
    print(f"  → TopicMatcher entries:     {len(tm)}")
    c.at_least("TopicMatcher entries", len(tm), 300)

    # TOC blocks (the heavy ones used by the Haiku topic mapper)
    full = build_toc_block()
    compact = build_toc_block_compact()
    abbrev = build_abbreviations_block()
    print(f"  → TOC full / compact / abbreviations: {len(full):,} / {len(compact):,} / {len(abbrev)} chars")
    c.at_least("full TOC chars", len(full), 50_000)
    c.at_least("compact TOC chars", len(compact), 10_000)
    c.at_least("abbreviations chars", len(abbrev), 1_000)


# ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list every passing check")
    args = ap.parse_args()

    c = Checks(verbose=args.verbose)
    con = open_db()
    try:
        check_migrations(con, c)
        check_tables(con, c)
        check_indexes(con, c)
        check_foreign_keys(con, c)
        check_referential_integrity(con, c)
        check_curriculum_counts(con, c)
        check_static_content(con, c)
        check_user_data_clean(con, c)
        check_resolver_round_trip(c)
        check_functional_loaders(c)
    finally:
        con.close()
    return c.report()


if __name__ == "__main__":
    sys.exit(main())
