"""
retrieval/topic_mapper_llm.py
─────────────────────────────
L9 implementation — single Haiku call replaces the 3-stage pipeline
(LLM intent classify → semantic vote → RapidFuzz fallback).

Per docs/AUDIT_2026-05-02.md L9:

  Inputs:
    1. Student utterance (raw free-text).
    2. Full TOC + per-leaf 1-line summary
       (topic_index.json ⋈ raptor_subsection_summaries.jsonl).
    3. Curated abbreviation list (~30 entries: CN VII, RCA, LAD, ATP, etc.)
       framed as "common shortcuts students use, generalize from your own
       medical knowledge — not exhaustive".

  Output (strict JSON):
    {
      "verdict": "strong" | "borderline" | "none",
      "confidence": float,
      "student_intent": "topic_request",   # locked per L24 (deferred handling)
      "deferred_question": null,
      "top_matches": [
        {"path": "<chapter> > <section> > <subsection>",
         "confidence": float,
         "rationale": "<one sentence>"},
        ...up to 3
      ]
    }

  Routing thresholds (caller-side, see TopicMapperResult.route_decision()):
    verdict=strong,     conf >= 0.85   → lock_anchors_call → coverage gate
    verdict=borderline, conf in 0.7-0.85 → confirm-and-lock UX (L10)
    verdict=borderline, conf in 0.5-0.7  → cards from top_matches
    verdict=none,       conf <  0.5    → refuse intro + sample_diverse cards

This module is the *implementation*, not the wiring. Wiring into dean.py
(replacing the topic_matcher.TopicMatcher.match() call site) lands in a
follow-up commit so the rewrite can ship + be tested in isolation first.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from memory.sqlite_store import REPO  # repo root anchor (consistent with other modules)


# ─────────────────────────────────────────────────────────────────────────────
# Routing thresholds (per L9; mirrored on the caller side via .route_decision)
# ─────────────────────────────────────────────────────────────────────────────

STRONG_MIN_CONFIDENCE = 0.85
BORDERLINE_HIGH_MIN = 0.70   # 0.70 - 0.85 → confirm-and-lock
BORDERLINE_LOW_MIN = 0.50    # 0.50 - 0.70 → cards from top_matches
# below 0.50 → "none"

RouteDecision = Literal[
    "lock_immediately",          # strong, ≥0.85
    "confirm_and_lock",          # borderline, 0.70 – 0.85
    "show_top_matches",          # borderline, 0.50 – 0.70
    "refuse_with_starter_cards", # none, <0.50
]


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TopicMatchCandidate:
    path: str          # "Chapter > Section > Subsection"
    confidence: float
    rationale: str


@dataclass
class TopicMapperResult:
    """Parsed + validated output of the L9 Haiku call."""
    query: str
    verdict: Literal["strong", "borderline", "none"]
    confidence: float
    student_intent: Literal["topic_request"]   # locked per L24
    deferred_question: Optional[str]           # always None per L24
    top_matches: list[TopicMatchCandidate] = field(default_factory=list)
    raw_response: str = ""                     # for trace / debugging
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def route_decision(self) -> RouteDecision:
        """Map (verdict, confidence) to the caller's downstream action."""
        if self.verdict == "strong" and self.confidence >= STRONG_MIN_CONFIDENCE:
            return "lock_immediately"
        if self.verdict == "borderline" and self.confidence >= BORDERLINE_HIGH_MIN:
            return "confirm_and_lock"
        if self.verdict == "borderline" and self.confidence >= BORDERLINE_LOW_MIN:
            return "show_top_matches"
        return "refuse_with_starter_cards"

    def best_match(self) -> Optional[TopicMatchCandidate]:
        return self.top_matches[0] if self.top_matches else None


# ─────────────────────────────────────────────────────────────────────────────
# TOC + abbreviations loaders (cached per process)
# ─────────────────────────────────────────────────────────────────────────────

_TOC_BLOCK_CACHE: dict[str, str] = {}        # keyed by topic_index path
_ABBREVS_BLOCK_CACHE: dict[str, str] = {}    # keyed by curated_abbrevs path


def _load_topic_index(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    return raw if isinstance(raw, list) else list(raw.values())


def _load_raptor_summaries(path: Path) -> dict[tuple, str]:
    out: dict[tuple, str] = {}
    if not path.exists():
        return out
    for line in path.open():
        s = json.loads(line)
        k = (
            s.get("chapter") or s.get("chapter_title") or "",
            s.get("section") or s.get("section_title") or "",
            s.get("subsection") or s.get("subsection_title") or "",
        )
        out[k] = s.get("summary") or s.get("text") or ""
    return out


def build_toc_block(
    topic_index_path: Path,
    raptor_summaries_path: Path,
    *,
    use_cache: bool = True,
) -> str:
    """Build the TOC + summaries text block fed to the Haiku prompt.

    One line per topic_index entry, formatted as:
        <chapter> > <section> > <subsection>
            display_label: <label>
            summary: <one-line raptor summary>

    Cached by topic_index path (the heaviest part of the prompt — ~25K
    tokens for the full anatomy index — and stable for the lifetime of
    a process).
    """
    cache_key = str(topic_index_path.resolve())
    if use_cache and cache_key in _TOC_BLOCK_CACHE:
        return _TOC_BLOCK_CACHE[cache_key]

    entries = _load_topic_index(topic_index_path)
    summaries = _load_raptor_summaries(raptor_summaries_path)

    lines: list[str] = []
    for e in entries:
        ch = e.get("chapter") or e.get("chapter_title") or ""
        sec = e.get("section") or e.get("section_title") or ""
        sub = e.get("subsection") or e.get("subsection_title") or ""
        label = e.get("display_label") or sub
        if not (ch and sec and sub):
            continue
        summ = (summaries.get((ch, sec, sub)) or "").replace("\n", " ").strip()
        # Truncate summary to keep the block size bounded — first sentence
        # or 220 chars, whichever is shorter.
        if len(summ) > 220:
            summ = summ[:217] + "..."
        lines.append(
            f"- {ch} > {sec} > {sub}\n  display_label: {label}\n  summary: {summ}"
        )
    block = "\n".join(lines)
    if use_cache:
        _TOC_BLOCK_CACHE[cache_key] = block
    return block


def build_abbreviations_block(
    abbrevs_path: Path,
    *,
    use_cache: bool = True,
) -> str:
    """Format the curated abbreviation list as prompt text.

    Returns "" if the file is missing or unreadable (the LLM falls back
    on its own medical knowledge per L9's framing).
    """
    cache_key = str(abbrevs_path.resolve())
    if use_cache and cache_key in _ABBREVS_BLOCK_CACHE:
        return _ABBREVS_BLOCK_CACHE[cache_key]

    if not abbrevs_path.exists():
        block = ""
    else:
        try:
            data = json.loads(abbrevs_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        items = (data or {}).get("abbreviations", []) or []
        lines = []
        for item in items:
            short = (item.get("short") or "").strip()
            expansion = (item.get("expansion") or "").strip()
            context = (item.get("context") or "").strip()
            if not short or not expansion:
                continue
            tag = f" [{context}]" if context else ""
            lines.append(f"  - {short} = {expansion}{tag}")
        block = "\n".join(lines)
    if use_cache:
        _ABBREVS_BLOCK_CACHE[cache_key] = block
    return block


def _resolve_paths_from_cfg() -> tuple[Path, Path, Path]:
    """Resolve (topic_index, raptor_summaries, curated_abbrevs) paths from
    the active domain's config. Per L78 every path lives in a per-domain
    slot. Raises if the slots are missing — refuse to silently fall back."""
    from config import cfg as _cfg
    domain = _cfg.domain.retrieval_domain
    paths = _cfg.paths

    def _slot(name: str) -> Path:
        slot_name = f"{name}_{domain}"
        val = getattr(paths, slot_name, None)
        if not val:
            raise RuntimeError(
                f"Missing per-domain config slot cfg.paths.{slot_name} "
                f"(domain={domain!r}). Required by L9 topic_mapper_llm. "
                "Fix in config/base.yaml per L78."
            )
        p = Path(val)
        if not p.is_absolute():
            p = REPO / val
        return p

    return _slot("topic_index"), _slot("raptor_subsection_summaries"), _slot("curated_abbrevs")


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_HEADER = """You are mapping a {domain_name} student's free-text request to one or more
nodes in a {domain_short} textbook's table of contents (TOC).

You will receive:
  1. The student's utterance (raw free-text).
  2. The full TOC, one entry per line, formatted as:
       <chapter> > <section> > <subsection>
         display_label: <student-friendly card label>
         summary: <one-line subsection summary>
  3. A short list of common abbreviations students use. This list is
     NOT exhaustive — apply your own {domain_short} knowledge to expand
     other shortcuts (e.g. anatomical, physiological, common medical
     abbreviations beyond what's listed).

Your job: decide which (if any) TOC entries the student is asking about,
how confident you are, and return strict JSON.

Decision rules:
  - "strong":     confidence ≥ 0.85. The query unambiguously matches one
                  TOC entry. Return that one entry.
  - "borderline": confidence 0.50 – 0.85. The query plausibly matches
                  multiple TOC entries, OR matches one weakly. Return up
                  to 3 candidates ordered by confidence.
  - "none":       confidence < 0.50. The query doesn't match any TOC
                  entry well — could be off-topic, too vague, or about
                  something not in this textbook. Return up to 3 nearest
                  neighbours so the caller can surface them as suggestions.

Always return strict JSON only — no prose, no markdown fences. Use the
exact `path` text from the TOC (verbatim, including the " > " separators).

Output schema:
{{
  "verdict": "strong" | "borderline" | "none",
  "confidence": 0.0,
  "student_intent": "topic_request",
  "deferred_question": null,
  "top_matches": [
    {{"path": "<chapter> > <section> > <subsection>",
      "confidence": 0.0,
      "rationale": "<one sentence>"}}
  ]
}}
"""


def build_prompt(
    query: str,
    *,
    domain_name: str,
    domain_short: str,
    toc_block: str,
    abbrevs_block: str,
) -> str:
    header = PROMPT_HEADER.format(domain_name=domain_name, domain_short=domain_short)
    abbrevs_section = (
        f"\n\nCOMMON ABBREVIATIONS (non-exhaustive — generalize from your own "
        f"{domain_short} knowledge):\n{abbrevs_block}\n"
        if abbrevs_block
        else ""
    )
    return (
        f"{header}\n\nTOC:\n{toc_block}{abbrevs_section}\n\n"
        f"STUDENT UTTERANCE:\n{query}\n\n"
        "Output JSON only:"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def _parse_response_json(text: str) -> dict:
    """Strip markdown fences if any, then json.loads. Raises ValueError on
    anything that isn't a parseable object."""
    s = (text or "").strip()
    if s.startswith("```"):
        # ```json\n{...}\n```  or  ```\n{...}\n```
        lines = s.split("\n")
        if lines[-1].startswith("```"):
            s = "\n".join(lines[1:-1])
        else:
            s = "\n".join(lines[1:])
    try:
        out = json.loads(s)
    except json.JSONDecodeError:
        # Last-ditch — find the first JSON object
        import re
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise ValueError(f"No JSON object found in response: {text[:200]!r}")
        out = json.loads(m.group(0))
    if not isinstance(out, dict):
        raise ValueError(f"Response is not a JSON object: {out!r}")
    return out


def _validate_and_coerce(parsed: dict) -> tuple[str, float, list[TopicMatchCandidate]]:
    """Coerce the LLM output into the canonical types. Tolerant of missing
    fields (defaults to 'none' verdict)."""
    verdict = (parsed.get("verdict") or "none").strip().lower()
    if verdict not in {"strong", "borderline", "none"}:
        verdict = "none"
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    top_matches: list[TopicMatchCandidate] = []
    for raw in (parsed.get("top_matches") or [])[:3]:
        if not isinstance(raw, dict):
            continue
        path = (raw.get("path") or "").strip()
        if not path or " > " not in path:
            continue
        try:
            mc = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            mc = 0.0
        top_matches.append(TopicMatchCandidate(
            path=path,
            confidence=max(0.0, min(1.0, mc)),
            rationale=(raw.get("rationale") or "").strip(),
        ))
    return verdict, confidence, top_matches


def map_topic(
    query: str,
    *,
    client: Any,
    model: str,
    topic_index_path: Optional[Path] = None,
    raptor_summaries_path: Optional[Path] = None,
    curated_abbrevs_path: Optional[Path] = None,
    domain_name: Optional[str] = None,
    domain_short: Optional[str] = None,
    max_tokens: int = 1200,
    temperature: float = 0.0,
) -> TopicMapperResult:
    """Single Haiku call → TopicMapperResult.

    All `*_path` and `domain_*` args default to the active domain's config
    (per L78), so production callers just pass `client + model + query`.
    """
    if topic_index_path is None or raptor_summaries_path is None or curated_abbrevs_path is None:
        ti, rs, ca = _resolve_paths_from_cfg()
        topic_index_path = topic_index_path or ti
        raptor_summaries_path = raptor_summaries_path or rs
        curated_abbrevs_path = curated_abbrevs_path or ca

    if domain_name is None or domain_short is None:
        from config import cfg as _cfg
        domain_name = domain_name or _cfg.domain.name
        domain_short = domain_short or _cfg.domain.short

    toc_block = build_toc_block(topic_index_path, raptor_summaries_path)
    abbrevs_block = build_abbreviations_block(curated_abbrevs_path)
    prompt = build_prompt(
        query,
        domain_name=domain_name,
        domain_short=domain_short,
        toc_block=toc_block,
        abbrevs_block=abbrevs_block,
    )

    t0 = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        # Network / API failure — return a "none" result so the caller can
        # fall back to the L9 refuse-with-starter-cards path. Never raises.
        return TopicMapperResult(
            query=query,
            verdict="none",
            confidence=0.0,
            student_intent="topic_request",
            deferred_question=None,
            top_matches=[],
            raw_response=f"<llm_error: {type(e).__name__}: {str(e)[:120]}>",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    elapsed_ms = int((time.time() - t0) * 1000)

    raw_text = resp.content[0].text if resp.content else ""
    try:
        parsed = _parse_response_json(raw_text)
    except (ValueError, json.JSONDecodeError) as e:
        return TopicMapperResult(
            query=query,
            verdict="none",
            confidence=0.0,
            student_intent="topic_request",
            deferred_question=None,
            top_matches=[],
            raw_response=f"<json_parse_error: {e}; raw={raw_text[:200]!r}>",
            elapsed_ms=elapsed_ms,
        )

    verdict, confidence, top_matches = _validate_and_coerce(parsed)
    usage = getattr(resp, "usage", None)
    return TopicMapperResult(
        query=query,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        student_intent="topic_request",
        deferred_question=None,
        top_matches=top_matches,
        raw_response=raw_text,
        elapsed_ms=elapsed_ms,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def clear_caches() -> None:
    """Drop the TOC + abbreviation caches (useful for tests + after corpus rebuild)."""
    _TOC_BLOCK_CACHE.clear()
    _ABBREVS_BLOCK_CACHE.clear()
