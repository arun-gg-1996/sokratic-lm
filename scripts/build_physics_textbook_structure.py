#!/usr/bin/env python3
"""
Build data/textbook_structure_openstax_physics.json from the cleaned
physics chunks JSONL.

Mirrors data/textbook_structure.json (anatomy) format:
  {
    "Chapter N: <title>": {
      "difficulty": "easy|moderate|hard",
      "sections": {
        "<section title>": {
          "difficulty": "...",
          "subsections": {
            "<subsection title>": {"difficulty": "..."}
          }
        }
      }
    }
  }

Anatomy's key prefix ("Chapter N: ...") is preserved here so downstream
consumers (build_topic_index, l19_l38, validate_data_prep_invariants) work
identically across both domains.

Inputs:
  data/processed/chunks_openstax_physics.jsonl  (4034 chunks, 100% hierarchy)

Output:
  data/textbook_structure_openstax_physics.json

This is a thin wrapper around ingestion.core.build_structure.build_structure
that re-keys chapter entries with the "Chapter N:" prefix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import cfg  # noqa: E402
from ingestion.core.build_structure import build_structure  # noqa: E402


def main() -> int:
    chunks_path = ROOT / cfg.domain_path("chunks")
    out_path = ROOT / cfg.domain_path("textbook_structure")

    if not chunks_path.exists():
        print(f"ERR: chunks not found: {chunks_path}", file=sys.stderr)
        return 2

    chunks = [json.loads(l) for l in chunks_path.open()]
    print(f"loaded {len(chunks)} chunks from {chunks_path.name}")

    # Re-key chapter_num + chapter_title → "Chapter N: <title>" so the
    # build_structure function indexes by the same key shape anatomy uses.
    # Without this, structure keys would be plain chapter_title strings,
    # which build_topic_index's CHAPTER_NUM_RE wouldn't be able to parse.
    keyed = []
    for c in chunks:
        ch_num = c.get("chapter_num")
        ch_title = c.get("chapter_title", "") or ""
        if not ch_title:
            continue
        keyed_chapter = (
            f"Chapter {ch_num}: {ch_title}" if ch_num is not None else ch_title
        )
        keyed.append({
            "chapter_title": keyed_chapter,
            "section_title": c.get("section_title", "") or "",
            "subsection_title": c.get("subsection_title", "") or "",
        })

    structure = build_structure(keyed)
    print(f"built structure: {len(structure)} chapters")

    # Counts per chapter (sanity check)
    for chap_key in list(structure.keys())[:5]:
        secs = structure[chap_key].get("sections", {})
        n_subs = sum(len(s.get("subsections", {})) for s in secs.values())
        print(f"  {chap_key:60s}  sections={len(secs):2}  subsections={n_subs}")
    if len(structure) > 5:
        print(f"  ... and {len(structure) - 5} more chapters")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(structure, indent=2, ensure_ascii=False))
    print(f"\nwrote: {out_path}")
    print(f"  size: {out_path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
