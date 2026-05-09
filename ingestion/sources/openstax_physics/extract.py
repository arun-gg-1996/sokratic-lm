"""
ingestion/sources/openstax_physics/extract.py
Physics counterpart to openstax_anatomy/extract.py.

Differences from anatomy:
  - domain literal stamped onto every seed chunk is "physics" (not "ot")
  - default extract_pdf domain label is "physics" (used by parse.py to name
    its per-chapter JSON output dir)
  - main-script PDF path comes from cfg.paths under the openstax_physics key

Everything else (sanitization, schema mapping, save) is identical to anatomy
and would be duplicated if we copied the whole file. To keep both modules
in lockstep we import the helpers and only override the domain-stamping
function. If physics-specific schema fields ever appear, swap the imports
for inline copies.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from dotenv import load_dotenv

from config import cfg
from ingestion.sources.openstax_physics.parse import parse_pdf
from ingestion.sources.openstax_anatomy.extract import (
    _sanitize_section_title,
    save_jsonl,
)

load_dotenv()

RAW_SECTIONS_PATH = cfg.domain_path("raw_sections")
RAW_SECTIONS_DIR = "data/processed/chunks/raw_sections"

# Physics domain stamp. Distinct from anatomy's "ot" so chunk filtering by
# domain (qdrant payload, JSONL grep, etc.) cleanly separates the two
# corpora at every downstream consumer.
DOMAIN_STAMP = "physics"


def _map_section_to_seed_chunk(section: dict) -> dict:
    """Map parse_pdf section schema to our pre-chunk schema (physics domain).

    Same shape as anatomy._map_section_to_seed_chunk; only the domain literal
    differs. We don't import anatomy's mapper directly because it hard-codes
    "ot" — the rebinding here is the cleanest way to flip the stamp without
    threading config through the source-loader call signature.
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

    return {
        "chunk_id": str(uuid.uuid4()),
        "text": section["text"],
        "chapter_num": int(section["chapter_num"]),
        "chapter_title": chapter_title,
        "section_num": section_num,
        "section_title": section_title,
        "subsection_title": subsection_title,
        "page": int(section["page_start"]),
        "element_type": "paragraph",
        "domain": DOMAIN_STAMP,

        # Provenance
        "source_section_id": section["id"],
        "source_level": level,
        "parent_section": section.get("parent_section", ""),
        "page_end": int(section["page_end"]),
        "source_pdf": section["source_pdf"],
    }


def sections_to_seed_chunks(sections: list[dict]) -> list[dict]:
    """Convert parse_pdf sections to pre-chunk seed records (physics)."""
    seeds: list[dict] = []
    for s in sections:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        seeds.append(_map_section_to_seed_chunk(s))
    return seeds


def extract_pdf(
    pdf_path: str,
    domain: str = "physics",
    out_dir: str = RAW_SECTIONS_DIR,
    out_jsonl: str = RAW_SECTIONS_PATH,
) -> list[dict]:
    """Run parse_pdf and return mapped section-seed chunks for physics."""
    sections = parse_pdf(
        pdf_path=pdf_path,
        domain=domain,
        out_dir=out_dir,
        save=True,
    )
    seeds = sections_to_seed_chunks(sections)
    save_jsonl(seeds, out_jsonl)
    return seeds


if __name__ == "__main__":
    pdf_path = cfg.paths.raw_physics_pdf if hasattr(cfg.paths, "raw_physics_pdf") else "data/raw/openStax_physics_v1.pdf"
    print(f"Running parse_pdf extraction on: {pdf_path}")
    seeds = extract_pdf(
        pdf_path=pdf_path,
        domain=DOMAIN_STAMP,
        out_dir=RAW_SECTIONS_DIR,
        out_jsonl=RAW_SECTIONS_PATH,
    )
    print(f"Mapped seed chunks saved: {len(seeds)} -> {RAW_SECTIONS_PATH}")
