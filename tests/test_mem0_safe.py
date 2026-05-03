"""
tests/test_mem0_safe.py
───────────────────────
Unit tests for memory/mem0_safe.py (L5 — safe wrappers + tracing).

Coverage:
  * safe_mem0_read returns [] on infra failure (never raises)
  * Trace entry emitted on every read with hit_count + elapsed_ms + error
  * Empty results vs infra failure are distinguishable in trace
  * Dedupe by thread_id (default ON per L5)
  * top_k cap honored
  * safe_mem0_write returns False + traces on missing required metadata
  * safe_mem0_write returns False on stub-unavailable (no exception)
  * emit_session_summary_trace appends rollup entry
  * REQUIRED_WRITE_METADATA matches L4 spec exactly
"""
from __future__ import annotations

from typing import Any

import pytest

from memory.mem0_safe import (
    REQUIRED_WRITE_METADATA,
    emit_session_summary_trace,
    safe_mem0_read,
    safe_mem0_write,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fakes for PersistentMemory
# ─────────────────────────────────────────────────────────────────────────────

class FakeAvailable:
    """Available PM. Returns whatever rows the test seeds."""
    def __init__(self, rows: list[dict] | None = None, raise_on_get: Exception | None = None,
                 raise_on_add: Exception | None = None, add_succeeds: bool = True):
        self.available = True
        self._rows = rows or []
        self._raise_on_get = raise_on_get
        self._raise_on_add = raise_on_add
        self._add_succeeds = add_succeeds
        self.add_calls: list[dict] = []

    def get(self, student_id: str, query: str, filters: dict | None = None):
        if self._raise_on_get:
            raise self._raise_on_get
        return list(self._rows)

    def add(self, student_id: str, text: str, metadata: dict | None = None):
        if self._raise_on_add:
            raise self._raise_on_add
        self.add_calls.append({"student_id": student_id, "text": text, "metadata": metadata})
        return self._add_succeeds


class FakeUnavailable:
    available = False


# ─────────────────────────────────────────────────────────────────────────────
# Required metadata spec (L4 + Codex round-1 fix #4)
# ─────────────────────────────────────────────────────────────────────────────

def test_required_metadata_matches_l4_spec():
    assert REQUIRED_WRITE_METADATA == {
        "category", "subsection_path", "section_path", "session_at", "thread_id"
    }


# ─────────────────────────────────────────────────────────────────────────────
# safe_mem0_read
# ─────────────────────────────────────────────────────────────────────────────

def test_read_returns_empty_when_pm_unavailable_and_traces():
    state = {"debug": {"turn_trace": []}}
    out = safe_mem0_read(FakeUnavailable(), "alice", "x", state=state)
    assert out == []
    trace = state["debug"]["turn_trace"]
    assert trace and trace[0]["wrapper"] == "mem0_read"
    assert trace[0]["error"] == "stub_unavailable"
    assert trace[0]["hit_count"] == 0


def test_read_returns_results_and_traces_success():
    rows = [
        {"text": "claim 1", "metadata": {"thread_id": "t1"}},
        {"text": "claim 2", "metadata": {"thread_id": "t2"}},
    ]
    state = {"debug": {"turn_trace": []}}
    out = safe_mem0_read(FakeAvailable(rows), "alice", "x", state=state)
    assert len(out) == 2
    trace = state["debug"]["turn_trace"][0]
    assert trace["error"] is None and trace["hit_count"] == 2


def test_read_distinguishes_zero_hits_from_failure():
    state_empty = {"debug": {"turn_trace": []}}
    state_fail = {"debug": {"turn_trace": []}}

    safe_mem0_read(FakeAvailable([]), "alice", "x", state=state_empty)
    safe_mem0_read(FakeAvailable(raise_on_get=RuntimeError("connection lost")),
                   "alice", "x", state=state_fail)

    e_trace = state_empty["debug"]["turn_trace"][0]
    f_trace = state_fail["debug"]["turn_trace"][0]
    assert e_trace["hit_count"] == 0 and e_trace["error"] is None
    assert f_trace["hit_count"] == 0 and "RuntimeError" in (f_trace["error"] or "")


def test_read_deduplicates_by_thread_id_by_default():
    rows = [
        {"text": "claim A1", "metadata": {"thread_id": "t1"}},
        {"text": "claim A2", "metadata": {"thread_id": "t1"}},  # dup thread
        {"text": "claim B1", "metadata": {"thread_id": "t2"}},
    ]
    out = safe_mem0_read(FakeAvailable(rows), "alice", "x")
    threads = [h["metadata"]["thread_id"] for h in out]
    assert threads == ["t1", "t2"]


def test_read_dedup_off_returns_all_rows():
    rows = [
        {"text": "claim A1", "metadata": {"thread_id": "t1"}},
        {"text": "claim A2", "metadata": {"thread_id": "t1"}},
    ]
    out = safe_mem0_read(FakeAvailable(rows), "alice", "x", dedupe_by_thread_id=False)
    assert len(out) == 2


def test_read_top_k_cap():
    rows = [{"text": str(i), "metadata": {"thread_id": f"t{i}"}} for i in range(10)]
    out = safe_mem0_read(FakeAvailable(rows), "alice", "x", top_k=3)
    assert len(out) == 3


def test_read_no_state_does_not_crash():
    """Trace skipping when state is None should not raise."""
    out = safe_mem0_read(FakeAvailable([]), "alice", "x", state=None)
    assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# safe_mem0_write
# ─────────────────────────────────────────────────────────────────────────────

VALID_METADATA = {
    "category": "misconception",
    "subsection_path": "Anatomy > Sec > Sub",
    "section_path": "Anatomy > Sec",
    "session_at": "2026-05-02T12:00:00Z",
    "thread_id": "alice_abcdef",
}


def test_write_succeeds_with_valid_metadata():
    pm = FakeAvailable()
    state = {"debug": {"turn_trace": []}}
    ok = safe_mem0_write(pm, "alice", "Student confused X with Y.", VALID_METADATA, state=state)
    assert ok is True
    assert pm.add_calls and pm.add_calls[0]["text"] == "Student confused X with Y."
    trace = state["debug"]["turn_trace"][0]
    assert trace["wrapper"] == "mem0_write" and trace["success"] is True


@pytest.mark.parametrize("missing_field", sorted(REQUIRED_WRITE_METADATA))
def test_write_rejects_missing_required_field(missing_field):
    pm = FakeAvailable()
    md = {**VALID_METADATA}
    md.pop(missing_field)
    state = {"debug": {"turn_trace": []}}
    ok = safe_mem0_write(pm, "alice", "x", md, state=state)
    assert ok is False
    assert not pm.add_calls  # never reaches PM
    trace = state["debug"]["turn_trace"][0]
    assert trace["error"] == "missing_required_field"
    assert trace["dropped_field"] == missing_field


def test_write_rejects_empty_string_required_field():
    pm = FakeAvailable()
    md = {**VALID_METADATA, "subsection_path": "  "}  # whitespace only
    state = {"debug": {"turn_trace": []}}
    ok = safe_mem0_write(pm, "alice", "x", md, state=state)
    assert ok is False
    assert state["debug"]["turn_trace"][0]["dropped_field"] == "subsection_path"


def test_write_returns_false_on_pm_unavailable():
    state = {"debug": {"turn_trace": []}}
    ok = safe_mem0_write(FakeUnavailable(), "alice", "x", VALID_METADATA, state=state)
    assert ok is False
    assert state["debug"]["turn_trace"][0]["error"] == "stub_unavailable"


def test_write_returns_false_on_pm_exception():
    pm = FakeAvailable(raise_on_add=ConnectionError("Qdrant down"))
    state = {"debug": {"turn_trace": []}}
    ok = safe_mem0_write(pm, "alice", "x", VALID_METADATA, state=state)
    assert ok is False
    assert "ConnectionError" in (state["debug"]["turn_trace"][0]["error"] or "")


def test_write_no_state_does_not_crash():
    pm = FakeAvailable()
    ok = safe_mem0_write(pm, "alice", "x", VALID_METADATA, state=None)
    assert ok is True


# ─────────────────────────────────────────────────────────────────────────────
# emit_session_summary_trace
# ─────────────────────────────────────────────────────────────────────────────

def test_session_summary_trace_format():
    state = {"debug": {"turn_trace": []}}
    emit_session_summary_trace(
        state, reads_ok=3, reads_failed=0, writes_ok=2, writes_failed=1,
        writes_dropped_missing_fields=0,
    )
    entry = state["debug"]["turn_trace"][0]
    assert entry["wrapper"] == "memory.session_summary"
    assert entry["reads_ok"] == 3
    assert entry["writes_ok"] == 2
    assert entry["writes_failed"] == 1
