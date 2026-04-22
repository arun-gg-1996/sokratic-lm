"""
retrieval/topic_suggester.py
-----------------------------
TopicSuggester loads textbook_structure.json and returns a list of topic paths
suitable for presenting to a new student who has no memory history.

Topics are returned as human-readable strings of the form:
  "Chapter 11: The Muscular System > Muscles of the Shoulder > Deltoid"

Usage:
    suggester = TopicSuggester()
    topics = suggester.suggest(n=6)              # random sample
    topics = suggester.suggest(n=6, difficulty="moderate")
    all_leaves = suggester.all_leaf_topics()
"""

import json
import random
from pathlib import Path

from config import cfg


class TopicSuggester:
    def __init__(self):
        structure_path = Path(getattr(cfg.paths, "textbook_structure", "data/textbook_structure.json"))
        if not structure_path.is_absolute():
            structure_path = Path(__file__).parent.parent / structure_path
        try:
            with open(structure_path, "r") as f:
                self._structure = json.load(f)
        except FileNotFoundError:
            self._structure = {}
        self._leaves: list[dict] = self._build_leaves()

    def _build_leaves(self) -> list[dict]:
        """
        Walk the nested structure and collect all leaf nodes with their path and difficulty.
        A leaf is the deepest node available (subsection if present, else section, else chapter).
        """
        leaves: list[dict] = []
        for chapter_name, chapter in (self._structure or {}).items():
            if not isinstance(chapter, dict):
                continue
            sections = chapter.get("sections", {})
            if not isinstance(sections, dict) or not sections:
                # Chapter is the leaf
                leaves.append({
                    "path": chapter_name,
                    "difficulty": str(chapter.get("difficulty", "moderate")),
                })
                continue
            for section_name, section in sections.items():
                if not isinstance(section, dict):
                    continue
                subsections = section.get("subsections", {})
                if not isinstance(subsections, dict) or not subsections:
                    # Section is the leaf
                    leaves.append({
                        "path": f"{chapter_name} > {section_name}",
                        "difficulty": str(section.get("difficulty", "moderate")),
                    })
                    continue
                for sub_name, sub in subsections.items():
                    if not isinstance(sub, dict):
                        continue
                    leaves.append({
                        "path": f"{chapter_name} > {section_name} > {sub_name}",
                        "difficulty": str(sub.get("difficulty", "moderate")),
                    })
        return leaves

    def all_leaf_topics(self) -> list[dict]:
        """Return all leaf topics as list of {path, difficulty} dicts."""
        return list(self._leaves)

    def suggest(
        self,
        n: int = 6,
        difficulty: str | None = None,
        seed: int | None = None,
    ) -> list[str]:
        """
        Return up to n topic path strings, optionally filtered by difficulty.

        Args:
            n:          Number of topics to return.
            difficulty: "easy" | "moderate" | "hard" | None (all).
            seed:       Optional random seed for reproducibility.

        Returns:
            List of topic path strings (e.g. "Chapter 11 > Shoulder > Deltoid").
        """
        pool = self._leaves
        if difficulty:
            pool = [t for t in pool if t["difficulty"] == difficulty]
        if not pool:
            pool = self._leaves  # fall back to all if filter yields nothing

        rng = random.Random(seed)
        sample = rng.sample(pool, min(n, len(pool)))
        return [t["path"] for t in sample]
