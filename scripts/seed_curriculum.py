#!/usr/bin/env python3
"""
Seed chapters/sections/subsections from textbook_structure.json + topic_index.json.

Run once after migration 003. Idempotent: re-running is safe — uses
INSERT OR IGNORE and only adds rows that don't already exist.

Strategy:
    1. Parse `data/textbook_structure.json` → chapter_num lookup (the JSON's
       top-level keys are "Chapter N: <title>", which is the only place the
       chapter number lives).
    2. Walk `data/topic_index.json` (363 entries) — the canonical subsection
       list with display_label / chunk_count metadata.
    3. Insert chapters, then sections (FK chapters), then subsections
       (FK sections). Track ids in a dict to resolve FKs without a second
       SELECT round trip.

Why both files: textbook_structure has chapter_num but inconsistent
subsection lists; topic_index is the curated production subsection list
but lacks chapter_num. Combine to get both.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memory.sqlite_store import SQLiteStore


def load_chapter_num_lookup() -> dict[str, int]:
    """Build {chapter_title: chapter_num} from textbook_structure.json keys.

    Example key: "Chapter 24: Metabolism and Nutrition"
        → {"Metabolism and Nutrition": 24}
    """
    structure = json.loads((REPO / "data" / "textbook_structure.json").read_text())
    out: dict[str, int] = {}
    for key in structure:
        m = re.match(r"^Chapter (\d+):\s*(.+)$", key)
        if m:
            out[m.group(2).strip()] = int(m.group(1))
    return out


def main() -> None:
    chapter_lookup = load_chapter_num_lookup()
    print(f"loaded chapter_num lookup: {len(chapter_lookup)} chapters")

    topic_index = json.loads((REPO / "data" / "topic_index.json").read_text())
    items = topic_index if isinstance(topic_index, list) else list(topic_index.values())
    print(f"topic_index entries: {len(items)}")

    store = SQLiteStore()
    conn = store._conn()

    # Track id mappings as we go so child rows can FK without re-SELECTing
    chapter_ids: dict[str, int] = {}   # chapter_title -> chapter_id
    section_ids: dict[tuple[int, str], int] = {}  # (chapter_id, section_title) -> section_id
    subsection_count = 0
    section_order_counters: dict[int, int] = {}    # chapter_id -> running order
    subsection_order_counters: dict[int, int] = {}  # section_id  -> running order

    for entry in items:
        ch_title = (entry.get("chapter") or "").strip()
        sec_title = (entry.get("section") or "").strip()
        sub_title = (entry.get("subsection") or "").strip()
        if not (ch_title and sec_title and sub_title):
            continue

        ch_num = chapter_lookup.get(ch_title)
        if ch_num is None:
            # Skip entries that can't be resolved to a numbered chapter —
            # they'd violate the chapter_num NOT NULL constraint.
            continue

        # Insert / fetch chapter
        if ch_title not in chapter_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO chapters(chapter_num, title) VALUES (?, ?)",
                (ch_num, ch_title),
            )
            row = conn.execute(
                "SELECT chapter_id FROM chapters WHERE chapter_num = ?", (ch_num,)
            ).fetchone()
            chapter_ids[ch_title] = row["chapter_id"]
        ch_id = chapter_ids[ch_title]

        # Insert / fetch section
        sec_key = (ch_id, sec_title)
        if sec_key not in section_ids:
            section_order_counters[ch_id] = section_order_counters.get(ch_id, 0) + 1
            conn.execute(
                "INSERT OR IGNORE INTO sections(chapter_id, title, section_order) "
                "VALUES (?, ?, ?)",
                (ch_id, sec_title, section_order_counters[ch_id]),
            )
            row = conn.execute(
                "SELECT section_id FROM sections WHERE chapter_id = ? AND title = ?",
                (ch_id, sec_title),
            ).fetchone()
            section_ids[sec_key] = row["section_id"]
        sec_id = section_ids[sec_key]

        # Insert subsection
        subsection_order_counters[sec_id] = subsection_order_counters.get(sec_id, 0) + 1
        conn.execute(
            "INSERT OR IGNORE INTO subsections("
            "section_id, title, display_label, summary, subsection_order"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                sec_id,
                sub_title,
                entry.get("display_label") or sub_title,
                None,  # summary — populated separately by raptor pipeline if available
                subsection_order_counters[sec_id],
            ),
        )
        subsection_count += 1

    conn.commit()

    # Report
    n_ch = conn.execute("SELECT COUNT(*) AS n FROM chapters").fetchone()["n"]
    n_sec = conn.execute("SELECT COUNT(*) AS n FROM sections").fetchone()["n"]
    n_sub = conn.execute("SELECT COUNT(*) AS n FROM subsections").fetchone()["n"]
    print(f"seeded: chapters={n_ch}  sections={n_sec}  subsections={n_sub}")
    print(f"input topic_index entries processed: {subsection_count}")


if __name__ == "__main__":
    main()
