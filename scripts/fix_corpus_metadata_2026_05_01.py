"""
fix_corpus_metadata_2026_05_01.py — corpus-level metadata patches.

Two bugs surfaced by Tier 1 #1.2(c) sample_related investigation
(see progress_journal/2026-05-01_19-05-43_*.md):

X1. Chapter 20 chunks have chapter_title='Circulation' (truncated by the
    chunker). textbook_structure.json key is 'Chapter 20: The
    Cardiovascular System: Blood Vessels and Circulation'. After
    _strip_chapter_prefix, structure expects 'The Cardiovascular System:
    Blood Vessels and Circulation' but chunks say 'Circulation' →
    scripts/build_topic_index.py join fails → 0 ch20 entries in
    topic_index.json → no card suggestions for blood-vessel /
    aorta / pulmonary-circulation queries.

    439 chunks affected. Plus 12 garbage 'REFERENCES' chunks at chapter_num=28
    that look like bibliography pages and should be dropped entirely.

X2. 6 chunks have subsection_title='Diseases of the…' (truncated, with
    literal ellipsis char). textbook_structure.json line 1201 has
    'Disorders of the...' (3 dot chars, not ellipsis). Word also
    differs (Diseases vs Disorders). Neither side is canonical; the
    PDF parse mis-extracted the same subsection two different ways.

    Pragmatic fix: rename both to a clean placeholder
    'Disorders of the Cardiovascular System' so they match each other
    AND the structure rebuild finds them. Not the canonical OpenStax
    title (would need PDF re-extraction to know the full title), but
    functional and stable.

What this script does
---------------------
  1. Reads data/processed/chunks_openstax_anatomy.jsonl
  2. Backs up to chunks_openstax_anatomy.jsonl.pre_2026_05_01.bak
  3. For each chunk:
     - if chapter_num=20 and chapter_title='Circulation':
       rewrite chapter_title → 'The Cardiovascular System: Blood Vessels and Circulation'
     - if chapter_title='REFERENCES': drop the chunk (bibliography junk)
     - if subsection_title contains '…' or '...':
       rewrite to 'Disorders of the Cardiovascular System'
  4. Atomic write back to chunks_openstax_anatomy.jsonl
  5. For Qdrant collection sokratic_kb_chunks: set_payload on the
     affected points (same chapter_title + subsection_title fixes),
     delete REFERENCES points.
  6. Print before/after counts as proof.

Idempotent — safe to re-run; second run finds nothing to fix.

Run:
    .venv/bin/python scripts/fix_corpus_metadata_2026_05_01.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env", override=True)

CHUNKS_PATH = ROOT / "data" / "processed" / "chunks_openstax_anatomy.jsonl"
BACKUP_PATH = ROOT / "data" / "processed" / "chunks_openstax_anatomy.jsonl.pre_2026_05_01.bak"

# Fix mappings
OLD_CH20_TITLE = "Circulation"
NEW_CH20_TITLE = "The Cardiovascular System: Blood Vessels and Circulation"

JUNK_CHAPTER_TITLE = "REFERENCES"

ELLIPSIS_CHARS = ("…", "...")
NEW_DISEASES_SUBSECTION = "Disorders of the Cardiovascular System"


def _is_truncated_subsection(sub: str) -> bool:
    if not sub:
        return False
    return any(e in sub for e in ELLIPSIS_CHARS)


def patch_chunks_jsonl(dry_run: bool = False) -> dict:
    """Patch the chunks JSONL. Returns stats dict."""
    if not CHUNKS_PATH.exists():
        raise SystemExit(f"chunks file not found: {CHUNKS_PATH}")

    if not BACKUP_PATH.exists():
        if dry_run:
            print(f"[dry-run] would back up {CHUNKS_PATH.name} → {BACKUP_PATH.name}")
        else:
            shutil.copy2(CHUNKS_PATH, BACKUP_PATH)
            print(f"backed up {CHUNKS_PATH.name} → {BACKUP_PATH.name}")
    else:
        print(f"backup already exists at {BACKUP_PATH.name} (skipping re-backup)")

    stats = {
        "input_chunks": 0,
        "ch20_title_fixed": 0,
        "subsection_truncation_fixed": 0,
        "references_dropped": 0,
        "output_chunks": 0,
        "ch20_chunks_with_title_fix_ids": [],
        "subsection_fix_chunk_ids": [],
        "dropped_chunk_ids": [],
    }

    out_lines: list[str] = []
    with open(CHUNKS_PATH, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            stats["input_chunks"] += 1
            c = json.loads(line)
            cid = c.get("chunk_id", "?")
            ch_num = c.get("chapter_num")
            ch_title = (c.get("chapter_title") or "").strip()
            sub_title = (c.get("subsection_title") or "").strip()

            # X1.b — drop REFERENCES junk
            if ch_title == JUNK_CHAPTER_TITLE:
                stats["references_dropped"] += 1
                stats["dropped_chunk_ids"].append(cid)
                continue

            # X1.a — Ch20 chapter_title truncation
            if ch_num == 20 and ch_title == OLD_CH20_TITLE:
                c["chapter_title"] = NEW_CH20_TITLE
                stats["ch20_title_fixed"] += 1
                stats["ch20_chunks_with_title_fix_ids"].append(cid)

            # X2 — subsection title truncation
            if _is_truncated_subsection(sub_title):
                c["subsection_title"] = NEW_DISEASES_SUBSECTION
                stats["subsection_truncation_fixed"] += 1
                stats["subsection_fix_chunk_ids"].append(cid)

            out_lines.append(json.dumps(c, ensure_ascii=False))
            stats["output_chunks"] += 1

    print(f"\nChunk patch summary:")
    print(f"  input chunks:            {stats['input_chunks']}")
    print(f"  ch20 chapter_title fix:  {stats['ch20_title_fixed']}")
    print(f"  subsection trunc fix:    {stats['subsection_truncation_fixed']}")
    print(f"  REFERENCES dropped:      {stats['references_dropped']}")
    print(f"  output chunks:           {stats['output_chunks']}")

    if dry_run:
        print(f"\n[dry-run] would write {len(out_lines)} chunks to {CHUNKS_PATH}")
        return stats

    # Atomic write
    tmp = CHUNKS_PATH.with_suffix(CHUNKS_PATH.suffix + ".tmp")
    with open(tmp, "w") as f:
        for line in out_lines:
            f.write(line + "\n")
    os.replace(tmp, CHUNKS_PATH)
    print(f"\nwrote {len(out_lines)} chunks to {CHUNKS_PATH}")
    return stats


def patch_qdrant_payloads(stats: dict, dry_run: bool = False) -> dict:
    """Patch Qdrant payloads for the same chunks. Drops REFERENCES points."""
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as rest

    qd_stats = {
        "ch20_payload_updates": 0,
        "subsection_payload_updates": 0,
        "references_deleted": 0,
    }

    client = QdrantClient(url="http://localhost:6333")
    collection = "sokratic_kb_chunks"

    # Verify collection exists
    info = client.get_collection(collection)
    print(f"\nQdrant '{collection}' has {info.points_count} points (pre-fix)")

    # Strategy: scroll through points, identify the affected ones by chunk_id,
    # then set_payload / delete in batches. The chunk_id is in the payload.
    # Build set of chunk_ids to update / delete.
    ch20_ids = set(stats["ch20_chunks_with_title_fix_ids"])
    sub_ids = set(stats["subsection_fix_chunk_ids"])
    drop_ids = set(stats["dropped_chunk_ids"])
    print(f"  ch20 chapter_title patches needed: {len(ch20_ids)}")
    print(f"  subsection patches needed:         {len(sub_ids)}")
    print(f"  REFERENCES points to delete:       {len(drop_ids)}")

    # Build mapping from chunk_id to Qdrant point id by scrolling the collection.
    print(f"\nScrolling Qdrant to map chunk_id → point_id...")
    point_id_by_chunk_id: dict[str, str | int] = {}
    next_offset = None
    scrolled = 0
    while True:
        batch, next_offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in batch:
            cid = (p.payload or {}).get("chunk_id")
            if cid in ch20_ids or cid in sub_ids or cid in drop_ids:
                point_id_by_chunk_id[cid] = p.id
        scrolled += len(batch)
        if next_offset is None:
            break
    print(f"  scrolled {scrolled} points; matched {len(point_id_by_chunk_id)} affected")

    if dry_run:
        print(f"[dry-run] would set_payload on ch20 + subsection points, delete REFERENCES")
        return qd_stats

    # Apply ch20 chapter_title patches
    for cid in ch20_ids:
        pid = point_id_by_chunk_id.get(cid)
        if pid is None:
            continue
        client.set_payload(
            collection_name=collection,
            payload={"chapter_title": NEW_CH20_TITLE},
            points=[pid],
            wait=False,
        )
        qd_stats["ch20_payload_updates"] += 1

    # Apply subsection patches
    for cid in sub_ids:
        pid = point_id_by_chunk_id.get(cid)
        if pid is None:
            continue
        client.set_payload(
            collection_name=collection,
            payload={"subsection_title": NEW_DISEASES_SUBSECTION},
            points=[pid],
            wait=False,
        )
        qd_stats["subsection_payload_updates"] += 1

    # Delete REFERENCES points
    drop_pids = [point_id_by_chunk_id[cid] for cid in drop_ids
                 if cid in point_id_by_chunk_id]
    if drop_pids:
        client.delete(
            collection_name=collection,
            points_selector=rest.PointIdsList(points=drop_pids),
            wait=True,
        )
        qd_stats["references_deleted"] = len(drop_pids)

    info = client.get_collection(collection)
    print(f"\nQdrant '{collection}' now has {info.points_count} points")
    print(f"  payload updates ch20: {qd_stats['ch20_payload_updates']}")
    print(f"  payload updates sub:  {qd_stats['subsection_payload_updates']}")
    print(f"  points deleted:       {qd_stats['references_deleted']}")
    return qd_stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    args = ap.parse_args()

    print(f"=== Corpus metadata fix (2026-05-01) ===\n")
    stats = patch_chunks_jsonl(dry_run=args.dry_run)
    qd = patch_qdrant_payloads(stats, dry_run=args.dry_run)

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
