"""
Defensive read / write wrappers around mem0 + Qdrant.

  safe_mem0_read(student_id, query, filters, top_k)   -> list   # never raises
  safe_mem0_write(student_id, text, metadata)         -> bool   # never raises

Both wrappers append trace entries to `state["debug"]["turn_trace"]`
so every mem0 operation is visible in the session export. An empty
read is distinguishable from an infrastructure failure
(`hit_count=0, error=null` vs `error="..."`).

Writes require these metadata fields and reject anything missing
them: `category`, `subsection_path` (`Chapter > Section > Subsection`),
`section_path`, `session_at` (ISO-8601 UTC), and `thread_id`.
Retrieval results are deduped by `thread_id` because mem0's internal
atomization can split one input into multiple stored claims with
identical metadata.
"""
from __future__ import annotations

import time
from typing import Any, Optional

REQUIRED_WRITE_METADATA = {
    "category",
    "subsection_path",
    "section_path",
    "session_at",
    "thread_id",
}

def _trace_append(state: Optional[dict], entry: dict) -> None:
    """Append to state.debug.turn_trace if state is wired; otherwise drop.

 Tests pass state=None to skip tracing; production callers always pass
 the live TutorState dict.
"""
    if not state:
        return
    debug = state.setdefault("debug", {})
    trace = debug.setdefault("turn_trace", [])
    trace.append(entry)

def _validate_metadata(metadata: dict) -> tuple[bool, str]:
    """Return (ok, missing_field_name_or_empty).

 First missing field is reported. None-valued fields are also missing.
"""
    if not isinstance(metadata, dict):
        return False, "metadata_not_dict"
    for k in REQUIRED_WRITE_METADATA:
        v = metadata.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            return False, k
    return True, ""

def safe_mem0_read(
    persistent,
    student_id: str,
    query: str = "",
    filters: Optional[dict] = None,
    top_k: int = 5,
    *,
    state: Optional[dict] = None,
    dedupe_by_thread_id: bool = False,
) -> list[dict]:
    """Safe wrapper around PersistentMemory.get. Never raises.

  Emits a trace entry with op / query / filters / hit_count / elapsed_ms /
  error. If `dedupe_by_thread_id` is True, results are collapsed so one
  logical session contributes at most one item.

  History note: this dedup defaulted to True back when the store was
  mem0/Qdrant — mem0 atomized one observation into multiple stored points
  with identical metadata, so deduping by thread_id was the only way to
  recover the "one observation per session" view. With the SQL-backed
  store each row is one explicit observation, so the default is False
  and all atoms surface. Callers that still want session-level
  deduplication can opt in explicitly.

  `persistent` is a PersistentMemory instance — passed in to keep this
  module unaware of construction.
  """
    op = "mem0_read"
    t0 = time.time()
    error: Optional[str] = None
    raw_hits: list[dict] = []

    if not getattr(persistent, "available", False):
        elapsed_ms = int((time.time() - t0) * 1000)
        _trace_append(state, {
            "wrapper": op, "query": query[:120], "filters": filters,
            "hit_count": 0, "elapsed_ms": elapsed_ms,
            "error": "stub_unavailable",
        })
        return []

    try:
        raw_hits = persistent.get(student_id, query, filters=filters) or []
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:160]}"

    elapsed_ms = int((time.time() - t0) * 1000)

    out_hits = raw_hits
    if dedupe_by_thread_id and raw_hits:
        seen: set[str] = set()
        deduped: list[dict] = []
        for h in raw_hits:
            tid = ""
            md = h.get("metadata") if isinstance(h, dict) else None
            if isinstance(md, dict):
                tid = str(md.get("thread_id") or "")
            # Items without a thread_id pass through (legacy entries)
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            deduped.append(h)
        out_hits = deduped

    if top_k and len(out_hits) > top_k:
        out_hits = out_hits[:top_k]

    _trace_append(state, {
        "wrapper": op, "query": query[:120], "filters": filters,
        "hit_count": len(out_hits), "elapsed_ms": elapsed_ms,
        "error": error,
    })
    return out_hits

def safe_mem0_write(
    persistent,
    student_id: str,
    text: str,
    metadata: dict,
    *,
    state: Optional[dict] = None,
) -> bool:
    """Safe wrapper around PersistentMemory.add. Never raises.

 Validates required metadata fields . On missing fields, emits a
 "writes_dropped_missing_fields" trace entry and returns False — does
 NOT silently drop without surfacing.

 Emits a trace entry with op / text-prefix / metadata / elapsed_ms /
 error / dropped_field.
"""
    op = "mem0_write"
    t0 = time.time()

    ok, missing = _validate_metadata(metadata)
    if not ok:
        elapsed_ms = int((time.time() - t0) * 1000)
        _trace_append(state, {
            "wrapper": op, "text": (text or "")[:120],
            "metadata": metadata, "elapsed_ms": elapsed_ms,
            "error": "missing_required_field",
            "dropped_field": missing,
        })
        return False

    if not getattr(persistent, "available", False):
        elapsed_ms = int((time.time() - t0) * 1000)
        _trace_append(state, {
            "wrapper": op, "text": (text or "")[:120],
            "metadata": metadata, "elapsed_ms": elapsed_ms,
            "error": "stub_unavailable",
        })
        return False

    error: Optional[str] = None
    success = False
    try:
        success = bool(persistent.add(student_id, text, metadata=metadata))
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:160]}"

    elapsed_ms = int((time.time() - t0) * 1000)
    _trace_append(state, {
        "wrapper": op, "text": (text or "")[:120],
        "metadata": metadata, "elapsed_ms": elapsed_ms,
        "error": error, "success": success,
    })
    return success

def emit_session_summary_trace(
    state: dict,
    *,
    reads_ok: int,
    reads_failed: int,
    writes_ok: int,
    writes_failed: int,
    writes_dropped_missing_fields: int,
) -> None:
    """Per : at session end, emit a single rollup trace summarizing all
 mem0 ops for the session. Lives in turn_trace under wrapper key
 'memory.session_summary'."""
    _trace_append(state, {
        "wrapper": "memory.session_summary",
        "reads_ok": reads_ok,
        "reads_failed": reads_failed,
        "writes_ok": writes_ok,
        "writes_failed": writes_failed,
        "writes_dropped_missing_fields": writes_dropped_missing_fields,
    })
