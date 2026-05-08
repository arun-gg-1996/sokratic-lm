"""
retrieval/topic_suggester.py
TopicSuggester returns a list of topic paths for new-student onboarding.

Source of truth: SQLite chapters/sections/subsections (post-migration 003).
Reads once at construction and caches in-memory; the curriculum is static
for the lifetime of the process.

Topics are returned as human-readable strings of the form:
    "Chapter 11: The Muscular System > Muscles of the Shoulder > Deltoid"

Usage:
    suggester = TopicSuggester()
    topics = suggester.suggest(n=6)
    topics = suggester.suggest(n=6, difficulty="moderate")
    all_leaves = suggester.all_leaf_topics()
"""

import random


class TopicSuggester:
    def __init__(self):
        self._leaves: list[dict] = self._build_leaves_from_sql()

    def _build_leaves_from_sql(self) -> list[dict]:
        """Pull every subsection from SQL with its full path string. Each row
  becomes one leaf candidate for suggestion.

  The `difficulty` field used to live in textbook_structure.json's
  per-subsection metadata, but it isn't carried in our chapters/
  sections/subsections schema. Default to "moderate" — the difficulty
  filter is rarely exercised in production and was only used by older
  rapport-phase A/B tests.
  """
        from memory.sqlite_store import SQLiteStore
        try:
            cur = SQLiteStore()._conn().execute(
                """
                SELECT
                    c.chapter_num, c.title AS chapter,
                    s.title AS section,
                    sub.title AS subsection
                FROM subsections sub
                JOIN sections s ON s.section_id = sub.section_id
                JOIN chapters c ON c.chapter_id = s.chapter_id
                ORDER BY c.chapter_num, s.section_order, sub.subsection_order
                """
            )
            rows = cur.fetchall()
        except Exception:
            return []

        leaves: list[dict] = []
        for r in rows:
            path = (
                f"Chapter {r['chapter_num']}: {r['chapter']} > "
                f"{r['section']} > {r['subsection']}"
            )
            leaves.append({"path": path, "difficulty": "moderate"})
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
 n: Number of topics to return.
 difficulty: "easy" | "moderate" | "hard" | None (all).
 seed: Optional random seed for reproducibility.

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

    def suggest_for_student(
        self,
        mastery_store,
        student_id: str,
        n: int = 6,
        weak_threshold: float = 0.5,
    ) -> list[str]:
        """Mastery-aware topic suggestions for a returning student.

 Returns up to `n` topic strings, half of them "revisit" picks
 from the student's weakest subsections (mastery < threshold)
 and the rest "explore" picks from the textbook structure.

 For a fresh student (no mastery data yet), falls back to
 suggest — same behavior as before .

 Args:
 mastery_store: a memory.mastery_store.MasteryStore instance
 student_id: validated student id
 n: total cards to return
 weak_threshold: subsections under this mastery are "weak"

 Returns:
 list[str] — same shape as suggest so callers don't need
 to branch on returning vs fresh.
"""
        try:
            n_weak_target = max(0, n // 2)
            weak = mastery_store.weak_subsections(
                student_id, threshold=weak_threshold, limit=n_weak_target
            )
        except Exception:
            weak = []

        # Format weak entries as path strings the dean's topic matcher
        # can resolve back to the same TOC node. Subsection title alone
        # is usually enough since the chunks retriever is good at
        # matching short topic names to the right section. We append
        # "(Ch{N})" so the user can tell the chapter at a glance from
        # the card label without clicking through.
        weak_strs: list[str] = []
        seen_paths: set[str] = set()
        for w in weak:
            sub = w.get("subsection_title") or w.get("section_title") or ""
            ch = w.get("chapter_num") or 0
            if not sub:
                continue
            label = f"{sub} (Ch{ch})" if ch else sub
            weak_strs.append(label)
            seen_paths.add(w.get("path") or "")

        n_explore = max(0, n - len(weak_strs))
        explore = self.suggest(n=n_explore)
        return weak_strs + explore
