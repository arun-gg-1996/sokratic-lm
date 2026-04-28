"""
ingestion/sources/openstax_anatomy/filters.py
---------------------------------------------
OpenStax-specific structural filters for the ingestion pipeline.

Why this lives here, not in `core/`:
  These detectors encode structural quirks of the OpenStax house style
  (back-matter layouts, sidebar markers, attribution patterns). They do
  not generalize to other publishers — each new textbook gets its own
  filters module.

Two responsibilities:

  (1) Back-matter detection — drop alphabetical index, glossary, references,
      and answer-key sections that the font-size parser misses because the
      heading fonts on those pages don't match the L1/L2 thresholds. The audit
      on 2026-04-28 found ~50-100 chunks polluted by these in `chunks_ot.jsonl`.

  (2) Sidebar marker handling — OpenStax embeds "Career Connection",
      "Everyday Connection", "Aging and the X System", "Disorders of the X",
      "Homeostatic Imbalances" sidebars inside section body text. We don't
      try to fully extract them (boundary detection from font size alone is
      hard), but we strip the bare marker phrases from body text so they
      don't leak into propositions.
"""
from __future__ import annotations

import re


# ── Back-matter detection ────────────────────────────────────────────────────

# Boilerplate headings to drop entirely. Used to AUGMENT the existing
# BOILERPLATE_TITLES set in parse.py. (Both layers need it because end-of-book
# back-matter sometimes uses different heading styles than end-of-chapter.)
BACK_MATTER_HEADINGS: frozenset[str] = frozenset({
    # Existing in parse.py:
    #   "key terms", "chapter review", "review questions",
    #   "interactive link questions", "critical thinking questions",
    #   "chapter objectives", "answers", "index", "references",
    #   "chapter summary"
    # Additions for end-of-book back-matter:
    "answer key",
    "glossary",
    "appendix",
    "appendix a", "appendix b", "appendix c", "appendix d",
    "the periodic table of the elements",
    "measurements and the metric system",
    "index of important terms",
    "credits",
    "license",
    "about openstax",
    "about openstax resources",
})


# Index-page detector: lines like
#   "abdominal aorta 846  abdominopelvic cavity 27  abducens nerve 522 …"
# These have a high density of "<Term> <PageNumber>" tokens.
_INDEX_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z\-]+\s+\d{1,4}\b")

# Glossary-paragraph detector: each entry is "term: definition" or
# "term  definition  page". Several "<Term>: " markers per paragraph.
_GLOSSARY_TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z\- ]{2,40}:\s")


def is_back_matter_text(text: str) -> bool:
    """
    Heuristic: is this section's body text actually back-matter content
    (alphabetical index, glossary listing, references list)?

    Triggered on either:
      - High index-pattern density (>1 "<Term> <Page>" per ~50 chars over the
        first 1000 chars), OR
      - High glossary-pattern density (>4 "<Term>:" markers in first 1000 chars), OR
      - References-style density (lines starting with "Author, A.").

    Calibrated empirically on Ch 28 polluted samples found in the audit.
    """
    if not text:
        return False

    sample = text[:1000]
    if len(sample) < 200:
        return False

    n_index_tokens = len(_INDEX_TOKEN_RE.findall(sample))
    if n_index_tokens >= 20:           # ~1 per 50 chars
        return True

    n_glossary_tokens = len(_GLOSSARY_TOKEN_RE.findall(sample))
    if n_glossary_tokens >= 4:
        return True

    # References list: lots of "Author, A." or "Author, A. (Year)." patterns.
    n_refs = len(re.findall(r"\b[A-Z][a-z]+,\s+[A-Z]\.\s*(?:[A-Z]\.\s*)?", sample))
    if n_refs >= 5:
        return True

    return False


def is_back_matter_heading(heading: str) -> bool:
    """True if heading text matches a known back-matter section label."""
    return (heading or "").strip().lower() in BACK_MATTER_HEADINGS


# ── Sidebar marker stripping ─────────────────────────────────────────────────

# Phrases that lead OpenStax sidebars. Stripped from body text inline so they
# don't appear as standalone tokens in propositions. Content of the sidebar is
# kept (we can't reliably segment the sidebar end without font info, so we
# leave the prose attached to the surrounding paragraph for now).
_SIDEBAR_MARKERS_RE = re.compile(
    r"\b(?:"
    r"INTERACTIVE\s+LINK"
    r"|EVERYDAY\s+CONNECTION"
    r"|CAREER\s+CONNECTION"
    r"|HOMEOSTATIC\s+IMBALANCES?"
    r"|AGING\s+AND\s+THE\s+[A-Z][A-Z\s]+SYSTEM"
    r"|DISORDERS\s+OF\s+THE\s+[A-Z][A-Z\s]+(?:SYSTEM)?"
    r"|Watch\s+this\s+(?:video|animation)"
    r"|View\s+this\s+animation"
    r"|Click\s+here"
    r")\b",
    re.IGNORECASE,
)


def strip_sidebar_markers(text: str) -> str:
    """Remove standalone sidebar marker phrases from body text.
    Does NOT remove the surrounding sidebar content (font-based boundary detection
    is too brittle); just removes the marker phrase itself so it doesn't end up
    as a proposition like "INTERACTIVE LINK Watch this animation"."""
    cleaned = _SIDEBAR_MARKERS_RE.sub("", text)
    # Also strip URLs that follow stripped markers
    cleaned = re.sub(r"\(https?://[^\s)]+\)", "", cleaned)
    cleaned = re.sub(r"\bhttps?://\S+", "", cleaned)
    # Collapse whitespace produced by stripping
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ── Legacy stub re-exports kept for the refactor's transitional period ───────

def is_back_matter(element: dict) -> bool:
    """Check an element dict (from extract.py) for back-matter content.

    This is a compatibility wrapper around `is_back_matter_text` for callers
    that pass full element dicts.
    """
    return is_back_matter_text((element or {}).get("text", ""))


def detect_sidebar(element: dict) -> str | None:
    """If element starts with a sidebar marker, return the matched marker label.
    Otherwise None."""
    text = (element or {}).get("text", "")
    m = _SIDEBAR_MARKERS_RE.search(text or "")
    return m.group(0) if m else None
