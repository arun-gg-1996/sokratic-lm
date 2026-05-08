"""
Orchestrator for cross-session memory.

mem0 carries two narrative categories about a student:

  * misconception   — observed factual errors / persistent confusions
  * learning_style  — interaction patterns (hedging, terseness,
                      hint-reliance, exploration tendency)

Both are extracted by a single Haiku call at session end
(`memory.observation_extractor.extract_observations`). All other
session metadata (per-session summary, open threads, topics covered)
lives in SQLite — see `memory/sqlite_store.py`.

Every mem0 read and write routes through `memory/mem0_safe.py`,
which never raises, validates required metadata, dedupes by thread,
and emits per-op trace entries on `state["debug"]["turn_trace"]`.

mem0 is namespaced by `(cfg.domain.mem0_namespace, student_id)` so
each domain and each student has an isolated memory store.
"""
from __future__ import annotations

from typing import Optional

import re  # kept for legacy clear_namespace path

from conversation.state import TutorState

# (_CORRECTION_TRACE_MARKERS) and learning-style detection (_HEDGE_MARKERS)
# have been removed. All observation extraction is now performed by a
# single Haiku call at session end via memory.observation_extractor.
# This honors the "LLM-only behavioral judgment, no heuristics" directive.

class MemoryManager:
    """Real implementation backed by `PersistentMemory` (mem0 + Qdrant).

 Falls back to no-op behavior if mem0 or Qdrant is unavailable, so the
 conversation graph never crashes on a memory failure — it just runs
 without persistence (matches the previous stubbed contract for callers).
"""

    def __init__(self):
        # Lazy import so test rigs that don't need persistence don't pay
        # the mem0 init cost.
        from memory.persistent_memory import PersistentMemory
        self.persistent = PersistentMemory()
        self.last_flush_status = "ready" if self.persistent.available else "stub_unavailable"

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------
    def load(
        self,
        student_id: str,
        query: str = "",
        filters: Optional[dict] = None,
        *,
        state: Optional[dict] = None,
        top_k: int = 5,
    ) -> list[dict]:
        """safe read wrapper. Never raises.

 Routes through `safe_mem0_read` so every read emits a trace entry
 and results are deduped by `thread_id`. Returns empty list on infra
 failure (distinguishable from "no hits" via the trace entry's
 `error` field).

 Args:
 student_id: unique student identifier.
 query: semantic search query. Empty returns recent.
 filters: metadata filter dict. Examples:
 {"category": "misconception"}
 {"category": "learning_style", "subsection_path": "X > Y > Z"}
 NOTE: post- mem0 ONLY carries category in
 {misconception, learning_style}. session_summary /
 topics_covered / open_thread are now SQL-side
 (read via SQLiteStore.list_sessions / mastery_tree).
 state: optional TutorState — when provided, the read trace
 is appended to state.debug.turn_trace .
 top_k: cap on returned hits (default 5).
"""
        from memory.mem0_safe import safe_mem0_read
        return safe_mem0_read(
            self.persistent, student_id, query, filters,
            top_k=top_k, state=state,
        )

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------
    def flush(
        self,
        student_id: str,
        state: TutorState,
        summary_text: str = "",
    ) -> bool:
        """ + implementation: extract observations via single Haiku call
 persist each as a separate mem0 entry with required metadata.

 After this commit mem0 only carries:
 * misconception — observed factual errors / confusions
 * learning_style — observable interaction patterns

 session_summary / open_thread / topics_covered are now in SQL
written by the SQLite hooks in nodes.memory_update_node.

 `summary_text` is kept on the signature for back-compat but is
 no longer consumed (the extractor builds its own context from
 state). Callers that pass it: harmless.

 Returns True if at least one observation was persisted.
"""
        from memory.mem0_safe import safe_mem0_write, emit_session_summary_trace
        from memory.observation_extractor import extract_observations

        if not self.persistent.available:
            self.last_flush_status = "stub_unavailable"
            emit_session_summary_trace(
                state, reads_ok=0, reads_failed=0,
                writes_ok=0, writes_failed=0, writes_dropped_missing_fields=0,
            )
            return False

        # Skip extraction for sessions with no real interaction
        # (rapport-only / pre-lock-terminated). Saves a Haiku call when
        # there's nothing to observe.
        student_msg_count = sum(
            1 for m in (state.get("messages") or []) if m.get("role") == "student"
        )
        if student_msg_count < 2:
            self.last_flush_status = "skipped_too_short"
            emit_session_summary_trace(
                state, reads_ok=0, reads_failed=0,
                writes_ok=0, writes_failed=0, writes_dropped_missing_fields=0,
            )
            return False

        # Extract observations via Haiku
        try:
            from conversation.llm_client import make_anthropic_client, resolve_model
            from config import cfg as _cfg
            client = make_anthropic_client()
            model = resolve_model(_cfg.models.summarizer)  # Haiku tier
        except Exception as e:
            self.last_flush_status = f"client_init_error: {type(e).__name__}: {str(e)[:80]}"
            return False

        observations = extract_observations(state, client=client, model=model)

        # Common metadata for every write — extractor produces 1 sentence
        # per claim with topic baked in (write-style rule), so we
        # only need session-level metadata here.
        topic_meta = self._topic_metadata(state)

        writes_ok = 0
        writes_failed = 0
        writes_dropped = 0

        for obs in observations:
            metadata = {**topic_meta, "category": obs.category}
            success = safe_mem0_write(
                self.persistent, student_id, obs.text, metadata, state=state,
            )
            if success:
                writes_ok += 1
            else:
                # Distinguish dropped-on-validation from infra failures by
                # peeking the latest trace entry the wrapper just appended.
                last = (state.get("debug", {}).get("turn_trace") or [{}])[-1]
                if last.get("dropped_field"):
                    writes_dropped += 1
                else:
                    writes_failed += 1

        emit_session_summary_trace(
            state,
            reads_ok=0,  # this method doesn't read; reads happen in nodes
            reads_failed=0,
            writes_ok=writes_ok,
            writes_failed=writes_failed,
            writes_dropped_missing_fields=writes_dropped,
        )

        self.last_flush_status = (
            f"wrote_{writes_ok}_failed_{writes_failed}_"
            f"dropped_{writes_dropped}_of_{len(observations)}"
        )
        return writes_ok > 0

    # ------------------------------------------------------------------
    # CLEAR (for clean test runs)
    # ------------------------------------------------------------------
    def clear_namespace(self) -> int:
        """Wipe ALL observations across every student.

  Used by eval/measurement scripts before a baseline run. Now that
  observations live in SQL it's a single DELETE; no longer the
  Qdrant-collection-drop dance the mem0-backed version did.

  Returns the count of rows wiped, 0 on error.
  """
        try:
            from memory.sqlite_store import SQLiteStore
            store = SQLiteStore()
            conn = store._conn()
            cur = conn.execute("DELETE FROM observations")
            conn.commit()
            return cur.rowcount or 0
        except Exception:
            return 0

    def forget(self, student_id: str) -> int:
        """Delete a student's narrative memory AND mastery store.

 "Forget me" should wipe everything the system remembers about
 the student, not just one of two stores. We delete:
mem0 entries via PersistentMemory.delete_user
mastery scores via MasteryStore.delete_student
 Each is independent — a failure in one doesn't block the other.

 Returns:
 Number of mem0 memories deleted, or -1 if mem0 was
 unavailable. Mastery file deletion is separately reported
 via the trace but always best-effort.
"""
        # Mastery — local file, no external service. Try first, ignore
        # failure since "no file" is a normal case.
        try:
            from memory.mastery_store import MasteryStore
            MasteryStore().delete_student(student_id)
        except Exception:
            pass

        if not self.persistent.available:
            return -1
        return self.persistent.delete_user(student_id)

    # ------------------------------------------------------------------
    # Internal: memory string builders
    # ------------------------------------------------------------------
    @staticmethod
    def _topic_metadata(state: TutorState) -> dict:
        """Extract canonical metadata for mem0 writes ( round-1 fix #4).

 Required fields (validated by safe_mem0_write before persisting):
 subsection_path "<chapter> > <section> > <subsection>"
 section_path "<chapter> > <section>"
 session_at ISO-8601 UTC of the session-end moment
 thread_id from state["thread_id"] (stashed by start_session)
 Optional:
 chapter_num derivable from subsection_path

 `topic_path` (legacy "ChN|sec|sub") is NOT emitted — +
 the canonical format uses " > " separators with the full
 chapter title.

 Sticky-snapshot fallback: in some session flows (notably the
 clinical-pass close path), `state["locked_topic"]` ends up
 None at memory_update_node time even though a topic WAS locked
 earlier. Falls back to `state["debug"]["locked_topic_snapshot"]`
 which the dean writes at lock time + never overwrites.
"""
        from memory.sqlite_store import normalize_subsection_path, utc_now

        locked = state.get("locked_topic") or {}
        if not locked:
            locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}

        legacy_path = str(locked.get("path", "") or "")
        canonical_subsection_path = (
            normalize_subsection_path(legacy_path) if legacy_path else ""
        )

        # Derive section_path (drop the trailing "> subsection" segment)
        section_path = ""
        if " > " in canonical_subsection_path:
            section_path = canonical_subsection_path.rsplit(" > ", 1)[0]

        # chapter_num: parse from the canonical "Chapter N: ..." prefix
        # (current format). Fall back to the old "ChN|..." legacy form so
        # any locked_topic snapshots stamped pre-canonicalization still
        # populate the field.
        chapter_num = 0
        m = re.match(r"Chapter (\d+):", canonical_subsection_path)
        if m:
            chapter_num = int(m.group(1))
        else:
            m = re.match(r"Ch(\d+)\|", legacy_path)
            if m:
                chapter_num = int(m.group(1))

        return {
            "subsection_path": canonical_subsection_path,
            "section_path": section_path,
            "session_at": utc_now(),
            "thread_id": str(state.get("thread_id") or ""),
            "chapter_num": chapter_num,  # optional; provided when available
        }

