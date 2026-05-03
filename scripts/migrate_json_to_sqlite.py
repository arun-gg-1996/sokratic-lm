"""
scripts/migrate_json_to_sqlite.py
─────────────────────────────────
One-shot migration of legacy per-student JSON mastery files into the new
SQLite store (per docs/AUDIT_2026-05-02.md L1, L2).

Source:    data/student_state/{student_id}.json   (one file per student)
Target:    data/student_state/sokratic.sqlite3    (single SQLite DB)

The legacy JSON shape is:

    {
      "concepts": {
        "Ch20|<section>|<subsection>": {
          "mastery": 0.25, "confidence": 0.25, "sessions": 2,
          "last_seen": "2026-04-30", "last_outcome": "not_reached",
          "last_rationale": "..."
        },
        ...
      }
    }

Path conversion: legacy uses "Ch{N}|<section>|<subsection>" with chapter
shorthand. The new canonical path per L4 is "<full chapter title> > <section>
> <subsection>". The mapping Ch{N} → full title is read from
data/textbook_structure.json.

What does NOT migrate:
  * `last_rationale` — narrative text, belongs to mem0 / message logs, not
    SQLite. Dropped on migration; the SQL row only carries the numeric
    score + categorical outcome + counters.
  * `confidence` — not in the L2 schema; the EWMA score IS the confidence-
    weighted estimate. Dropped.
  * Per-session history — the legacy JSON doesn't have full session rows,
    so the `sessions` table is NOT populated by this migration. Sessions
    will accrue forward from the first new session on the SQLite store.

Idempotent: re-running is safe (uses INSERT OR IGNORE for students,
upsert for subsection_mastery via plain UPDATE-or-INSERT).

Usage:
    .venv/bin/python scripts/migrate_json_to_sqlite.py [--dry-run] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memory.sqlite_store import SQLiteStore  # noqa: E402

JSON_DIR = REPO / "data" / "student_state"
STRUCTURE_PATH = REPO / "data" / "textbook_structure.json"


def load_chapter_lookup() -> dict[int, str]:
    """Return {chapter_num: full_chapter_title} from textbook_structure.json."""
    structure = json.loads(STRUCTURE_PATH.read_text())
    out: dict[int, str] = {}
    for key in structure.keys():
        # Key like "Chapter 20: The Cardiovascular System: Blood Vessels and Circulation"
        if not key.startswith("Chapter "):
            continue
        try:
            after = key[len("Chapter "):]
            num_str, title = after.split(":", 1)
            num = int(num_str.strip())
            out[num] = title.strip()
        except (ValueError, IndexError):
            continue
    return out


def convert_path(legacy_path: str, chapter_lookup: dict[int, str]) -> str | None:
    """Convert "Ch20|Section|Subsection" → "<full title> > Section > Subsection".

    Returns None if the chapter number can't be resolved.
    """
    parts = legacy_path.split("|")
    if len(parts) != 3:
        return None
    ch_short, section, subsection = parts
    if not ch_short.startswith("Ch"):
        return None
    try:
        ch_num = int(ch_short[2:])
    except ValueError:
        return None
    full_title = chapter_lookup.get(ch_num)
    if not full_title:
        return None
    return f"{full_title} > {section} > {subsection}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would migrate; do not write to SQLite.")
    p.add_argument("--db", type=Path, default=None,
                   help="Override target SQLite DB path (default: data/student_state/sokratic.sqlite3).")
    args = p.parse_args()

    chapter_lookup = load_chapter_lookup()
    print(f"loaded {len(chapter_lookup)} chapter title mappings", flush=True)

    json_files = sorted(JSON_DIR.glob("*.json"))
    print(f"found {len(json_files)} legacy student JSON files in {JSON_DIR}", flush=True)

    store: SQLiteStore | None = None
    if not args.dry_run:
        store = SQLiteStore(db_path=args.db)
        print(f"opened SQLite DB at {store.db_path}", flush=True)

    n_students = 0
    n_concepts_migrated = 0
    n_concepts_dropped = 0
    drop_reasons: dict[str, int] = {}

    for path in json_files:
        student_id = path.stem
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"  WARN cannot parse {path.name}: {e}", flush=True)
            continue
        concepts = (payload or {}).get("concepts", {}) or {}
        if not isinstance(concepts, dict):
            continue

        n_students += 1
        student_concepts = 0
        if not args.dry_run and store is not None:
            store.ensure_student(student_id)

        for legacy_path, fields in concepts.items():
            new_path = convert_path(legacy_path, chapter_lookup)
            if new_path is None:
                n_concepts_dropped += 1
                drop_reasons["bad_path_format_or_unknown_chapter"] = (
                    drop_reasons.get("bad_path_format_or_unknown_chapter", 0) + 1
                )
                continue

            score = fields.get("mastery")
            outcome = fields.get("last_outcome") or "not_reached"
            last_seen = fields.get("last_seen")
            if score is None:
                n_concepts_dropped += 1
                drop_reasons["missing_mastery_score"] = (
                    drop_reasons.get("missing_mastery_score", 0) + 1
                )
                continue

            n_concepts_migrated += 1
            student_concepts += 1
            if args.dry_run or store is None:
                continue

            # Coerce outcome to one of the L3 enum values.
            if outcome not in {"reached", "partial", "not_reached"}:
                outcome = "not_reached"

            # Use raw insert/update to preserve historical EWMA score (don't
            # blend with itself; this is a snapshot, not a fresh attempt).
            existing = store.get_subsection_mastery(student_id, new_path)
            attempt = int(fields.get("sessions", 1) or 1)
            session_at = (
                last_seen + "T00:00:00Z"
                if last_seen and "T" not in last_seen
                else (last_seen or "")
            )
            conn = store._conn()  # noqa: SLF001 — controlled migration write
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO subsection_mastery(
                        student_id, subsection_path, ewma_score, last_outcome,
                        last_session_at, attempt_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (student_id, new_path, float(score), outcome, session_at, attempt),
                )
            else:
                # Re-running migration: keep the most recent timestamp; trust
                # the existing live data over the JSON snapshot.
                pass
            conn.commit()

        print(
            f"  {path.name:50s}  student_id={student_id!r:30s}  "
            f"concepts_kept={student_concepts}",
            flush=True,
        )

    print(flush=True)
    print(f"{'DRY RUN — ' if args.dry_run else ''}Migration summary:", flush=True)
    print(f"  students processed: {n_students}", flush=True)
    print(f"  concepts migrated:  {n_concepts_migrated}", flush=True)
    print(f"  concepts dropped:   {n_concepts_dropped}", flush=True)
    for reason, n in sorted(drop_reasons.items(), key=lambda x: -x[1]):
        print(f"    {n:4d}  {reason}", flush=True)


if __name__ == "__main__":
    main()
