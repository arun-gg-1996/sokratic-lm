#!/usr/bin/env python3
"""
Deterministic, free repair pass on data/processed/chunks_openstax_physics.jsonl.

Two operations, neither requires LLM:

  1. DROP back-matter chapters that bled through the parser's font-size
     filter (PREFACE, ANSWER KEY, INDEX, APPENDIX*). These have full
     hierarchy populated but the content is non-teachable (preface/credits
     prose, problem-set answer keys, alphabetical index entries, appendix
     reference tables). Anatomy did the equivalent via
     drop_disorders_callout_chunks_2026_05_01.py + finalize_data_prep_invariants.py.

  2. FILL all remaining orphans (any chunk with empty subsection_title)
     by setting subsection_title = section_title. Two source-faithful
     cases:

     (a) Flat sections: section has 0 chunks with a populated subsection
         title — the source PDF has NO L2 heading. Setting
         subsection_title = section_title surfaces the section as one
         teachable topic.

     (b) Mixed sections (the surprising case for physics): the section
         HAS L2 subsections but they're worked-example boxes ("A Ladder
         Resting Against a Wall", "Force on the Cart"). The orphan
         paragraphs are the section's MAIN NARRATIVE — generic prose
         about the concept itself, separate from any example. Naming
         them with section_title gives the topic_index a "main concept"
         entry alongside the example-named L2 subsections, faithful to
         how a student would think about the section.

  Both satisfy the L76 invariant ("every chunk has all 3 hierarchies")
  without inventing structure not present in the textbook, and neither
  needs an LLM call.

LLM-based L76 (use_existing classification) is intentionally NOT used
here — we deep-dived several mixed sections and confirmed orphans are
the section's main narrative, not mis-binned children of existing L2
subsections. Forcing them into example subsections would conflate
conceptually distinct content.

Output:
  chunks_openstax_physics.jsonl                — overwritten
  chunks_openstax_physics.jsonl.pre_repair.bak — pre-repair backup
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

CHUNKS = Path("data/processed/chunks_openstax_physics.jsonl")
BACKUP = Path("data/processed/chunks_openstax_physics.jsonl.pre_repair.bak")

DROP_CHAPTER_TITLES: set[str] = {"PREFACE", "ANSWER KEY", "INDEX"}
DROP_CHAPTER_PREFIXES: tuple[str, ...] = ("APPENDIX",)


def is_drop_chapter(chapter_title: str) -> bool:
    if chapter_title in DROP_CHAPTER_TITLES:
        return True
    return any(chapter_title.startswith(p) for p in DROP_CHAPTER_PREFIXES)


def main() -> int:
    if not CHUNKS.exists():
        print(f"ERR: {CHUNKS} not found", file=sys.stderr)
        return 2

    # Backup
    shutil.copyfile(CHUNKS, BACKUP)
    print(f"backup: {BACKUP}")

    chunks = [json.loads(line) for line in CHUNKS.open()]
    n_in = len(chunks)
    print(f"input chunks: {n_in}")

    # --- Pass 1: drop back-matter ---
    keep: list[dict] = []
    dropped_by_chapter: Counter = Counter()
    for c in chunks:
        ch = c.get("chapter_title", "") or ""
        if is_drop_chapter(ch):
            dropped_by_chapter[ch] += 1
            continue
        keep.append(c)
    n_dropped = n_in - len(keep)
    print(f"\n=== Pass 1: drop back-matter ===")
    print(f"  dropped: {n_dropped} chunks")
    for ch, n in dropped_by_chapter.most_common():
        print(f"    {ch:25s}  {n}")

    # --- Pass 2: fill ALL remaining orphans ---
    # Set subsection_title = section_title for every chunk with empty
    # subsection_title. Covers both flat sections (no L2 in source) and
    # mixed sections (orphans are main-section narrative, not children
    # of existing example L2 subsections — see deep-dive on Newton's
    # First Law in repair audit notes).
    by_section: dict[tuple, list[dict]] = defaultdict(list)
    for c in keep:
        by_section[(c.get("chapter_num"), c.get("section_num", ""))].append(c)

    flat_count = sum(
        1 for lst in by_section.values()
        if all(not c.get("subsection_title") for c in lst)
    )
    mixed_count = sum(
        1 for lst in by_section.values()
        if any(c.get("subsection_title") for c in lst)
        and any(not c.get("subsection_title") for c in lst)
    )

    n_filled = 0
    n_skipped_no_section = 0
    for c in keep:
        if c.get("subsection_title"):
            continue
        sec_title = c.get("section_title", "") or ""
        ch_title = c.get("chapter_title", "") or ""
        # If section_title is also empty, fall back to chapter_title — last
        # resort to keep the invariant satisfied. Shouldn't happen after
        # pass 1 strips back-matter; guard anyway.
        fill = sec_title or ch_title
        if not fill:
            n_skipped_no_section += 1
            continue
        c["subsection_title"] = fill
        n_filled += 1

    print(f"\n=== Pass 2: fill all remaining orphans (flat + mixed) ===")
    print(f"  flat sections (no L2 in source):       {flat_count}")
    print(f"  mixed sections (orphans = main prose): {mixed_count}")
    print(f"  filled: {n_filled} chunks")
    if n_skipped_no_section:
        print(f"  WARNING: {n_skipped_no_section} chunks had neither section nor chapter title — left as orphan")

    # --- Recompute hierarchy stats ---
    n_complete = sum(
        1 for c in keep
        if c.get("chapter_title") and c.get("section_title") and c.get("subsection_title")
    )
    n_remaining_orphans = len(keep) - n_complete
    print(f"\n=== Post-repair stats ===")
    print(f"  total chunks:           {len(keep)}")
    print(f"  full 3-level hierarchy: {n_complete}/{len(keep)} ({100*n_complete/len(keep):.1f}%)")
    print(f"  remaining orphans:      {n_remaining_orphans}  ← these need LLM (mixed-section)")

    # --- Write output ---
    with CHUNKS.open("w") as f:
        for c in keep:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"\nwrote: {CHUNKS}  ({len(keep)} chunks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
