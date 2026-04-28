"""
tests/test_retriever_window.py
------------------------------
Unit tests for the D.0 window expansion in retrieval/retriever.py.

We test the _expand_window logic in isolation by injecting a fake
_chunks_index_cache rather than constructing a full Retriever (which
needs Qdrant + OpenAI clients live). This isolates the algorithm:

  - W=0 short-circuits and returns input unchanged
  - W=1 with full prev/next chain returns 3-chunk groups
  - Boundaries (first/last in subsection) emit fewer neighbors
  - Shared neighbors between two primaries are not duplicated
  - Token budget cap stops expansion before over-running
  - Missing chunks file (None index) returns input unchanged
"""
from __future__ import annotations

import pytest

from retrieval.retriever import Retriever


def _bare_retriever(index: dict | None) -> Retriever:
    """Build a Retriever shell that bypasses __init__ (which needs live
    Qdrant + OpenAI). We're only exercising _expand_window."""
    r = Retriever.__new__(Retriever)
    r._chunks_index_cache = index
    return r


def _make_chunk(chunk_id: str, prev: str | None = None, nxt: str | None = None,
                text: str = "x" * 50) -> dict:
    return {
        "chunk_id": chunk_id,
        "prev_chunk_id": prev,
        "next_chunk_id": nxt,
        "text": text,
        "chapter_num": 1,
        "chapter_title": "Test Chapter",
        "section_title": "Test Section",
        "subsection_title": "Test Subsection",
        "page": 1,
        "element_type": "paragraph",
    }


class TestWindowExpansion:
    def test_window_zero_passes_through(self):
        r = _bare_retriever({})
        primaries = [{"chunk_id": "A", "text": "primary A"}]
        out = r._expand_window(primaries, window_size=0)
        assert out == primaries

    def test_w1_with_full_chain_emits_three(self):
        idx = {
            "B": _make_chunk("B", prev="A", nxt="C"),
            "A": _make_chunk("A", prev=None, nxt="B"),
            "C": _make_chunk("C", prev="B", nxt=None),
        }
        r = _bare_retriever(idx)
        primaries = [{"chunk_id": "B", "text": "primary B"}]
        out = r._expand_window(primaries, window_size=1)
        assert len(out) == 3
        assert [c["chunk_id"] for c in out] == ["A", "B", "C"]
        roles = [c["_window_role"] for c in out]
        assert roles == ["neighbor_prev", "primary", "neighbor_next"]
        # Every neighbor must point back to its primary
        for c in out:
            assert c["_primary_chunk_id"] == "B"

    def test_w2_with_chain(self):
        idx = {
            "C": _make_chunk("C", prev="B", nxt="D"),
            "B": _make_chunk("B", prev="A", nxt="C"),
            "A": _make_chunk("A", prev=None, nxt="B"),
            "D": _make_chunk("D", prev="C", nxt="E"),
            "E": _make_chunk("E", prev="D", nxt=None),
        }
        r = _bare_retriever(idx)
        primaries = [{"chunk_id": "C", "text": "primary C"}]
        out = r._expand_window(primaries, window_size=2)
        assert [c["chunk_id"] for c in out] == ["A", "B", "C", "D", "E"]
        assert [c["_window_role"] for c in out] == [
            "neighbor_prev", "neighbor_prev", "primary", "neighbor_next", "neighbor_next"
        ]

    def test_first_chunk_no_prev(self):
        idx = {
            "A": _make_chunk("A", prev=None, nxt="B"),
            "B": _make_chunk("B", prev="A", nxt=None),
        }
        r = _bare_retriever(idx)
        out = r._expand_window([{"chunk_id": "A", "text": "primary A"}], window_size=1)
        assert [c["chunk_id"] for c in out] == ["A", "B"]
        assert out[0]["_window_role"] == "primary"
        assert out[1]["_window_role"] == "neighbor_next"

    def test_last_chunk_no_next(self):
        idx = {
            "A": _make_chunk("A", prev=None, nxt="B"),
            "B": _make_chunk("B", prev="A", nxt=None),
        }
        r = _bare_retriever(idx)
        out = r._expand_window([{"chunk_id": "B", "text": "primary B"}], window_size=1)
        assert [c["chunk_id"] for c in out] == ["A", "B"]
        assert out[0]["_window_role"] == "neighbor_prev"
        assert out[1]["_window_role"] == "primary"

    def test_isolated_chunk_no_neighbors(self):
        idx = {"X": _make_chunk("X", prev=None, nxt=None)}
        r = _bare_retriever(idx)
        out = r._expand_window([{"chunk_id": "X", "text": "primary X"}], window_size=2)
        assert len(out) == 1
        assert out[0]["chunk_id"] == "X"
        assert out[0]["_window_role"] == "primary"

    def test_two_primaries_share_neighbor_no_dup(self):
        idx = {
            "B": _make_chunk("B", prev="A", nxt="C"),
            "A": _make_chunk("A", prev=None, nxt="B"),
            "C": _make_chunk("C", prev="B", nxt="D"),
            "D": _make_chunk("D", prev="C", nxt=None),
        }
        r = _bare_retriever(idx)
        # Both B and D would naturally include C as a neighbor
        primaries = [
            {"chunk_id": "B", "text": "primary B"},
            {"chunk_id": "D", "text": "primary D"},
        ]
        out = r._expand_window(primaries, window_size=1)
        # C should appear exactly once (as B's next; D's prev gets skipped because C already emitted)
        cids = [c["chunk_id"] for c in out]
        assert cids.count("C") == 1
        # B and D both still primary
        primary_ids = [c["chunk_id"] for c in out if c["_window_role"] == "primary"]
        assert "B" in primary_ids
        assert "D" in primary_ids

    def test_token_budget_caps_expansion(self):
        # Make each chunk text 4000 chars (~1000 tok). Budget = 1500 tok =>
        # only ~1.5 chunks fit.
        big_text = "y" * 4000
        idx = {
            "B": _make_chunk("B", prev="A", nxt="C", text=big_text),
            "A": _make_chunk("A", prev=None, nxt="B", text=big_text),
            "C": _make_chunk("C", prev="B", nxt=None, text=big_text),
        }
        r = _bare_retriever(idx)
        primaries = [{"chunk_id": "B", "text": big_text}]
        out = r._expand_window(primaries, window_size=1, max_total_tokens=1500)
        # Primary always emitted; budget should stop A or C from joining
        assert any(c["chunk_id"] == "B" for c in out)
        assert len(out) <= 2  # primary + at most 1 neighbor

    def test_missing_chunks_file_passes_through(self):
        # No chunks index loaded — expansion should just return input.
        r = _bare_retriever({})
        primaries = [{"chunk_id": "A", "text": "primary A"}]
        out = r._expand_window(primaries, window_size=1)
        assert out == primaries

    def test_empty_input(self):
        r = _bare_retriever({"A": _make_chunk("A")})
        assert r._expand_window([], window_size=1) == []
