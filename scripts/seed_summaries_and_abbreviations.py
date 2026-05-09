#!/usr/bin/env python3
"""
Backfill subsections.summary, sections.summary, and topic_abbreviations
from the legacy flat-file artifacts. Idempotent — re-runnable.

Inputs (seed-input artifacts; will become unused at runtime after this):
    data/artifacts/raptor_subsection_summaries.jsonl
    data/artifacts/raptor_section_summaries.jsonl
    data/curated_abbrevs_ot.json   (active domain's abbrev file)

Strategy:
    For each summary JSONL line, look up the (chapter_title, section_title,
    subsection_title?) triple in chapters/sections/subsections and UPDATE
    the summary column. Skip rows whose triple doesn't resolve — typically
    these are extracted from textbook content the curriculum tables don't
    list (orphan summaries, ingestion artifacts).

Run after migrations 003 + 004 + scripts/seed_curriculum.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memory.sqlite_store import SQLiteStore


def backfill_subsection_summaries(store: SQLiteStore) -> tuple[int, int]:
    """UPDATE subsections.summary from raptor_subsection_summaries.jsonl.

    Returns (matched, skipped).
    """
    from config import cfg as _cfg
    p = REPO / _cfg.domain_path("raptor_subsection_summaries")
    if not p.exists():
        print(f"  ! missing {p.name} — skipping subsection summaries")
        return (0, 0)

    conn = store._conn()
    matched = 0
    skipped = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        ch_title = (entry.get("chapter_title") or entry.get("chapter") or "").strip()
        sec_title = (entry.get("section_title") or entry.get("section") or "").strip()
        sub_title = (entry.get("subsection_title") or entry.get("subsection") or "").strip()
        summary = (entry.get("summary") or entry.get("text") or "").strip()
        if not (ch_title and sec_title and sub_title and summary):
            skipped += 1
            continue

        cur = conn.execute(
            """
            UPDATE subsections
            SET summary = ?
            WHERE subsection_id = (
                SELECT sub.subsection_id
                FROM subsections sub
                JOIN sections s ON s.section_id = sub.section_id
                JOIN chapters c ON c.chapter_id = s.chapter_id
                WHERE c.title = ? AND s.title = ? AND sub.title = ?
            )
            """,
            (summary, ch_title, sec_title, sub_title),
        )
        if cur.rowcount > 0:
            matched += 1
        else:
            skipped += 1
    conn.commit()
    return matched, skipped


def backfill_section_summaries(store: SQLiteStore) -> tuple[int, int]:
    """UPDATE sections.summary from raptor_section_summaries.jsonl."""
    from config import cfg as _cfg
    p = REPO / _cfg.domain_path("raptor_section_summaries")
    if not p.exists():
        print(f"  ! missing {p.name} — skipping section summaries")
        return (0, 0)

    conn = store._conn()
    matched = 0
    skipped = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        ch_title = (entry.get("chapter_title") or entry.get("chapter") or "").strip()
        sec_title = (entry.get("section_title") or entry.get("section") or "").strip()
        summary = (entry.get("summary") or entry.get("text") or "").strip()
        if not (ch_title and sec_title and summary):
            skipped += 1
            continue

        cur = conn.execute(
            """
            UPDATE sections
            SET summary = ?
            WHERE section_id = (
                SELECT s.section_id
                FROM sections s
                JOIN chapters c ON c.chapter_id = s.chapter_id
                WHERE c.title = ? AND s.title = ?
            )
            """,
            (summary, ch_title, sec_title),
        )
        if cur.rowcount > 0:
            matched += 1
        else:
            skipped += 1
    conn.commit()
    return matched, skipped


def backfill_abbreviations(store: SQLiteStore) -> int:
    """Insert rows into topic_abbreviations from curated_abbrevs_ot.json.

    Schema in source file is one of:
        {"abbreviations": [{"alias": "...", "canonical": "..."}, ...]}
        {"alias1": "canonical1", ...}    # flat dict variant

    INSERT OR IGNORE makes this idempotent.
    """
    from config import cfg as _cfg
    p = REPO / _cfg.domain_path("curated_abbrevs")
    if not p.exists():
        print(f"  ! missing {p.name} — skipping abbreviations")
        return 0

    raw = json.loads(p.read_text())

    # Normalize both schema flavors into list of (alias, canonical, notes) triples.
    rows: list[tuple[str, str, str | None]] = []
    if isinstance(raw, dict) and "abbreviations" in raw:
        for entry in (raw.get("abbreviations") or []):
            if isinstance(entry, dict):
                # The OT abbrevs file uses {short, expansion, context}; older
                # formats used {alias, canonical, notes}. Accept both.
                alias = (entry.get("short") or entry.get("alias") or "").strip()
                canonical = (
                    entry.get("expansion") or entry.get("canonical") or ""
                ).strip()
                notes = entry.get("context") or entry.get("notes")
                if alias and canonical:
                    rows.append((alias, canonical, notes))
    elif isinstance(raw, dict):
        for alias, canonical in raw.items():
            if isinstance(canonical, str):
                rows.append((alias.strip(), canonical.strip(), None))
            elif isinstance(canonical, list):
                for c in canonical:
                    if isinstance(c, str):
                        rows.append((alias.strip(), c.strip(), None))
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                alias = (entry.get("alias") or "").strip()
                canonical = (entry.get("canonical") or "").strip()
                notes = entry.get("notes")
                if alias and canonical:
                    rows.append((alias, canonical, notes))

    if not rows:
        return 0
    conn = store._conn()
    inserted = 0
    for alias, canonical, notes in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO topic_abbreviations(alias, canonical, notes) "
            "VALUES (?, ?, ?)",
            (alias, canonical, notes),
        )
        inserted += cur.rowcount or 0
    conn.commit()
    return inserted


def main() -> None:
    store = SQLiteStore()
    print("Backfilling subsection summaries...")
    matched, skipped = backfill_subsection_summaries(store)
    print(f"  subsection: matched={matched}  skipped={skipped}")

    print("Backfilling section summaries...")
    matched, skipped = backfill_section_summaries(store)
    print(f"  section: matched={matched}  skipped={skipped}")

    print("Backfilling abbreviations...")
    n = backfill_abbreviations(store)
    print(f"  abbreviations: inserted={n}")

    # Coverage report so we know which subsections still lack a summary
    conn = store._conn()
    n_with = conn.execute(
        "SELECT COUNT(*) FROM subsections WHERE summary IS NOT NULL AND summary != ''"
    ).fetchone()[0]
    n_total = conn.execute("SELECT COUNT(*) FROM subsections").fetchone()[0]
    print(f"\nsubsections with summary: {n_with}/{n_total}")
    n_with_sec = conn.execute(
        "SELECT COUNT(*) FROM sections WHERE summary IS NOT NULL AND summary != ''"
    ).fetchone()[0]
    n_total_sec = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    print(f"sections with summary:    {n_with_sec}/{n_total_sec}")
    n_abbr = conn.execute("SELECT COUNT(*) FROM topic_abbreviations").fetchone()[0]
    print(f"topic abbreviations:      {n_abbr}")


if __name__ == "__main__":
    main()
