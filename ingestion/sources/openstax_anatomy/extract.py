"""
ingestion/extract.py
--------------------
Extraction foundation switched to ingestion.parse_pdf.parse_pdf
(font-size heading parser for OpenStax PDFs).

This module now:
1) runs parse_pdf() to get structured sections (L1 + L2)
2) maps sections into our ChunkSchema-compatible pre-chunk records
3) saves mapped records to data/processed/raw_sections_ot.jsonl

No core parsing logic is implemented here; parse_pdf.py is the source of truth.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from dotenv import load_dotenv

from config import cfg
from ingestion.sources.openstax_anatomy.parse import parse_pdf

load_dotenv()

RAW_SECTIONS_PATH = cfg.domain_path("raw_sections")
RAW_SECTIONS_DIR = "data/processed/chunks/raw_sections"


def _sanitize_section_title(section_title: str, chapter_title: str, section_num: str) -> str:
    """
    Normalize section titles for indexing.
    Never allow 1-2 character section titles (e.g., 'A', 'B').
    """
    title = (section_title or "").strip()
    if len(title) <= 2:
        sec = (section_num or "").strip()
        if sec:
            return f"{chapter_title} {sec}".strip()
        return chapter_title
    return title


def _map_section_to_seed_chunk(section: dict) -> dict:
    """Map parse_pdf section schema to our pre-chunk schema.

    parse.py emits clean fields after the 2026-04-28 fix:
      - section_title is the canonical L1 name (no section_num prefix,
        no em-dash mash). L2 sections inherit the parent L1 title here.
      - subsection_title is the L2 heading text (or "" for L1).
      - section_num is the dotted number ("6.7") in its own field.
    No de-mashing is needed at the extract layer anymore.
    """
    level = int(section.get("level", 1))
    chapter_title = section["chapter"]
    section_num = section.get("section_num", "") or ""
    section_title = _sanitize_section_title(
        section.get("section_title", ""),
        chapter_title=chapter_title,
        section_num=section_num,
    )
    subsection_title = (section.get("subsection_title") or "").strip()

    mapped = {
        # ChunkSchema-compatible fields
        "chunk_id": str(uuid.uuid4()),
        "text": section["text"],
        "chapter_num": int(section["chapter_num"]),
        "chapter_title": chapter_title,
        "section_num": section_num,
        "section_title": section_title,
        "subsection_title": subsection_title,
        "page": int(section["page_start"]),
        "element_type": "paragraph",
        "domain": "ot",

        # Additional provenance fields (useful for downstream checks)
        "source_section_id": section["id"],
        "source_level": level,
        "parent_section": section.get("parent_section", ""),
        "page_end": int(section["page_end"]),
        "source_pdf": section["source_pdf"],
    }
    return mapped


def sections_to_seed_chunks(sections: list[dict]) -> list[dict]:
    """Convert parse_pdf sections to pre-chunk seed records."""
    seeds: list[dict] = []
    for s in sections:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        seeds.append(_map_section_to_seed_chunk(s))
    return seeds


def save_jsonl(rows: list[dict], out_path: str) -> None:
    """Save list of dicts as JSONL."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def extract_pdf(
    pdf_path: str,
    domain: str = "OT_anatomy",
    out_dir: str = RAW_SECTIONS_DIR,
    out_jsonl: str = RAW_SECTIONS_PATH,
) -> list[dict]:
    """
    Run parse_pdf and return mapped section-seed chunks.

    Args:
        pdf_path: Source PDF path.
        domain: parse_pdf domain label.
        out_dir: parse_pdf output dir for per-chapter JSON.
        out_jsonl: JSONL output path for mapped seeds.

    Returns:
        List of mapped seed chunk dicts.
    """
    sections = parse_pdf(
        pdf_path=pdf_path,
        domain=domain,
        out_dir=out_dir,
        save=True,
    )

    seeds = sections_to_seed_chunks(sections)
    save_jsonl(seeds, out_jsonl)
    return seeds


def _report_section_stats(sections: list[dict]) -> None:
    total_sections = len(sections)
    chapters = sorted({int(s["chapter_num"]) for s in sections if int(s["chapter_num"]) > 0})
    total_words = sum(len((s.get("text") or "").split()) for s in sections)

    axillary_nerve_sections = [s for s in sections if "axillary nerve" in (s.get("text") or "").lower()]
    deltoid_sections = [s for s in sections if "deltoid" in (s.get("text") or "").lower()]

    print("\n" + "=" * 70)
    print("SECTION EXTRACTION REPORT")
    print("=" * 70)
    print(f"Total sections found   : {total_sections}")
    print(f"Chapters covered       : {len(chapters)} ({chapters[:6]} ... {chapters[-3:]})")
    print(f"Total words (sections) : {total_words:,}")
    print(f"Sections with 'axillary nerve' : {len(axillary_nerve_sections)}")
    print(f"Sections with 'deltoid'        : {len(deltoid_sections)}")

    if axillary_nerve_sections:
        print("\nSections containing 'axillary nerve' (up to 5):")
        for s in axillary_nerve_sections[:5]:
            print(
                f"- Ch{s['chapter_num']} {s.get('section_num', '')} "
                f"[{s.get('page_start')}-{s.get('page_end')}] {s.get('section_title','')[:90]}"
            )


def _load_sections_from_saved_dir(out_dir: str, domain: str) -> list[dict]:
    """Load combined parse_pdf output if available."""
    combined = Path(out_dir) / f"all_sections_{domain}.json"
    if not combined.exists():
        return []
    with combined.open("r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    pdf_path = cfg.paths.raw_ot_pdf
    domain = "OT_anatomy"

    print(f"Running parse_pdf extraction on: {pdf_path}")
    seeds = extract_pdf(
        pdf_path=pdf_path,
        domain=domain,
        out_dir=RAW_SECTIONS_DIR,
        out_jsonl=RAW_SECTIONS_PATH,
    )

    sections = _load_sections_from_saved_dir(RAW_SECTIONS_DIR, domain)
    if sections:
        _report_section_stats(sections)

    print(f"\nMapped seed chunks saved: {len(seeds)} -> {RAW_SECTIONS_PATH}")
