"""
Finalize data-prep invariants — fixes the 3 residual issues surfaced by
scripts/validate_data_prep_invariants.py after the L76 + L19 + L38 + reindex pass:

  1. Ch20 chapter rename: chunks with chapter_title="Circulation" need to be
     rewritten to "The Cardiovascular System: Blood Vessels and Circulation"
     (the canonical title in textbook_structure.json post-Nidhi's fix).
  2. Drop chunks with chapter_title="REFERENCES" (bibliography junk that
     leaked through chunking — not teachable content).
  3. Append legitimate chunker-extracted subsections (e.g. "Heart Valves",
     "Heart: Cardiac Tamponade") to textbook_structure.json with
     source="chunker_extracted" tag. These were always present in chunks but
     missing from structure because the structure-parser pass dropped them.

After this script runs, the validation gate should be ALL GREEN.

Usage:
  .venv/bin/python scripts/finalize_data_prep_invariants.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CHUNKS_PATH = REPO / "data" / "processed" / "chunks_openstax_anatomy.jsonl"
STRUCT_PATH = REPO / "data" / "textbook_structure.json"

CH20_OLD = "Circulation"
CH20_NEW = "The Cardiovascular System: Blood Vessels and Circulation"

DROP_CHAPTERS = {"REFERENCES"}


def load_chunks() -> list[dict]:
    return [json.loads(l) for l in CHUNKS_PATH.open()]


def load_structure() -> dict:
    return json.loads(STRUCT_PATH.read_text())


def build_struct_keys(structure: dict) -> set[tuple]:
    out = set()
    for ck, cn in structure.items():
        if not isinstance(cn, dict):
            continue
        title = ck.split(":", 1)[1].strip() if ":" in ck else ck
        sections = cn.get("sections", {})
        if isinstance(sections, dict):
            for sn, sv in sections.items():
                subs = (sv or {}).get("subsections", {})
                if isinstance(subs, dict):
                    for sub in subs:
                        out.add((title, sn, sub))
    return out


def find_chapter_key(structure: dict, chapter_title: str) -> str | None:
    """Return the structure dict key (e.g. 'Chapter 10: Muscle Tissue') for a chapter title."""
    for k in structure.keys():
        ct = k.split(":", 1)[1].strip() if ":" in k else k
        if ct == chapter_title:
            return k
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    chunks = load_chunks()
    structure = load_structure()
    print(f"loaded {len(chunks)} chunks", flush=True)

    # ── Fix 1 + 2: rename Ch20 + drop REFERENCES chunks ──────────────────
    n_renamed = 0
    n_dropped = 0
    out_chunks = []
    for c in chunks:
        ct = c.get("chapter_title")
        if ct in DROP_CHAPTERS:
            n_dropped += 1
            continue
        if ct == CH20_OLD:
            c["chapter_title"] = CH20_NEW
            n_renamed += 1
        out_chunks.append(c)
    print(f"  rename Ch20 'Circulation' -> full title: {n_renamed} chunks", flush=True)
    print(f"  drop REFERENCES bibliography chunks: {n_dropped} chunks", flush=True)

    # ── Fix 3: identify chunker-extracted subsections missing from structure ──
    struct_keys = build_struct_keys(structure)
    chunk_keys: set[tuple] = set()
    for c in out_chunks:
        chunk_keys.add(
            (
                c.get("chapter_title") or "",
                c.get("section_title") or "",
                c.get("subsection_title") or "",
            )
        )
    missing = chunk_keys - struct_keys
    print(f"  chunker-extracted subsections to append to structure: {len(missing)} unique tuples", flush=True)

    appended = 0
    not_appendable = []
    for (ch_title, sec_title, sub_title) in sorted(missing):
        ck = find_chapter_key(structure, ch_title)
        if not ck:
            not_appendable.append(("no-chapter", (ch_title, sec_title, sub_title)))
            continue
        ch_node = structure[ck]
        sections = ch_node.setdefault("sections", {})
        if not isinstance(sections, dict):
            not_appendable.append(("sections-not-dict", (ch_title, sec_title, sub_title)))
            continue
        sec_node = sections.get(sec_title)
        if sec_node is None:
            # Section doesn't exist either — create it as chunker_extracted too
            sec_node = {"difficulty": "moderate", "source": "chunker_extracted", "subsections": {}}
            sections[sec_title] = sec_node
        subs = sec_node.setdefault("subsections", {})
        if not isinstance(subs, dict):
            not_appendable.append(("subsections-not-dict", (ch_title, sec_title, sub_title)))
            continue
        if sub_title not in subs:
            subs[sub_title] = {
                "difficulty": "moderate",
                "source": "chunker_extracted",
            }
            appended += 1
    print(f"  appended {appended} subsections to structure", flush=True)
    if not_appendable:
        print(f"  WARN: {len(not_appendable)} could not be appended:", flush=True)
        for reason, key in not_appendable[:5]:
            print(f"    {reason}: {key}", flush=True)

    if args.dry_run:
        print("\nDRY RUN — no files written", flush=True)
        return

    # Write
    shutil.copy(CHUNKS_PATH, str(CHUNKS_PATH) + ".pre_finalize.bak")
    shutil.copy(STRUCT_PATH, str(STRUCT_PATH) + ".pre_finalize.bak")

    with CHUNKS_PATH.open("w") as f:
        for c in out_chunks:
            f.write(json.dumps(c) + "\n")
    print(f"\nwrote {CHUNKS_PATH} ({len(out_chunks)} chunks)", flush=True)

    STRUCT_PATH.write_text(json.dumps(structure, indent=2))
    print(f"wrote {STRUCT_PATH}", flush=True)
    print("\nNote: BM25 + Qdrant should be re-indexed (chunks file changed).", flush=True)
    print("Run: .venv/bin/python scripts/reindex_chunks.py", flush=True)


if __name__ == "__main__":
    main()
