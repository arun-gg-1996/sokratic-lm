"""
memory/persistent_memory.py
SQL-backed cross-session student memory.

Replaces the previous mem0/Qdrant-backed implementation. Same public
surface — `available`, `get(...)`, `add(...)`, `delete_user(...)` —
so callers (`safe_mem0_read`, `safe_mem0_write`, `MemoryManager`) keep
working without modification.

What's stored:
    Narrative observations about each student, in two categories:
      * misconception   — factual errors observed in a session
      * learning_style  — interaction patterns (hedging, hint-reliance, etc.)
    Each row is one atomic sentence, written at session-end by
    `memory.observation_extractor.extract_observations`. Persisted to the
    `observations` table in the per-domain SQLite file.

Why SQL instead of mem0:
    * Reads are filter-by-(student_id, category, subsection_path) — exact
      match queries SQL serves natively. We weren't using mem0's semantic
      ranking meaningfully at our scale (5–15 atoms per student).
    * Writes don't need mem0's ADD/UPDATE/DELETE/NOOP arbitration. That
      LLM-driven decision was opaque, occasionally clobbered distinct
      misconceptions, and we never queried its outcome.
    * Deduplication via UNIQUE(student_id, hash) constraint — same effect
      as mem0's hash dedup, no second-LLM-call required.
    * Clean foreign keys: thread_id → sessions, student_id → students.

Response shape preserved: `get()` returns dicts with mem0-compatible keys
(`memory`, `data`, `text`, `metadata`, `created_at`, `id`) so
`MemoryDrawer.tsx` and `/api/memory/{student_id}` continue to render
without frontend changes.
"""
from __future__ import annotations

import uuid
from typing import Any

from memory.sqlite_store import SQLiteStore


class PersistentMemory:
    """SQL-backed observation store. Always available locally."""

    def __init__(self) -> None:
        # Open the per-domain SQLite store. The store auto-applies migrations
        # (including 002_observations.sql) on first use, so no separate setup.
        self._store = SQLiteStore()
        # Flagged True so callers' `if not persistent.available: return []`
        # short-circuits don't hide our reads. SQL is local — there's no
        # network failure mode to model. If the DB file is unreachable the
        # underlying calls raise and `safe_mem0_*` catches them.
        self.available = True
        self.unavailable_reason = ""
        # Kept for API compatibility — historically callers (clear_namespace)
        # peeked at a `client` attribute. Nothing reads it beyond the legacy
        # `MemoryManager.clear_namespace` path which we've simplified.
        self.client = self._store
        # Namespace kept for API compatibility (unused now — student_id is
        # the only key we need; SQL doesn't need the "anatomy:" prefix).
        self.namespace = "default"

    # ── Reads ──────────────────────────────────────────────────────────────

    def get(
        self,
        student_id: str,
        query: str = "",
        filters: dict | None = None,
    ) -> list[dict]:
        """Fetch observations for a student.

  Args:
      student_id: Unique student identifier (no namespace prefix needed).
      query: Free-text query string. Ignored — SQL filtering is exact
          on category + subsection_path. The `query` slot is kept for API
          compatibility with mem0; semantic ranking is not implemented
          because the scale (5–15 atoms/student) doesn't benefit from it.
      filters: Optional dict with any of:
          * "category": str or list[str] — restrict to those categories
          * "subsection_path": str — exact match (with prefix tolerance)

  Returns:
      List of dicts shaped to match mem0's response so existing callers
      and the frontend keep working unchanged. Empty list on error.
  """
        try:
            cat = (filters or {}).get("category")
            sub = (filters or {}).get("subsection_path")
            rows = self._store.list_observations(
                student_id,
                category=cat,
                subsection_path=sub,
                limit=50,
            )
        except Exception:
            return []

        out: list[dict] = []
        for r in rows:
            md = {
                "category": r.get("category"),
                "subsection_path": r.get("subsection_path"),
                "section_path": r.get("section_path"),
                "session_at": r.get("session_at"),
                "thread_id": r.get("thread_id"),
                "chapter_num": r.get("chapter_num"),
            }
            text = r.get("text") or ""
            out.append({
                "id": r.get("observation_id"),
                # mem0's response used both "memory" and "data" depending
                # on version — we expose both so legacy paths don't break.
                "memory": text,
                "data": text,
                "text": text,
                "score": None,
                "created_at": r.get("created_at"),
                "metadata": md,
            })
        return out

    # ── Writes ─────────────────────────────────────────────────────────────

    def add(
        self,
        student_id: str,
        memory_text: str,
        metadata: dict | None = None,
    ) -> bool:
        """Insert one observation.

  Maps the mem0-style call into the typed `observations` table. The
  metadata dict comes from `MemoryManager._topic_metadata` plus the
  category appended by `MemoryManager.flush`.

  Returns True if the row landed; False on dedup-skip or error.
  """
        if not memory_text or not memory_text.strip():
            return False
        md = metadata or {}
        # Post-migration 003: the observations table FKs to subsections by
        # subsection_id; section_path / chapter_num are derivable via JOIN
        # and no longer accepted by add_observation. We forward only the
        # supported kwargs.
        #
        # Errors propagate up to safe_mem0_write where they're recorded in
        # the turn_trace with the exception class + message. (Previous
        # implementation caught Exception here and returned False, which is
        # how a TypeError on a removed kwarg silently killed every narrative
        # memory write for an entire backend session without surfacing in
        # any log.)
        return self._store.add_observation(
            observation_id=str(uuid.uuid4()),
            student_id=student_id,
            thread_id=md.get("thread_id") or None,
            category=str(md.get("category") or "uncategorized"),
            subsection_path=md.get("subsection_path") or None,
            text=memory_text.strip(),
            evidence=md.get("evidence") or None,
            session_at=md.get("session_at") or None,
        )

    # ── Per-user delete (forget-me) ────────────────────────────────────────

    def delete_user(self, student_id: str) -> int:
        """Delete every observation for one student.

  Used by `MemoryManager.forget` for the privacy / forget-me flow.
  Returns the count of rows deleted, -1 on error.
  """
        try:
            return self._store.delete_observations(student_id)
        except Exception:
            return -1
