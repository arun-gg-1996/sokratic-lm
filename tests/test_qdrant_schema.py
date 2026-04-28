"""
tests/test_qdrant_schema.py
---------------------------
Unit tests for the v1 Qdrant payload schema and window-navigation metadata
(B.4). These pin down:

  - subsection_id normalization is deterministic and lookup-friendly
  - prev_chunk_id / next_chunk_id chains form correct intra-subsection links
  - subsection_chunk_count counts every chunk in the subsection (including
    table / figure_caption types that don't get prev/next links)
  - build_payload emits exactly the documented field set
  - chunk_type validation rejects unknown types
"""
from __future__ import annotations

import pytest

from ingestion.core.qdrant import (
    PAYLOAD_FIELDS,
    VALID_CHUNK_TYPES,
    PROMPT_VERSION_DEFAULT,
    build_payload,
    compute_window_nav_metadata,
    enrich_chunks_with_subsection_id,
    enrich_chunks_with_window_nav,
    normalize_subsection_id,
)


# ── normalize_subsection_id ─────────────────────────────────────────────────

class TestNormalizeSubsectionId:
    def test_basic_normalization(self):
        result = normalize_subsection_id(
            "openstax_anatomy", "20.1", "Shared Structure of Vessels"
        )
        assert result == "openstax_anatomy:20.1:shared_structure_of_vessels"

    def test_punctuation_collapsed_to_underscore(self):
        result = normalize_subsection_id(
            "openstax_anatomy", "23.4", "The Stomach: Anatomy & Function"
        )
        # ":", "&", spaces all collapse to single "_"
        assert result == "openstax_anatomy:23.4:the_stomach_anatomy_function"

    def test_em_dash_collapses(self):
        result = normalize_subsection_id(
            "openstax_anatomy", "7.2", "The Skull — Cranial Fossae"
        )
        assert result == "openstax_anatomy:7.2:the_skull_cranial_fossae"

    def test_empty_subsection_yields_trailing_colon(self):
        result = normalize_subsection_id("openstax_anatomy", "1.3", "")
        assert result == "openstax_anatomy:1.3:"

    def test_deterministic_same_input_same_output(self):
        a = normalize_subsection_id("ot", "20.1", "Shared Structure")
        b = normalize_subsection_id("ot", "20.1", "Shared Structure")
        assert a == b

    def test_case_insensitive(self):
        a = normalize_subsection_id("ot", "20.1", "Shared Structure")
        b = normalize_subsection_id("ot", "20.1", "shared structure")
        assert a == b


# ── compute_window_nav_metadata ─────────────────────────────────────────────

class TestComputeWindowNavMetadata:
    def _make_chunks(self, specs: list[tuple[str, str, str, int]]):
        """specs: [(chunk_id, subsection_id, chunk_type, sequence_index), ...]"""
        return [
            {
                "chunk_id": cid,
                "subsection_id": sub,
                "chunk_type": ctype,
                "sequence_index": seq,
            }
            for (cid, sub, ctype, seq) in specs
        ]

    def test_three_chunks_one_subsection_form_a_chain(self):
        chunks = self._make_chunks([
            ("c1", "ot:20.1:shared", "paragraph", 1),
            ("c2", "ot:20.1:shared", "paragraph", 2),
            ("c3", "ot:20.1:shared", "paragraph", 3),
        ])
        nav = compute_window_nav_metadata(chunks)
        assert nav["c1"]["prev_chunk_id"] is None
        assert nav["c1"]["next_chunk_id"] == "c2"
        assert nav["c2"]["prev_chunk_id"] == "c1"
        assert nav["c2"]["next_chunk_id"] == "c3"
        assert nav["c3"]["prev_chunk_id"] == "c2"
        assert nav["c3"]["next_chunk_id"] is None
        # All in same subsection, count = 3
        for c in ("c1", "c2", "c3"):
            assert nav[c]["subsection_chunk_count"] == 3
            # sequence_index is 0-indexed within subsection
        assert nav["c1"]["sequence_index"] == 0
        assert nav["c2"]["sequence_index"] == 1
        assert nav["c3"]["sequence_index"] == 2

    def test_separate_subsections_dont_link(self):
        chunks = self._make_chunks([
            ("c1", "ot:20.1:a", "paragraph", 1),
            ("c2", "ot:20.1:b", "paragraph", 2),
            ("c3", "ot:20.1:a", "paragraph", 3),
        ])
        nav = compute_window_nav_metadata(chunks)
        # c1 ↔ c3 are in subsection "a", c2 is in subsection "b"
        assert nav["c1"]["next_chunk_id"] == "c3"
        assert nav["c3"]["prev_chunk_id"] == "c1"
        assert nav["c2"]["prev_chunk_id"] is None
        assert nav["c2"]["next_chunk_id"] is None
        assert nav["c1"]["subsection_chunk_count"] == 2
        assert nav["c2"]["subsection_chunk_count"] == 1

    def test_table_and_figure_caption_dont_break_navigation_chain(self):
        # Tables and figure_captions exist in the subsection (count + index)
        # but don't link into the prev/next narrative chain — paragraphs link
        # past them.
        chunks = self._make_chunks([
            ("p1", "ot:20.1:shared", "paragraph", 1),
            ("t1", "ot:20.1:shared", "table", 2),
            ("p2", "ot:20.1:shared", "paragraph", 3),
            ("fc1", "ot:20.1:shared", "figure_caption", 4),
            ("p3", "ot:20.1:shared", "paragraph", 5),
        ])
        nav = compute_window_nav_metadata(chunks)
        # Paragraph chain: p1 ↔ p2 ↔ p3
        assert nav["p1"]["next_chunk_id"] == "p2"
        assert nav["p2"]["prev_chunk_id"] == "p1"
        assert nav["p2"]["next_chunk_id"] == "p3"
        assert nav["p3"]["prev_chunk_id"] == "p2"
        # Table and figure_caption have no prev/next (not navigable)
        assert nav["t1"]["prev_chunk_id"] is None
        assert nav["t1"]["next_chunk_id"] is None
        assert nav["fc1"]["prev_chunk_id"] is None
        assert nav["fc1"]["next_chunk_id"] is None
        # subsection_chunk_count counts everyone (5 total)
        for cid in ("p1", "t1", "p2", "fc1", "p3"):
            assert nav[cid]["subsection_chunk_count"] == 5

    def test_overlap_chunks_link_into_chain(self):
        # paragraph_overlap is navigable, so they link in.
        chunks = self._make_chunks([
            ("p1", "ot:20.1:s", "paragraph", 1),
            ("o1", "ot:20.1:s", "paragraph_overlap", 2),
            ("p2", "ot:20.1:s", "paragraph", 3),
        ])
        nav = compute_window_nav_metadata(chunks)
        assert nav["p1"]["next_chunk_id"] == "o1"
        assert nav["o1"]["prev_chunk_id"] == "p1"
        assert nav["o1"]["next_chunk_id"] == "p2"
        assert nav["p2"]["prev_chunk_id"] == "o1"

    def test_chunks_without_subsection_id_are_skipped(self):
        chunks = self._make_chunks([("c1", "", "paragraph", 1)])
        nav = compute_window_nav_metadata(chunks)
        assert nav == {}


# ── build_payload ───────────────────────────────────────────────────────────

class TestBuildPayload:
    def _proposition(self):
        return {
            "proposition_id": "p-uuid-1",
            "text": "The heart pumps blood.",
        }

    def _chunk(self, **overrides):
        chunk = {
            "chunk_id": "c-uuid-1",
            "text": "The heart is a four-chambered organ.",
            "chunk_type": "paragraph",
            "chapter_num": 19,
            "chapter_title": "The Heart",
            "section_num": "19.1",
            "section_title": "Heart Anatomy",
            "subsection_title": "Chambers",
            "page": 723,
        }
        chunk.update(overrides)
        return chunk

    def _nav(self, **overrides):
        nav = {
            "sequence_index": 2,
            "prev_chunk_id": "c-uuid-0",
            "next_chunk_id": "c-uuid-2",
            "subsection_chunk_count": 5,
        }
        nav.update(overrides)
        return nav

    def test_payload_has_exactly_the_documented_fields(self):
        payload = build_payload(
            self._proposition(), self._chunk(), self._nav(),
            textbook_id="openstax_anatomy",
        )
        assert set(payload.keys()) == set(PAYLOAD_FIELDS)

    def test_payload_values_propagate_correctly(self):
        payload = build_payload(
            self._proposition(), self._chunk(), self._nav(),
            textbook_id="openstax_anatomy",
        )
        assert payload["proposition_id"] == "p-uuid-1"
        assert payload["chunk_id"] == "c-uuid-1"
        assert payload["text"] == "The heart pumps blood."
        assert payload["parent_chunk_text"] == "The heart is a four-chambered organ."
        assert payload["textbook_id"] == "openstax_anatomy"
        assert payload["domain"] == "openstax_anatomy"  # legacy alias
        assert payload["chunk_type"] == "paragraph"
        assert payload["chapter_num"] == 19
        assert payload["section_num"] == "19.1"
        assert payload["section_title"] == "Heart Anatomy"
        assert payload["subsection_title"] == "Chambers"
        assert payload["subsection_id"] == "openstax_anatomy:19.1:chambers"
        assert payload["sequence_index"] == 2
        assert payload["prev_chunk_id"] == "c-uuid-0"
        assert payload["next_chunk_id"] == "c-uuid-2"
        assert payload["subsection_chunk_count"] == 5
        assert payload["page"] == 723
        assert payload["prompt_version"] == PROMPT_VERSION_DEFAULT
        assert payload["subsection_summary_id"] is None
        # ingested_at is ISO-format timestamp
        assert "T" in payload["ingested_at"]

    def test_chunk_type_validation_rejects_unknown(self):
        with pytest.raises(ValueError, match="invalid chunk_type"):
            build_payload(
                self._proposition(),
                self._chunk(chunk_type="appendix"),
                self._nav(),
                textbook_id="openstax_anatomy",
            )

    def test_chunk_type_falls_back_to_element_type(self):
        # Compatibility with chunker output that uses element_type
        chunk = self._chunk()
        chunk.pop("chunk_type")
        chunk["element_type"] = "paragraph_overlap"
        payload = build_payload(
            self._proposition(), chunk, self._nav(),
            textbook_id="openstax_anatomy",
        )
        assert payload["chunk_type"] == "paragraph_overlap"

    def test_all_valid_chunk_types_accepted(self):
        for ct in VALID_CHUNK_TYPES:
            payload = build_payload(
                self._proposition(),
                self._chunk(chunk_type=ct),
                self._nav(),
                textbook_id="openstax_anatomy",
            )
            assert payload["chunk_type"] == ct

    def test_missing_proposition_id_raises(self):
        with pytest.raises(ValueError, match="proposition must include"):
            build_payload(
                {"text": "..."},
                self._chunk(), self._nav(),
                textbook_id="openstax_anatomy",
            )

    def test_subsection_id_overridden_by_chunk(self):
        # If chunk already has subsection_id, build_payload uses it directly
        # rather than recomputing from section_num + subsection_title.
        chunk = self._chunk()
        chunk["subsection_id"] = "ot:99.99:override_me"
        payload = build_payload(
            self._proposition(), chunk, self._nav(),
            textbook_id="openstax_anatomy",
        )
        assert payload["subsection_id"] == "ot:99.99:override_me"

    def test_prompt_version_and_ingested_at_overridable(self):
        payload = build_payload(
            self._proposition(), self._chunk(), self._nav(),
            textbook_id="openstax_anatomy",
            prompt_version="v2-experimental",
            ingested_at="2026-04-28T12:34:56+00:00",
        )
        assert payload["prompt_version"] == "v2-experimental"
        assert payload["ingested_at"] == "2026-04-28T12:34:56+00:00"


# ── enrich_chunks_with_* ────────────────────────────────────────────────────

class TestEnrichmentHelpers:
    def test_enrich_subsection_id_idempotent(self):
        chunks = [
            {"chunk_id": "c1", "section_num": "20.1", "subsection_title": "Vessels"},
            {"chunk_id": "c2", "section_num": "20.1", "subsection_title": "Vessels",
             "subsection_id": "preset:20.1:already_set"},
        ]
        enrich_chunks_with_subsection_id(chunks, textbook_id="ot")
        assert chunks[0]["subsection_id"] == "ot:20.1:vessels"
        # c2 had a preset value — must not be overwritten
        assert chunks[1]["subsection_id"] == "preset:20.1:already_set"

    def test_enrich_window_nav_writes_all_four_fields(self):
        chunks = [
            {"chunk_id": "c1", "subsection_id": "ot:20.1:s",
             "chunk_type": "paragraph", "sequence_index": 1},
            {"chunk_id": "c2", "subsection_id": "ot:20.1:s",
             "chunk_type": "paragraph", "sequence_index": 2},
        ]
        enrich_chunks_with_window_nav(chunks)
        assert chunks[0]["sequence_index"] == 0
        assert chunks[0]["prev_chunk_id"] is None
        assert chunks[0]["next_chunk_id"] == "c2"
        assert chunks[0]["subsection_chunk_count"] == 2
        assert chunks[1]["sequence_index"] == 1
        assert chunks[1]["prev_chunk_id"] == "c1"
        assert chunks[1]["next_chunk_id"] is None
