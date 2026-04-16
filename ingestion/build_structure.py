"""
ingestion/build_structure.py
-----------------------------
Builds data/textbook_structure.json from the extracted raw elements.

The structure captures the full topic hierarchy:
  Chapter → Section → Subsection

Each node includes:
  - title     : str
  - difficulty: "easy" | "moderate" | "hard"  (set by GPT-4o, one call per node)

The difficulty is asked once and stored permanently — no need to re-run.

Output format example:
{
  "Chapter 11: Muscle Tissue": {
    "difficulty": "moderate",
    "sections": {
      "11.1 Interactions of Skeletal Muscles": {
        "difficulty": "easy",
        "subsections": {
          "Origin, Insertion, and Action": { "difficulty": "easy" },
          "Lever Systems": { "difficulty": "moderate" }
        }
      }
    }
  }
}

Usage:
    python -m ingestion.build_structure
"""

# TODO: build hierarchy dict from raw elements (chapter_title, section_title, subsection_title)
# TODO: for each unique leaf node, call GPT-4o with the 'difficulty_rating' prompt from config.yaml
# TODO: save final structure to cfg.paths.textbook_structure


def build_structure(elements: list[dict]) -> dict:
    """
    Build the chapter/section/subsection hierarchy from raw extracted elements.

    Args:
        elements: Output of extract.extract_pdf()

    Returns:
        Nested dict representing the textbook structure (without difficulty yet).
    """
    raise NotImplementedError


def assign_difficulty(structure: dict) -> dict:
    """
    Walk every leaf node in the structure and ask GPT-4o to rate its difficulty.
    Uses the 'difficulty_rating' prompt from config.yaml.

    Args:
        structure: Output of build_structure()

    Returns:
        Same structure with 'difficulty' fields filled in.
    """
    raise NotImplementedError


def save_structure(structure: dict, output_path: str) -> None:
    """Save the final structure dict to JSON."""
    raise NotImplementedError


if __name__ == "__main__":
    import json
    from config import cfg
    from ingestion.extract import extract_pdf

    elements = extract_pdf(cfg.paths.raw_ot_pdf)
    structure = build_structure(elements)
    structure = assign_difficulty(structure)
    save_structure(structure, cfg.paths.textbook_structure)
    print(f"Saved textbook structure to {cfg.paths.textbook_structure}")
