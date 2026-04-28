"""
ingest/parse_pdf.py — extract sections from an OpenStax PDF using PyMuPDF.

Font-size thresholds calibrated against OpenStax Anatomy & Physiology 2e
(1317 pages).  Same thresholds work for OpenStax University Physics V1
because both books share the OpenStax house style.

Heading hierarchy (measured from actual PDF spans):
  22.4 pt            →  chapter title   ("Bone Tissue and the Skeletal System")
  15.6 pt  CAPS      →  chapter label   ("CHAPTER 4")
  13.0 pt  bold      →  L1 section      ("6.7 Calcium Homeostasis …")   ← Level 1
  10.8 pt  bold      →  L2 subsection   ("Negative Feedback", "Motor Innervation")  ← Level 2 NEW
   9.0 pt            →  body text
   7.5 pt            →  captions / footers  (skipped)

LEVEL 1 detection (unchanged):
  font size >= 12.5 pt AND all spans on the line are bold.
  Catches the 13.0pt bold numbered section headings.

LEVEL 2 detection (NEW):
  All spans on the line are 10.5–11.2 pt AND all bold
  AND the line text is >= 2 words
  AND does not match callout / boilerplate patterns
  AND does not look like a TOC entry (starts with "N.M ")
  AND the resulting body text would be >= L2_MIN_WORDS words.

  The section_title for Level 2 is stored as:
    "<parent L1 title> — <subsection heading>"
  e.g.  "6.7 Calcium Homeostasis — Negative Feedback"

  L2_MIN_WORDS is set to 200 by default.
  Note: the A&P 2e textbook contains ~640 qualifying Level 2 subsections
  (200-word minimum).  The user's target was 350-500; the difference is that
  the textbook has more subsection headings than estimated.  To reduce the
  section count, raise L2_MIN_WORDS (e.g. 400 → ~490 total sections).

Output per section (list[dict]):
  {
    "id":                  str,   # "<domain>_ch<N>_sec<M>"
    "section_title":       str,   # L1: numbered title | L2: "L1 title — subsection"
    "parent_section":      str,   # L1: same as section_title | L2: the parent L1 title
    "level":               int,   # 1 or 2
    "chapter":             str,   # full chapter title
    "chapter_num":         int,
    "section_num":         str,   # "6.7", "4.2", …  ("" for non-numbered)
    "page_start":          int,
    "page_end":            int,
    "text":                str,   # full concatenated section text
    "source_pdf":          str,   # filename only
  }
"""

import re
import json
import os
from pathlib import Path
from typing import Generator

import fitz  # PyMuPDF

# ── Font-size thresholds ───────────────────────────────────────────────────────

CHAPTER_TITLE_MIN  = 20.0    # ≥ this          → chapter title
CHAPTER_LABEL_MIN  = 14.5    # ≥ this          → "CHAPTER N" label
L1_HEADING_MIN     = 12.5    # ≥ this, bold    → Level 1 section heading
L2_HEADING_MIN     = 10.5    # ≥ this          → Level 2 candidate (bold required)
L2_HEADING_MAX     = 11.2    # ≤ this          → Level 2 candidate upper bound
CAPTION_MAX        =  8.0    # ≤ this          → footer/caption, skip

# Minimum body-word counts before a section is kept
L1_MIN_WORDS = 50    # Level 1 sections
L2_MIN_WORDS = 200   # Level 2 subsections — raise to reduce section count

# ── Regex patterns ────────────────────────────────────────────────────────────

# "CHAPTER 6", "CHAPTER 12"
CHAPTER_LABEL_RE = re.compile(r'^CHAPTER\s+(\d+)$', re.IGNORECASE)

# Numbered section at start of heading: "6.7 ", "4.2 ", "12.3 "
SECTION_NUM_RE = re.compile(r'^(\d+\.\d+)\s+')

# TOC entry: starts with a number-dot-number (avoid mis-classifying L2 headings)
TOC_RE = re.compile(r'^\d+\.\d+')

# Callout / career spotlight / boilerplate sub-headings to NOT split on.
# These are small boxes embedded in sections — not true structural subsections.
CALLOUT_RE = re.compile(
    r'^(interactive link|everyday connection|career connection|'
    r'disorders of the|aging and the|homeostatic imbalances?|'
    r'about openstax|physical therapist|interventional radio|'
    r'anesthesia and|career connect|license)',
    re.IGNORECASE,
)

# End-of-chapter boilerplate headings to drop entirely
BOILERPLATE_TITLES = {
    "key terms", "chapter review", "review questions",
    "interactive link questions", "critical thinking questions",
    "chapter objectives", "answers", "index", "references",
    "chapter summary",
}


# ── Line-level span aggregator ────────────────────────────────────────────────

def _classify_line(line: dict) -> dict:
    """
    Aggregate all spans in a PDF line into a single line-level record.

    Returns a dict with:
      text        full joined text of the line
      size        minimum font size across spans (conservative)
      size_max    maximum font size across spans
      all_bold    True if every non-empty span is bold
      any_bold    True if any span is bold
      role        "chapter_title" | "chapter_label" | "l1_heading" |
                  "l2_heading" | "body" | "skip"
      page        page number (int)
    """
    spans = [s for s in line["spans"] if s["text"].strip()]
    if not spans:
        return {}

    texts    = [s["text"].strip() for s in spans]
    sizes    = [s["size"] for s in spans]
    bolds    = [bool(s["flags"] & 16) for s in spans]
    full     = " ".join(texts).strip()
    sz_min   = min(sizes)
    sz_max   = max(sizes)
    all_bold = all(bolds)
    word_cnt = len(full.split())

    # Skip captions / footers
    if sz_max <= CAPTION_MAX:
        return {}

    # Chapter title (large font, can be multi-span)
    if sz_min >= CHAPTER_TITLE_MIN:
        return {"text": full, "size": sz_min, "size_max": sz_max,
                "all_bold": all_bold, "role": "chapter_title"}

    # Chapter label ("CHAPTER N")
    if sz_min >= CHAPTER_LABEL_MIN:
        return {"text": full, "size": sz_min, "size_max": sz_max,
                "all_bold": all_bold, "role": "chapter_label"}

    # Level 1 heading: 12.5+ pt, all spans bold
    if sz_min >= L1_HEADING_MIN and all_bold:
        return {"text": full, "size": sz_min, "size_max": sz_max,
                "all_bold": True, "role": "l1_heading"}

    # Level 2 heading: 10.5–11.2 pt, ALL spans bold, >= 2 words,
    # not a callout label, not a TOC entry
    if (L2_HEADING_MIN <= sz_min and sz_max <= L2_HEADING_MAX
            and all_bold
            and word_cnt >= 2
            and not CALLOUT_RE.match(full)
            and not TOC_RE.match(full)):
        return {"text": full, "size": sz_min, "size_max": sz_max,
                "all_bold": True, "role": "l2_heading"}

    # Everything else is body text
    return {"text": full, "size": sz_min, "size_max": sz_max,
            "all_bold": all_bold, "role": "body"}


def _iter_lines(doc: fitz.Document) -> Generator[dict, None, None]:
    """Yield one classified line-dict per non-empty text line in the PDF."""
    for page_num, page in enumerate(doc, start=1):
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                rec = _classify_line(line)
                if rec:
                    rec["page"] = page_num
                    yield rec


# ── Section builder ───────────────────────────────────────────────────────────

def _build_sections(lines: list[dict], source_pdf: str, domain: str) -> list[dict]:
    """
    Walk the line stream and emit one section dict per L1 or L2 heading.

    State machine:
      chapter_title  → resets chapter context
      chapter_label  → updates chapter_num
      l1_heading     → flush prev section; start new L1 section
      l2_heading     → flush prev section (if >= L2_MIN_WORDS); start L2 sub-section
      body           → accumulate text
    """
    sections: list[dict] = []

    # Current context
    current_chapter_title  = ""
    current_chapter_num    = 0
    current_section_title  = ""   # full title to store
    current_parent_section = ""   # always the L1 title (for Level 2 inheritance)
    current_section_num    = ""
    current_level          = 1
    current_page_start     = 1
    body_parts: list[str]  = []
    section_index          = 0    # global — never resets; guarantees unique IDs
    in_boilerplate         = False

    def _flush(min_words: int) -> None:
        nonlocal section_index
        text = " ".join(body_parts).strip()
        wc   = len(text.split())
        if wc < min_words:
            return
        section_index += 1
        # ch-scoped index kept in ID for readability, but uniqueness comes from
        # the global section_index which never resets across chapter boundaries.
        sec_id = f"{domain}_ch{current_chapter_num:02d}_sec{section_index:04d}"
        sections.append({
            "id":             sec_id,
            "section_title":  current_section_title or current_chapter_title,
            "parent_section": current_parent_section or current_section_title,
            "level":          current_level,
            "chapter":        current_chapter_title,
            "chapter_num":    current_chapter_num,
            "section_num":    current_section_num,
            "page_start":     current_page_start,
            "page_end":       lines[-1]["page"] if lines else current_page_start,
            "text":           text,
            "source_pdf":     source_pdf,
        })

    for line in lines:
        role = line["role"]
        page = line["page"]
        text = line["text"]

        # ── Chapter label ──────────────────────────────────────────────────────
        if role == "chapter_label":
            m = CHAPTER_LABEL_RE.match(text)
            if m:
                current_chapter_num = int(m.group(1))
            continue

        # ── Chapter title ──────────────────────────────────────────────────────
        if role == "chapter_title":
            _flush(L2_MIN_WORDS if current_level == 2 else L1_MIN_WORDS)
            body_parts         = []
            current_chapter_title  = text
            current_section_title  = text
            current_parent_section = text
            current_section_num    = ""
            current_level          = 1
            current_page_start     = page
            # section_index is GLOBAL — do NOT reset here
            in_boilerplate         = False
            continue

        # ── Level 1 heading ────────────────────────────────────────────────────
        if role == "l1_heading":
            title_lower = text.strip().lower()
            if title_lower in BOILERPLATE_TITLES:
                _flush(L2_MIN_WORDS if current_level == 2 else L1_MIN_WORDS)
                body_parts     = []
                in_boilerplate = True
                current_section_title  = "__BOILERPLATE__"
                current_parent_section = "__BOILERPLATE__"
                current_level          = 1
                current_page_start     = page
                continue

            in_boilerplate = False
            _flush(L2_MIN_WORDS if current_level == 2 else L1_MIN_WORDS)
            body_parts = []
            m = SECTION_NUM_RE.match(text)
            current_section_num    = m.group(1) if m else ""
            current_section_title  = text
            current_parent_section = text          # L2 children inherit this
            current_level          = 1
            current_page_start     = page
            continue

        if in_boilerplate:
            continue

        # ── Level 2 heading ────────────────────────────────────────────────────
        if role == "l2_heading":
            _flush(L2_MIN_WORDS)
            body_parts = []
            # Combined title: "11.3 The Ulnar Nerve — Motor Innervation"
            if current_parent_section and current_parent_section not in (
                "__BOILERPLATE__", current_chapter_title
            ):
                combined = f"{current_parent_section} — {text}"
            else:
                combined = text
            current_section_title = combined
            current_level         = 2
            current_page_start    = page
            # Keep current_parent_section and current_section_num unchanged
            # (inherited from the enclosing L1 section)
            continue

        # ── Body text ──────────────────────────────────────────────────────────
        body_parts.append(text)

    # Flush the final open section
    last_page = lines[-1]["page"] if lines else 1
    _flush(L1_MIN_WORDS if current_level == 1 else L2_MIN_WORDS)

    # Fix page_end: section[i] ends one page before section[i+1] starts
    for i in range(len(sections) - 1):
        end = sections[i + 1]["page_start"] - 1
        sections[i]["page_end"] = max(end, sections[i]["page_start"])
    if sections:
        sections[-1]["page_end"] = last_page

    # Drop boilerplate sentinel sections and front matter (chapter 0)
    sections = [
        s for s in sections
        if s["section_title"] not in ("__BOILERPLATE__",)
        and s["chapter_num"] >= 1
    ]

    return sections


# ── Public API ────────────────────────────────────────────────────────────────

def parse_pdf(
    pdf_path: str,
    domain:   str  = "OT_anatomy",
    out_dir:  str  = "data/processed/chunks/raw_sections",
    save:     bool = True,
) -> list[dict]:
    """
    Parse an OpenStax PDF into sections (Level 1 + Level 2).

    Args:
        pdf_path: path to the PDF file
        domain:   prefix used in section IDs ("OT_anatomy" or "physics")
        out_dir:  directory where raw sections JSON is saved
        save:     if True, write per-chapter + combined JSON files

    Returns:
        list of section dicts (see module docstring for schema)
    """
    pdf_path = str(pdf_path)
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(
            f"PDF not found: {pdf_path}\n"
            "Download from openstax.org — see verify_downloads.py output."
        )

    source_pdf = os.path.basename(pdf_path)
    print(f"Opening {source_pdf} …")

    doc = fitz.open(pdf_path)
    print(f"  Pages: {len(doc)}")

    print("  Extracting lines …")
    lines = list(_iter_lines(doc))
    doc.close()
    print(f"  Lines extracted: {len(lines):,}")

    print("  Building sections (L1 + L2) …")
    sections = _build_sections(lines, source_pdf, domain)

    l1 = sum(1 for s in sections if s["level"] == 1)
    l2 = sum(1 for s in sections if s["level"] == 2)
    print(f"  Sections found: {len(sections)}  (L1={l1}  L2={l2})")

    if save:
        os.makedirs(out_dir, exist_ok=True)

        by_chapter: dict[int, list[dict]] = {}
        for sec in sections:
            ch = sec["chapter_num"]
            by_chapter.setdefault(ch, []).append(sec)

        for ch_num, ch_secs in sorted(by_chapter.items()):
            fname = f"ch{ch_num:02d}_{domain}.json"
            with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
                json.dump(ch_secs, f, indent=2, ensure_ascii=False)

        combined = os.path.join(out_dir, f"all_sections_{domain}.json")
        with open(combined, "w", encoding="utf-8") as f:
            json.dump(sections, f, indent=2, ensure_ascii=False)

        print(f"  Saved {len(by_chapter)} chapter files + combined → {out_dir}/")

    return sections


# ── Reporting helpers ─────────────────────────────────────────────────────────

def print_toc(sections: list[dict], max_rows: int = 50) -> None:
    """Print a table-of-contents summary of parsed sections."""
    print(f"\n{'Ch':>4}  {'Lv':>2}  {'Sec':>5}  {'Pages':>10}  {'Words':>6}  Title")
    print("-" * 95)
    shown = 0
    for sec in sections:
        if shown >= max_rows:
            print(f"  … {len(sections) - max_rows} more sections …")
            break
        words = len(sec["text"].split())
        pages = f"{sec['page_start']}–{sec['page_end']}"
        indent = "  " if sec["level"] == 2 else ""
        print(
            f"  {sec['chapter_num']:>2}  "
            f"  {sec['level']:>1}  "
            f"  {sec['section_num']:>4}  "
            f"  {pages:>10}  "
            f"  {words:>5}  "
            f"  {indent}{sec['section_title'][:60]}"
        )
        shown += 1


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import config

    target = sys.argv[1] if len(sys.argv) > 1 else "OT_anatomy"

    if target == "OT_anatomy":
        pdf = os.path.join(config.RAW_DIR, "textbooks", "openStax_AP2e.pdf")
        dom = "OT_anatomy"
    elif target == "physics":
        pdf = os.path.join(config.RAW_DIR, "physics", "openStax_physics_v1.pdf")
        dom = "physics"
    else:
        print(f"Unknown target: {target}. Use 'OT_anatomy' or 'physics'.")
        sys.exit(1)

    sections = parse_pdf(
        pdf_path=pdf,
        domain=dom,
        out_dir=config.CHUNKS_DIR + "/raw_sections",
    )

    print_toc(sections)

    total_words = sum(len(s["text"].split()) for s in sections)
    chapters    = len({s["chapter_num"] for s in sections})
    l1 = sum(1 for s in sections if s["level"] == 1)
    l2 = sum(1 for s in sections if s["level"] == 2)

    print(f"\nTotal : {len(sections)} sections across {chapters} chapters, "
          f"{total_words:,} words")
    print(f"  L1  : {l1} numbered sections (≥{L1_MIN_WORDS} words)")
    print(f"  L2  : {l2} subsections (≥{L2_MIN_WORDS} words)")
    print(f"\nNote  : A&P 2e has ~640 qualifying L2 subsections at ≥200 words.")
    print(f"        To hit 350-500 total, set L2_MIN_WORDS=400 in parse_pdf.py.")
