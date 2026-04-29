"""
scripts/build_topic_index.py
-----------------------------
Build a TOC-grounded topic index from textbook_structure.json + indexed chunks.

Pipeline:
1. Walk `data/textbook_structure.json` (nested: chapter > section > subsection).
2. Drop nodes whose names match domain-specific junk patterns
   (see `topic_index.junk_patterns` in config/domains/{domain}.yaml).
3. Count chunks per (chapter, section, subsection) by normalizing the chunk
   fields against the structure keys — chunks carry a different naming
   convention than the structure file, so we strip section-number prefixes
   and split on the " — " separator that the ingestion pipeline uses to
   join section + subsection.
4. Keep nodes with chunk_count >= `topic_index.min_chunk_count` (default 1).
5. Tag `limited: true` on nodes with chunk_count <= `limited_chunk_threshold`.
6. Write `data/topic_index.json` as a flat list of leaf entries:
   {chapter, section, subsection, difficulty, chunk_count, limited, path}

Run:
    .venv/bin/python -m scripts.build_topic_index
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import cfg  # noqa: E402

STRUCTURE_PATH = ROOT / getattr(cfg.paths, "textbook_structure", "data/textbook_structure.json")
CHUNKS_PATH = ROOT / getattr(cfg.paths, "chunks_ot", "data/processed/chunks_ot.jsonl")
OUT_PATH = ROOT / "data" / "topic_index.json"

CHAPTER_PREFIX_RE = re.compile(r"^Chapter\s+\d+\s*:\s*", re.IGNORECASE)
SECTION_NUM_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*\s+")


def _strip_chapter_prefix(name: str) -> str:
    return CHAPTER_PREFIX_RE.sub("", name).strip()


def _strip_section_num(name: str) -> str:
    return SECTION_NUM_PREFIX_RE.sub("", name).strip()


def _is_junk(name: str, patterns: list[str], suffixes: list[str]) -> bool:
    lname = name.lower()
    for p in patterns:
        if p.lower() in lname:
            return True
    for s in suffixes:
        if name.endswith(s):
            return True
    return False


def _load_junk_config() -> tuple[list[str], list[str], int, int]:
    ti = cfg.topic_index
    patterns = list(getattr(ti, "junk_patterns", []) or [])
    suffixes = list(getattr(ti, "junk_suffixes", []) or [])
    min_count = int(getattr(ti, "min_chunk_count", 1))
    limited = int(getattr(ti, "limited_chunk_threshold", 2))
    return patterns, suffixes, min_count, limited


def _count_chunks() -> dict[tuple[str, str, str], int]:
    """
    Return counts keyed by (chapter_norm, section_norm, subsection_norm).
    subsection_norm is "" when the chunk has no subsection.

    Normalization rules (based on actual chunk schema):
      - chapter: chunk.chapter_title verbatim (no prefix in chunks).
      - section/subsection: chunk.section_title has the form
          "<section_num> <section_name>[ — <subsection_name>]"
        so we split on " — " and strip section_num from the left half.
    """
    counts: dict[tuple[str, str, str], int] = {}
    with open(CHUNKS_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            chapter = (c.get("chapter_title") or "").strip()
            section_raw = (c.get("section_title") or "").strip()
            if not chapter or not section_raw:
                continue
            if " — " in section_raw:
                section_part, sub_part = section_raw.split(" — ", 1)
            else:
                section_part, sub_part = section_raw, ""
            section_norm = _strip_section_num(section_part)
            sub_norm = sub_part.strip()
            key = (chapter, section_norm, sub_norm)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _aggregate_counts(
    chunk_counts: dict[tuple[str, str, str], int],
    chapter: str,
    section: str = "",
    subsection: str = "",
) -> int:
    """Sum chunks matching the given TOC level (empty = wildcard)."""
    total = 0
    for (ch, sec, sub), n in chunk_counts.items():
        if ch != chapter:
            continue
        if section and sec != section:
            continue
        if subsection and sub != subsection:
            continue
        total += n
    return total


def build_index() -> list[dict]:
    structure = json.loads(STRUCTURE_PATH.read_text())
    patterns, suffixes, min_count, limited_thresh = _load_junk_config()
    chunk_counts = _count_chunks()

    entries: list[dict] = []
    dropped_junk = 0
    dropped_empty = 0

    for chapter_key, chapter_node in structure.items():
        if not isinstance(chapter_node, dict):
            continue
        chapter_name = _strip_chapter_prefix(chapter_key)
        if _is_junk(chapter_name, patterns, suffixes):
            dropped_junk += 1
            continue

        sections = chapter_node.get("sections", {}) or {}
        chapter_difficulty = str(chapter_node.get("difficulty", "moderate"))

        if not sections:
            count = _aggregate_counts(chunk_counts, chapter_name)
            if count < min_count:
                dropped_empty += 1
                continue
            entries.append({
                "chapter": chapter_name,
                "section": "",
                "subsection": "",
                "difficulty": chapter_difficulty,
                "chunk_count": count,
                "limited": count <= limited_thresh,
                "path": chapter_key,
            })
            continue

        for section_name, section_node in sections.items():
            if not isinstance(section_node, dict):
                continue
            if _is_junk(section_name, patterns, suffixes):
                dropped_junk += 1
                continue
            section_difficulty = str(section_node.get("difficulty", chapter_difficulty))
            subsections = section_node.get("subsections", {}) or {}

            if not subsections:
                count = _aggregate_counts(chunk_counts, chapter_name, section_name)
                if count < min_count:
                    dropped_empty += 1
                    continue
                entries.append({
                    "chapter": chapter_name,
                    "section": section_name,
                    "subsection": "",
                    "difficulty": section_difficulty,
                    "chunk_count": count,
                    "limited": count <= limited_thresh,
                    "path": f"{chapter_key} > {section_name}",
                })
                continue

            for sub_name, sub_node in subsections.items():
                if not isinstance(sub_node, dict):
                    continue
                if _is_junk(sub_name, patterns, suffixes):
                    dropped_junk += 1
                    continue
                sub_difficulty = str(sub_node.get("difficulty", section_difficulty))
                count = _aggregate_counts(chunk_counts, chapter_name, section_name, sub_name)
                if count < min_count:
                    dropped_empty += 1
                    continue
                entries.append({
                    "chapter": chapter_name,
                    "section": section_name,
                    "subsection": sub_name,
                    "difficulty": sub_difficulty,
                    "chunk_count": count,
                    "limited": count <= limited_thresh,
                    "path": f"{chapter_key} > {section_name} > {sub_name}",
                })

    entries.sort(key=lambda e: (e["chapter"], e["section"], e["subsection"]))
    return entries, dropped_junk, dropped_empty


def main() -> None:
    entries, dropped_junk, dropped_empty = build_index()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(entries, indent=2))

    limited_n = sum(1 for e in entries if e["limited"])
    by_difficulty: dict[str, int] = {}
    for e in entries:
        by_difficulty[e["difficulty"]] = by_difficulty.get(e["difficulty"], 0) + 1

    print(f"Wrote {len(entries)} topics → {OUT_PATH.relative_to(ROOT)}")
    print(f"  dropped (junk patterns): {dropped_junk}")
    print(f"  dropped (no chunks):     {dropped_empty}")
    print(f"  limited coverage (<= {cfg.topic_index.limited_chunk_threshold} chunks): {limited_n}")
    print(f"  by difficulty: {by_difficulty}")


if __name__ == "__main__":
    main()
