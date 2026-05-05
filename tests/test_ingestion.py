"""
tests/test_ingestion.py
-----------------------
Stage gate tests for the ingestion pipeline.
Run after ingestion is complete, before building the retrieval pipeline.

Tests:
  1. Chunk count in expected range
  2. Proposition count in expected range
  3. All chunks have required metadata fields
  4. No empty or suspiciously short chunks
  5. textbook_structure.json exists and has expected chapters
  6. All propositions have a valid parent_chunk_id that exists in chunks
  7. Table chunks detected — count + per-chapter report (informational)
  8. Figure caption chunks detected — count (informational)
  9. Chapters with zero tables flagged as warning (possible missed tables)

NOTE on table tests:
  Tests 7-9 are informational — they print a report but do NOT fail the gate
  unless table count is 0 across the entire book (which would mean extraction
  completely missed all tables). Per-chapter zeros are warnings only, because
  some chapters genuinely have no tables.

Pass all → safe to build retrieval.
Fail any hard test → something went wrong in ingestion, do not proceed.

Run:
    python -m pytest tests/test_ingestion.py -v
"""

import json
import pytest
from collections import defaultdict
from pathlib import Path
from config import cfg

# Expected ranges — adjusted for parse_pdf.py + overlap-chunk architecture
CHUNK_COUNT_MIN = 3000
CHUNK_COUNT_MAX = 10000
PROPOSITION_COUNT_MIN = 3000
PROPOSITION_COUNT_MAX = 50000
CHUNK_MIN_CHARS = 50      # base chunks shorter than this are suspicious

# Minimum total table chunks across the whole book.
# OpenStax A&P has many tables — if we see fewer than this, extraction likely failed.
TABLE_CHUNK_MIN = 30

REQUIRED_CHUNK_FIELDS = {"text", "chunk_id", "chapter_title", "section_title", "page", "element_type"}
REQUIRED_PROP_FIELDS = {"text", "proposition_id", "parent_chunk_id", "chapter_title", "section_title"}


@pytest.fixture(scope="module")
def chunks():
    path = Path(cfg.paths.chunks_openstax_anatomy)
    assert path.exists(), f"chunks file not found: {path}"
    with open(path) as f:
        return [json.loads(line) for line in f]


@pytest.fixture(scope="module")
def propositions():
    path = Path(cfg.paths.propositions_openstax_anatomy)
    assert path.exists(), f"propositions file not found: {path}"
    with open(path) as f:
        return [json.loads(line) for line in f]


def test_chunk_count_in_range(chunks):
    n = len(chunks)
    assert CHUNK_COUNT_MIN <= n <= CHUNK_COUNT_MAX, (
        f"Chunk count {n} outside expected range [{CHUNK_COUNT_MIN}, {CHUNK_COUNT_MAX}]. "
        f"Too low = extraction failed. Too high = check if overlap chunks are being double-counted."
    )


def test_proposition_count_in_range(propositions):
    n = len(propositions)
    assert PROPOSITION_COUNT_MIN <= n <= PROPOSITION_COUNT_MAX, (
        f"Proposition count {n} outside expected range "
        f"[{PROPOSITION_COUNT_MIN}, {PROPOSITION_COUNT_MAX}]."
    )


def test_chunk_metadata_fields(chunks):
    missing = []
    for i, chunk in enumerate(chunks):
        missing_fields = REQUIRED_CHUNK_FIELDS - set(chunk.keys())
        if missing_fields:
            missing.append((i, missing_fields))
    assert not missing, f"Chunks missing required fields: {missing[:5]}"


def test_proposition_metadata_fields(propositions):
    missing = []
    for i, prop in enumerate(propositions):
        missing_fields = REQUIRED_PROP_FIELDS - set(prop.keys())
        if missing_fields:
            missing.append((i, missing_fields))
    assert not missing, f"Propositions missing required fields: {missing[:5]}"


def test_no_empty_chunks(chunks):
    # Overlap chunks are intentionally short sentence-prefix context.
    non_overlap_chunks = [
        c for c in chunks
        if not c.get("is_overlap", False)
        and c.get("element_type") != "paragraph_overlap"
    ]
    short = [
        (i, len(c["text"]))
        for i, c in enumerate(non_overlap_chunks)
        if len(c["text"]) < CHUNK_MIN_CHARS
    ]
    assert not short, (
        f"{len(short)} chunks are suspiciously short (< {CHUNK_MIN_CHARS} chars). "
        f"First few: {short[:5]}"
    )


def test_proposition_parent_chunk_ids_valid(chunks, propositions):
    """Every proposition's parent_chunk_id must exist in the chunks file."""
    valid_ids = {c["chunk_id"] for c in chunks}
    orphans = [p for p in propositions if p["parent_chunk_id"] not in valid_ids]
    assert not orphans, (
        f"{len(orphans)} propositions have invalid parent_chunk_id. "
        f"First: {orphans[0]}"
    )


def test_table_chunks_detected(chunks):
    """
    HARD: total table chunks across the book must be >= TABLE_CHUNK_MIN.
    If 0 tables detected, pdfplumber extraction is broken or tables are image-based.

    INFORMATIONAL: prints per-chapter table count so you can spot chapters
    where tables might have been missed. A chapter showing 0 is a warning,
    not a failure (some chapters have no tables).
    """
    table_chunks = [c for c in chunks if c.get("element_type") == "table"]
    total = len(table_chunks)

    # Per-chapter breakdown
    by_chapter = defaultdict(int)
    for c in table_chunks:
        by_chapter[c["chapter_title"]] += 1

    # Print report — always visible in pytest -v output
    print(f"\n--- Table extraction report ---")
    print(f"Total table chunks: {total}")
    print(f"Per chapter:")
    all_chapters = {c["chapter_title"] for c in chunks}
    for chapter in sorted(all_chapters):
        count = by_chapter.get(chapter, 0)
        flag = " ⚠️  WARNING: 0 tables — check if this chapter has tables in the PDF" if count == 0 else ""
        print(f"  {chapter}: {count} table chunks{flag}")
    print(f"------------------------------")

    assert total >= TABLE_CHUNK_MIN, (
        f"Only {total} table chunks found across the whole book (expected >= {TABLE_CHUNK_MIN}). "
        f"Likely cause: pdfplumber not detecting tables, or tables are embedded as images. "
        f"Check extract.py table detection logic."
    )


def test_figure_caption_chunks_detected(chunks):
    """
    INFORMATIONAL: prints figure caption count.
    For parse_pdf.py architecture, figure captions are absorbed into body text,
    so zero explicit figure_caption chunks is expected.
    """
    caption_chunks = [c for c in chunks if c.get("element_type") == "figure_caption"]
    total = len(caption_chunks)

    print(f"\n--- Figure caption report ---")
    print(f"Total figure caption chunks: {total}")
    print(f"Note: parse_pdf.py absorbs captions into body text (expected)")
    print(f"-----------------------------")


def test_element_type_distribution(chunks):
    """
    INFORMATIONAL: prints the breakdown of element_type across all chunks.
    Helps spot if one type is drastically under-represented.
    Expected rough ratio for OpenStax A&P: ~80% paragraph, ~10% table, ~10% figure_caption.
    Not a hard assertion — just a sanity print.
    """
    by_type = defaultdict(int)
    for c in chunks:
        by_type[c.get("element_type", "unknown")] += 1

    total = len(chunks)
    print(f"\n--- Element type distribution ---")
    for etype, count in sorted(by_type.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"  {etype}: {count} ({pct:.1f}%)")
    print(f"---------------------------------")

    # Only hard-fail if unknown types appear (means element_type not being set)
    unknown = by_type.get("unknown", 0)
    assert unknown == 0, (
        f"{unknown} chunks have no element_type set. "
        f"Every chunk must be tagged as paragraph, table, or figure_caption."
    )


def test_textbook_structure_exists():
    path = Path(cfg.paths.textbook_structure)
    assert path.exists(), f"textbook_structure.json not found at {path}"
    with open(path) as f:
        structure = json.load(f)
    assert len(structure) > 0, "textbook_structure.json is empty"
    # Spot check: every top-level key should have a 'difficulty' field
    for chapter, data in list(structure.items())[:5]:
        assert "difficulty" in data, f"Chapter '{chapter}' missing difficulty label"
