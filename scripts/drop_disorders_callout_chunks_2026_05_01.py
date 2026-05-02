"""
drop_disorders_callout_chunks_2026_05_01.py — finishes X2 cleanup.

The previous fix (fix_corpus_metadata_2026_05_01.py) renamed the 6
truncated 'Diseases of the…' / 'Disorders of the...' chunks to a
placeholder 'Disorders of the Cardiovascular System'. But:

- The textbook_structure.json has 46 'Disorders of the...' /
  'Aging and the...' callout-box entries marked as junk patterns
  (config/domains/ot.yaml:60-66 — `DISORDERS OF THE`, `AGING AND THE`
  are explicit junk-pattern substrings).
- These callout boxes are pedagogical sidebars in OpenStax (clinical
  imbalances / age-related changes), not core teachable content.
- Renaming our 6 chunks to a similar string still keeps them outside
  the teachable surface AND clutters retrieval with non-teachable
  content the topic-index can't reference.

Cleanest fix: drop them outright. Qdrant + JSONL.
6 chunks affected, all with subsection_title 'Disorders of the
Cardiovascular System' (renamed) — these are the same chunks the prior
script identified as truncation cases.

Idempotent — second run finds no chunks matching.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env", override=True)

CHUNKS_PATH = ROOT / "data" / "processed" / "chunks_openstax_anatomy.jsonl"
# Original target (X2 round): the 6 chunks renamed from "Diseases of the…"
# Extended (Tier 1 #1.4 e2e bug A1.2): also drop callout chunks whose
# subsection_title matches OpenStax's pedagogical-sidebar patterns. The
# topic_index junk filter keeps these out of the teachable surface, but
# they were still in JSONL+Qdrant and could be picked up by dean.py's
# vote-based lock. Found 40 chunks across 3 subsections.
TARGET_SUBSECTION = "Disorders of the Cardiovascular System"
JUNK_SUBSECTION_PATTERNS = (
    "aging and",                  # "Aging and Muscle Tissue", etc.
    "tissue and aging",
    "disorders of the",
    "interactive link",
    "career connection",
    "everyday connection",
    "homeostatic imbalances",     # "Cancer Arises from Homeostatic Imbalances"
)


def _is_junk_subsection(sub: str) -> bool:
    if not sub:
        return False
    s = sub.lower()
    return any(p in s for p in JUNK_SUBSECTION_PATTERNS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"=== Dropping callout-box chunks (X2 cleanup) ===\n")

    # Pass 1: identify
    drop_ids: list[str] = []
    out_lines: list[str] = []
    n_in = 0
    n_target = 0
    n_junk_pattern = 0
    with open(CHUNKS_PATH, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            n_in += 1
            c = json.loads(line)
            sub = (c.get("subsection_title") or "").strip()
            if sub == TARGET_SUBSECTION:
                drop_ids.append(c.get("chunk_id", "?"))
                n_target += 1
                continue
            if _is_junk_subsection(sub):
                drop_ids.append(c.get("chunk_id", "?"))
                n_junk_pattern += 1
                continue
            out_lines.append(line)
    print(f"  by category: target_subsection={n_target}, junk_pattern={n_junk_pattern}")

    print(f"Input chunks:  {n_in}")
    print(f"Drop count:    {len(drop_ids)}")
    print(f"Output chunks: {len(out_lines)}")

    if args.dry_run:
        print(f"\n[dry-run] would drop {len(drop_ids)} chunks; not writing")
        return

    if drop_ids:
        tmp = CHUNKS_PATH.with_suffix(CHUNKS_PATH.suffix + ".tmp")
        with open(tmp, "w") as f:
            for line in out_lines:
                f.write(line + "\n")
        os.replace(tmp, CHUNKS_PATH)
        print(f"wrote {len(out_lines)} chunks to {CHUNKS_PATH}")

        # Qdrant cleanup
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as rest

        client = QdrantClient(url="http://localhost:6333")
        collection = "sokratic_kb_chunks"
        info = client.get_collection(collection)
        print(f"\nQdrant '{collection}' has {info.points_count} points (pre-drop)")

        # Map chunk_id -> point_id
        drop_set = set(drop_ids)
        point_id_by_chunk_id: dict[str, str | int] = {}
        next_offset = None
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
                if cid in drop_set:
                    point_id_by_chunk_id[cid] = p.id
            if next_offset is None:
                break

        drop_pids = list(point_id_by_chunk_id.values())
        if drop_pids:
            client.delete(
                collection_name=collection,
                points_selector=rest.PointIdsList(points=drop_pids),
                wait=True,
            )
            info = client.get_collection(collection)
            print(f"Qdrant '{collection}' now has {info.points_count} points")
            print(f"  deleted: {len(drop_pids)}")
    else:
        print("nothing to drop (idempotent — already clean)")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
