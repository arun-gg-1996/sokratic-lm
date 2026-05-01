"""
retrieval/topic_matcher.py
---------------------------
TOC-grounded topic matcher.

Loads `data/topic_index.json` (built by `scripts/build_topic_index.py`) and
maps a student's free-form topic request to one or more TOC leaf nodes. This
replaces the previous LLM-brainstormed topic-options flow: every acceptable
topic must correspond to a real, chunk-backed node in the textbook structure.

Matcher tiers:
  strong      — single unambiguous match; caller can lock directly
  borderline  — 2-5 plausible candidates; caller should show "Did you mean…?" cards
  none        — nothing plausible; caller should refuse with alternatives

Scoring uses RapidFuzz token_set_ratio across each entry's subsection,
section, chapter, and full path, taking the max. The subsection label gets a
small preference because it's usually the most discriminative surface form.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rapidfuzz import fuzz

from config import cfg
from retrieval.ontology import DomainOntologyAdapter, NoopAdapter


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX_PATH = ROOT / "data" / "topic_index.json"


@dataclass
class TopicMatch:
    path: str
    chapter: str
    section: str
    subsection: str
    difficulty: str
    chunk_count: int
    limited: bool
    score: float = 0.0
    # Whether retrieval on this node actually returns strong in-section content.
    # Stamped by `scripts/validate_topic_index.py`. Defaults to True so entries
    # without a validation pass still appear (fail-open).
    teachable: bool = True

    @property
    def label(self) -> str:
        """Short, card-friendly label — prefers the most specific level."""
        return self.subsection or self.section or self.chapter


@dataclass
class MatchResult:
    query: str
    tier: Literal["strong", "borderline", "none"]
    matches: list[TopicMatch] = field(default_factory=list)

    @property
    def top(self) -> TopicMatch | None:
        return self.matches[0] if self.matches else None


# Score thresholds. Tuned for OT corpus — student free-text is usually short
# (1-4 words), while TOC labels are 1-10 words; token_set_ratio is the right
# primitive because it's invariant to extra/missing tokens.
#
# 2026-04-29: lowered STRONG_MIN 90 → 65 after end-to-end testing showed
# the previous threshold was rejecting valid in-corpus topics.
#
# 2026-04-30 (post-18-convo qualitative review): bumped STRONG_MIN 65 → 78
# and STRONG_GAP 5 → 10. The 18-convo batch surfaced ~5 cases where a
# fuzzy match in the 65-78 range was conceptually WRONG (long bone →
# muscle types; cardiac cycle → muscle twitch). The fuzzy matcher's
# token_set_ratio on student questions against TOC titles can score in
# this range when partial keyword overlap exists, but it doesn't capture
# concept-level match. Stricter threshold pushes ambiguous cases to the
# "borderline" tier where the dean presents cards — letting the student
# disambiguate is much better than auto-locking on a mediocre match.
STRONG_MIN = 78
STRONG_GAP = 10           # top must lead second by this much to auto-lock
BORDERLINE_MIN = 50       # below this, treat as no-match and refuse with alternatives
MAX_CANDIDATES = 5


class TopicMatcher:
    def __init__(
        self,
        index_path: Path | None = None,
        ontology: DomainOntologyAdapter | None = None,
    ):
        path = Path(index_path) if index_path else DEFAULT_INDEX_PATH
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = []
        self._entries: list[TopicMatch] = [
            TopicMatch(
                path=e["path"],
                chapter=e.get("chapter", ""),
                section=e.get("section", ""),
                subsection=e.get("subsection", ""),
                difficulty=e.get("difficulty", "moderate"),
                chunk_count=int(e.get("chunk_count", 0)),
                limited=bool(e.get("limited", False)),
                teachable=bool(e.get("teachable", True)),
            )
            for e in raw
        ]
        self._ontology: DomainOntologyAdapter = ontology or NoopAdapter()

    def __len__(self) -> int:
        return len(self._entries)

    # UMLS canonicals often wrap the useful term in generic scaffolding:
    #   "Structure of deltoid muscle"  (we want "deltoid muscle")
    #   "Left deltoid"                  (we want "deltoid")
    #   "Entire brachial plexus"        (we want "brachial plexus")
    # These wrappers inflate token_set_ratio against any TOC section that
    # mentions "Structure", "Muscle", or body parts of the same laterality.
    # We strip them before feeding the canonical into the fuzz query.
    _CANONICAL_STRIP_PREFIXES: tuple[str, ...] = (
        "structure of the ",
        "structure of ",
        "entire ",
        "left ",
        "right ",
    )
    _CANONICAL_STRIP_SUFFIXES: tuple[str, ...] = (
        ", nos",
        " (body structure)",
    )

    @classmethod
    def _clean_canonical(cls, term: str) -> str:
        t = (term or "").strip()
        low = t.lower()
        for p in cls._CANONICAL_STRIP_PREFIXES:
            if low.startswith(p):
                t = t[len(p):]
                low = t.lower()
                break
        for s in cls._CANONICAL_STRIP_SUFFIXES:
            if low.endswith(s):
                t = t[: -len(s)]
                low = t.lower()
                break
        return t.strip()

    def _expand_query(self, query: str) -> str:
        """
        Add canonical entity names from the ontology adapter to the raw query.

        Why: student free-text ("deltoid", "cn vii") rarely matches TOC section
        labels literally. UMLS entity linking bridges the gap by recognising
        "deltoid" and emitting "Deltoid muscle", which then token-matches
        "Axial Muscles of the … Deltoid" etc. RapidFuzz `token_set_ratio`
        handles the union cleanly — adding canonical names never hurts recall.
        Noop adapters return [] so this is a zero-cost no-op off-domain.

        UMLS wrappers ("Structure of …", "Left …") are stripped before the
        canonical joins the query — see `_clean_canonical` for rationale.
        """
        if not query:
            return query
        try:
            mentions = self._ontology.link_entities(query)
        except Exception:
            return query
        if not mentions:
            return query
        extras: list[str] = []
        seen = {query.lower().strip()}
        for m in mentions:
            for raw in (m.canonical, m.span):
                t = self._clean_canonical(raw)
                if t and t.lower() not in seen:
                    extras.append(t)
                    seen.add(t.lower())
        if not extras:
            return query
        return query + " " + " ".join(extras)

    def _score(self, query: str, e: TopicMatch) -> float:
        q = query.strip().lower()
        if not q:
            return 0.0
        sub_score = fuzz.token_set_ratio(q, e.subsection.lower()) if e.subsection else 0
        sec_score = fuzz.token_set_ratio(q, e.section.lower()) if e.section else 0
        ch_score = fuzz.token_set_ratio(q, e.chapter.lower()) if e.chapter else 0
        # Small bonus for subsection-level match since it's the most specific.
        return max(sub_score + 1 if sub_score else 0, sec_score, ch_score)

    def match(self, query: str, k: int = MAX_CANDIDATES) -> MatchResult:
        query = (query or "").strip()
        if not query or not self._entries:
            return MatchResult(query=query, tier="none", matches=[])

        scoring_query = self._expand_query(query)

        scored: list[TopicMatch] = []
        for e in self._entries:
            s = self._score(scoring_query, e)
            if s <= 0:
                continue
            scored.append(TopicMatch(
                path=e.path, chapter=e.chapter, section=e.section,
                subsection=e.subsection, difficulty=e.difficulty,
                chunk_count=e.chunk_count, limited=e.limited,
                teachable=e.teachable, score=s,
            ))
        scored.sort(key=lambda m: (-m.score, -m.chunk_count))
        top_k = scored[:k]

        if not top_k or top_k[0].score < BORDERLINE_MIN:
            return MatchResult(query=query, tier="none", matches=top_k)

        top = top_k[0]
        second_score = top_k[1].score if len(top_k) > 1 else 0
        is_strong = top.score >= STRONG_MIN and (top.score - second_score) >= STRONG_GAP
        tier: Literal["strong", "borderline"] = "strong" if is_strong else "borderline"
        return MatchResult(query=query, tier=tier, matches=top_k)

    def sample_diverse(
        self,
        n: int = 3,
        seed: int | None = None,
        min_chunk_count: int = 5,
        exclude_paths: set[str] | None = None,
    ) -> list[TopicMatch]:
        """
        Random-but-diverse sample across chapters, used when no match is found.

        Cards surfaced here are a promise we can teach the topic. We therefore
        enforce these filters, strongest first:
          - `teachable=False` entries are excluded outright — they failed the
            build-time retrieval validation in
            `scripts/validate_topic_index.py`, so the coverage gate will
            reject them at lock time. Showing them is a guaranteed card-loop.
          - `limited=True` entries are excluded (weak ingestion coverage).
          - `chunk_count >= min_chunk_count` — low-chunk topics have a high
            probability of failing the coverage gate at lock time.
          - `exclude_paths` is the set of TOC paths that already failed the
            coverage gate this session, so we never re-suggest them.

        Falls back through relaxed thresholds (→ min_chunk_count=3 → any) only
        if the strict pass produces nothing. `teachable=False` is never
        relaxed — better an empty card list than a guaranteed dead-end.
        """
        if not self._entries:
            return []
        exclude_paths = exclude_paths or set()
        rng = random.Random(seed)

        def _pick_with_floor(floor: int) -> list[TopicMatch]:
            by_chapter: dict[str, list[TopicMatch]] = {}
            for e in self._entries:
                if not e.teachable:
                    continue
                if e.limited:
                    continue
                if e.chunk_count < floor:
                    continue
                if e.path in exclude_paths:
                    continue
                by_chapter.setdefault(e.chapter, []).append(e)
            chapters = list(by_chapter.keys())
            rng.shuffle(chapters)
            out: list[TopicMatch] = []
            for ch in chapters:
                if len(out) >= n:
                    break
                out.append(rng.choice(by_chapter[ch]))
            return out[:n]

        for floor in (min_chunk_count, max(3, min_chunk_count - 2), 1):
            picks = _pick_with_floor(floor)
            if len(picks) >= n:
                return picks
        # Absolute fallback — relax limited/chunk_count filters but never
        # `teachable` (those are guaranteed dead-ends) and never re-suggest
        # already-rejected paths.
        pool = [
            e for e in self._entries
            if e.teachable and e.path not in exclude_paths
        ]
        if not pool:
            return []
        rng.shuffle(pool)
        return pool[:n]


    def sample_related(
        self,
        retriever,
        query: str,
        n: int = 3,
        min_chunk_count: int = 3,
        exclude_paths: set[str] | None = None,
    ) -> list[TopicMatch]:
        """
        Pick `n` teachable topics SEMANTICALLY RELATED to `query` rather
        than random. Uses the existing retriever to find chunks for the
        query, votes the top results onto (chapter, section, subsection),
        and returns the top-N teachable subsections that match.

        Falls back to `sample_diverse(n)` only when retrieval surfaces
        nothing related (true out-of-corpus query).

        Why: previously `sample_diverse(3)` picked 3 random teachable
        topics with no relation to what the student typed. Typing "brain"
        returned cards like "DNA Replication" and "Compensation
        Mechanisms" — useless. Now `sample_related` returns the topics
        the corpus actually covers around the query.
        """
        if not self._entries or not retriever or not (query or "").strip():
            return self.sample_diverse(n, exclude_paths=exclude_paths)

        exclude_paths = exclude_paths or set()
        try:
            chunks = retriever.retrieve(query, top_k=12)
        except Exception:
            return self.sample_diverse(n, exclude_paths=exclude_paths)
        if not chunks:
            return self.sample_diverse(n, exclude_paths=exclude_paths)

        # Vote chunks onto (chapter_num, section_title, subsection_title).
        # Weight by 1/(rank+1) so earlier results dominate.
        votes: dict[tuple, float] = {}
        for rank, c in enumerate(chunks):
            key = (
                c.get("chapter_num", 0) or 0,
                (c.get("section_title", "") or "").strip(),
                (c.get("subsection_title", "") or "").strip(),
            )
            if not key[2]:
                continue
            votes[key] = votes.get(key, 0.0) + 1.0 / (rank + 1)

        # Map vote keys back to TopicMatch entries (must be teachable + not excluded).
        # Path format: subsection title is the unique key inside the index.
        related: list[TopicMatch] = []
        seen_paths: set[str] = set()
        for key, _w in sorted(votes.items(), key=lambda kv: -kv[1]):
            ch_num, section_title, subsection_title = key
            for e in self._entries:
                if not e.teachable:
                    continue
                if e.path in exclude_paths:
                    continue
                if e.path in seen_paths:
                    continue
                if (e.subsection or "").strip() == subsection_title and (
                    not section_title or (e.section or "").strip() == section_title
                ):
                    if e.chunk_count < min_chunk_count:
                        continue
                    related.append(e)
                    seen_paths.add(e.path)
                    break
            if len(related) >= n:
                break

        # If retrieval didn't surface enough teachable matches, top up
        # from sample_diverse so we always return n cards.
        if len(related) < n:
            top_up = self.sample_diverse(
                n - len(related),
                exclude_paths=exclude_paths | seen_paths,
            )
            related.extend(top_up)

        return related[:n]


_matcher_singleton: TopicMatcher | None = None


def get_topic_matcher() -> TopicMatcher:
    """
    Lazy singleton — index is small (~360 entries) and immutable per process.
    Uses the domain-configured ontology adapter (UMLS for OT, Noop otherwise).
    """
    global _matcher_singleton
    if _matcher_singleton is None:
        from retrieval.ontology import get_ontology_adapter

        domain_name = getattr(getattr(cfg, "domain", None), "name", "") or ""
        retrieval_domain = getattr(
            getattr(cfg, "domain", None), "retrieval_domain", ""
        ) or ""
        # Prefer the retrieval_domain key when set (e.g. "anatomy") — it's
        # what `get_ontology_adapter` keys on.
        _matcher_singleton = TopicMatcher(
            ontology=get_ontology_adapter(retrieval_domain or domain_name)
        )
    return _matcher_singleton
