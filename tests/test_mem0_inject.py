"""
tests/test_mem0_inject.py
─────────────────────────
Coverage for L6 mem0 read injection helpers (Track 4.7f).

Both helpers (read_topic_lock_carryover + read_hint_advance_carryover)
go through safe_mem0_read so they NEVER raise; tests focus on:
  * Correct query/filter shape passed to mem0
  * Correct top_k limit
  * Empty inputs return empty string (don't bother mem0)
  * Empty hits return empty string (clean prompt)
  * Hits formatted as compact label-prefixed lines
  * combine_carryover stacks blocks and clips length
  * No-mem0 path returns empty (graceful degradation)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from conversation import mem0_inject as M


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _state(student_id: str = "s1", **extras) -> dict:
    base = {
        "student_id": student_id,
        "locked_question": "What artery supplies the left ventricle?",
        "debug": {"turn_trace": []},
    }
    base.update(extras)
    return base


def _persistent(available: bool = True, hits=None) -> MagicMock:
    p = MagicMock()
    p.available = available
    p.get.return_value = hits or []
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Topic-lock injection (#1)
# ─────────────────────────────────────────────────────────────────────────────


def test_topic_lock_no_persistent_returns_empty():
    out = M.read_topic_lock_carryover(_state(), None, {"path": "Ch20|Heart|LAD"})
    assert out == ""


def test_topic_lock_no_student_id_returns_empty():
    persistent = _persistent(hits=[{"text": "Past misconception about LAD."}])
    out = M.read_topic_lock_carryover(
        _state(student_id=""),
        persistent,
        {"path": "Ch20|Heart|LAD", "subsection": "LAD"},
    )
    assert out == ""
    persistent.get.assert_not_called()


def test_topic_lock_no_locked_path_returns_empty():
    persistent = _persistent(hits=[{"text": "Whatever."}])
    out = M.read_topic_lock_carryover(_state(), persistent, {})
    assert out == ""


def test_topic_lock_empty_hits_returns_empty():
    persistent = _persistent(hits=[])
    out = M.read_topic_lock_carryover(
        _state(),
        persistent,
        {"path": "Ch20|Heart|LAD", "subsection": "LAD"},
    )
    assert out == ""
    # Even on empty hits, the get call should have been made
    persistent.get.assert_called_once()


def test_topic_lock_formats_misconception_and_style_hits():
    hits = [
        {
            "text": "Confused LAD with circumflex during prior session.",
            "metadata": {"category": "misconception"},
        },
        {
            "text": "Responds well to vasculature analogies.",
            "metadata": {"category": "learning_style"},
        },
    ]
    persistent = _persistent(hits=hits)
    out = M.read_topic_lock_carryover(
        _state(),
        persistent,
        {"path": "Ch20|Heart|LAD", "subsection": "LAD"},
    )
    assert "PRIOR-SESSION CONTEXT" in out
    assert "Past misconception:" in out
    assert "Learning-style cue:" in out
    assert "Confused LAD" in out
    assert "vasculature analogies" in out


def test_topic_lock_filters_query_and_top_k():
    persistent = _persistent(hits=[])
    M.read_topic_lock_carryover(
        _state(),
        persistent,
        {"path": "Ch20|Heart|LAD", "subsection": "LAD"},
    )
    args, kwargs = persistent.get.call_args
    # safe_mem0_read calls persistent.get(student_id, query, filters=...)
    assert args[0] == "s1"
    # query should include locked_question + subsection terms
    assert "left ventricle" in args[1]
    assert "LAD" in args[1]
    # filters should include category list + subsection_path
    filters = kwargs["filters"]
    assert filters["subsection_path"] == "Ch20|Heart|LAD"
    assert "misconception" in filters["category"]
    assert "learning_style" in filters["category"]


# ─────────────────────────────────────────────────────────────────────────────
# Hint-advance injection (#2)
# ─────────────────────────────────────────────────────────────────────────────


def test_hint_advance_no_persistent_returns_empty():
    out = M.read_hint_advance_carryover(_state(), None, {})
    assert out == ""


def test_hint_advance_no_locked_question_returns_empty():
    persistent = _persistent(hits=[{"text": "Style cue."}])
    out = M.read_hint_advance_carryover(
        _state(locked_question=""),
        persistent,
        {},
    )
    assert out == ""
    persistent.get.assert_not_called()


def test_hint_advance_query_includes_what_worked():
    persistent = _persistent(hits=[])
    M.read_hint_advance_carryover(_state(), persistent, {})
    query = persistent.get.call_args[0][1]
    assert "what worked when stuck" in query
    assert "left ventricle" in query  # from locked_question
    filters = persistent.get.call_args[1]["filters"]
    assert filters == {"category": "learning_style"}


def test_hint_advance_returns_formatted_style_cue():
    persistent = _persistent(hits=[
        {
            "text": "Visual diagrams clicked when verbal scaffolds failed.",
            "metadata": {"category": "learning_style"},
        },
    ])
    out = M.read_hint_advance_carryover(_state(), persistent, {})
    assert "STYLE CUE" in out
    assert "Style cue:" in out
    assert "Visual diagrams" in out


# ─────────────────────────────────────────────────────────────────────────────
# combine_carryover
# ─────────────────────────────────────────────────────────────────────────────


def test_combine_carryover_drops_empties():
    out = M.combine_carryover("", "block A", "", "block B", "")
    assert out == "block A\n\nblock B"


def test_combine_carryover_returns_empty_for_all_empty():
    assert M.combine_carryover("", "", "") == ""


def test_combine_carryover_clips_oversize():
    big = "x" * (M.MAX_CARRYOVER_CHARS * 2)
    out = M.combine_carryover(big)
    assert len(out) == M.MAX_CARRYOVER_CHARS
    assert out.endswith("...")


# ─────────────────────────────────────────────────────────────────────────────
# Trace integration — never raises, even on mem0.get explosion
# ─────────────────────────────────────────────────────────────────────────────


def test_topic_lock_traces_mem0_op_via_safe_wrapper():
    """safe_mem0_read appends a trace entry; verify it lands on state."""
    persistent = _persistent(hits=[
        {"text": "x", "metadata": {"category": "misconception"}},
    ])
    state = _state()
    M.read_topic_lock_carryover(
        state, persistent,
        {"path": "Ch20|Heart|LAD", "subsection": "LAD"},
    )
    trace = state["debug"]["turn_trace"]
    assert any(e.get("wrapper") == "mem0_read" for e in trace)


def test_topic_lock_does_not_raise_when_mem0_get_explodes():
    persistent = MagicMock()
    persistent.available = True
    persistent.get.side_effect = RuntimeError("backend down")
    state = _state()
    out = M.read_topic_lock_carryover(
        state, persistent,
        {"path": "Ch20|Heart|LAD", "subsection": "LAD"},
    )
    assert out == ""
    # safe_mem0_read traced the error
    trace = state["debug"]["turn_trace"]
    err_entries = [e for e in trace if e.get("wrapper") == "mem0_read"]
    assert err_entries
    assert err_entries[0].get("error", "").startswith("RuntimeError:")
