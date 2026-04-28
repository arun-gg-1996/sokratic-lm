"""
ingestion/build_structure.py
-----------------------------
Build data/textbook_structure.json from extracted raw elements.

Output hierarchy:
  Chapter -> Section -> Subsection
with a difficulty label on each node.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path


def _clean_title(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _difficulty_for_title(title: str, level: str) -> str:
    """
    Deterministic difficulty heuristic.
    We keep this local/fast so structure generation remains lightweight.
    """
    t = title.lower()
    hard_terms = (
        "peripheral nervous system",
        "central nervous system",
        "endocrine",
        "renal",
        "acid-base",
        "electrolyte",
        "metabolism",
        "immune",
        "reproductive",
    )
    easy_terms = (
        "overview",
        "introduction",
        "organization",
        "anatomy and physiology",
        "homeostasis",
    )
    if any(k in t for k in hard_terms):
        return "hard"
    if any(k in t for k in easy_terms):
        return "easy"
    # Slightly bump subsection complexity by default.
    if level == "subsection":
        return "moderate"
    return "moderate"


def build_structure(elements: list[dict]) -> dict:
    """
    Build chapter/section/subsection hierarchy from raw extracted elements.
    """
    chapter_sections: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )

    for el in elements:
        chapter_title = _clean_title(el.get("chapter_title", ""))
        if not chapter_title or chapter_title.lower() in {"unknown", "index"}:
            continue

        section_title = _clean_title(el.get("section_title", ""))
        subsection_title = _clean_title(el.get("subsection_title", ""))

        # Ensure each chapter appears even if sections are sparse.
        _ = chapter_sections[chapter_title]
        if section_title:
            _ = chapter_sections[chapter_title][section_title]
            if subsection_title:
                chapter_sections[chapter_title][section_title].add(subsection_title)

    structure: dict[str, dict] = {}
    for chapter_title in sorted(chapter_sections.keys()):
        sections_dict: dict[str, dict] = {}
        for section_title in sorted(chapter_sections[chapter_title].keys()):
            subsections = chapter_sections[chapter_title][section_title]
            subsection_dict = {
                sub: {"difficulty": _difficulty_for_title(sub, "subsection")}
                for sub in sorted(subsections)
            }
            sections_dict[section_title] = {
                "difficulty": _difficulty_for_title(section_title, "section"),
                "subsections": subsection_dict,
            }

        structure[chapter_title] = {
            "difficulty": _difficulty_for_title(chapter_title, "chapter"),
            "sections": sections_dict,
        }

    return structure


def assign_difficulty(structure: dict) -> dict:
    """
    Kept for API compatibility with architecture docs.
    build_structure() already assigns difficulty labels.
    """
    return structure


def save_structure(structure: dict, output_path: str) -> None:
    """Save final structure dict to JSON."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)


def _load_cached_elements(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


if __name__ == "__main__":
    from config import cfg
    # Source-specific extraction; B.7 (pipeline orchestrator) will route this through
    # source modules so build_structure.py stays generic.
    from ingestion.sources.openstax_anatomy.extract import extract_pdf

    cached_raw = Path("data/processed/raw_elements_ot.jsonl")
    elements = _load_cached_elements(cached_raw)
    if elements:
        print(f"Loaded cached raw elements: {len(elements)} from {cached_raw}")
    else:
        print("Cached raw elements not found; running extract_pdf...")
        elements = extract_pdf(cfg.paths.raw_ot_pdf)
        print(f"Extracted elements: {len(elements)}")

    structure = build_structure(elements)
    structure = assign_difficulty(structure)
    save_structure(structure, cfg.paths.textbook_structure)
    print(f"Saved textbook structure to {cfg.paths.textbook_structure}")
    print(f"Chapters in structure: {len(structure)}")
