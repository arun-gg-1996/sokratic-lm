"""
memory/mastery_store.py
------------------------
Per-concept knowledge tracing — D.3.

Tracks numeric mastery per (student_id, subsection_path) tuple. The
store is updated at the end of every session (one entry moves —
the locked subsection's score is blended with the prior via EWMA)
and read at the start of every session (rapport opener + topic
suggester) plus on-demand for the /mastery dashboard.

Why a separate store from mem0
------------------------------
mem0 carries narrative facts ("student confused musculocutaneous with
median nerve") with metadata tags. Aggregating numeric scores out of
free-text fragments is fragile, slow, and doesn't compose well — by
the time you've LLM-parsed all 20+ fact strings to recover a single
mastery number, you've spent more compute and got less reliability
than just storing the number directly. The two stores complement:

  mem0           → narrative ("why was this hard?")
  MasteryStore   → quantitative ("how strong is the student here?")

The /mastery page reads BOTH: numeric mastery from this store, and
the narrative reason from mem0 (filtered to the same subsection_path).

Storage shape
-------------
One JSON file per student at `data/student_state/{student_id}.json`:

  {
    "concepts": {
      "Ch13|Anatomy of the Nervous System|Brachial Plexus": {
        "mastery": 0.62,
        "sessions": 3,
        "last_seen": "2026-04-29",
        "last_outcome": "reached"
      },
      ...
    }
  }

Atomic writes via tmp + os.replace so a crash mid-flush can't corrupt.
For thesis scale (~10 students × ~50 sessions each, dozens of unique
subsections per student) one JSON file per student is well-sized; if
the corpus grows we can swap the backend to SQLite without changing
the public API.

Heuristic vs LLM-based session scoring
--------------------------------------
score_session() is a deterministic function of (outcome, hints, turns).
The interface is intentionally simple so a future LLM-backed scorer
can be A/B compared against this baseline. For now, the heuristic
maps:
   reached  + 0/1/2/3 hints  → 0.95 / 0.80 / 0.65 / 0.50
   not_reached + few/many turns → 0.20 / 0.35
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Optional

# Single source of truth for the storage directory. Tests can monkeypatch
# this; production reads it once at import time.
_STORE_DIR = (
    Path(__file__).parent.parent / "data" / "student_state"
)


def _ensure_dir() -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)


def _student_path(student_id: str) -> Path:
    # student_id is validated by the API layer (backend/api/users.py).
    # Defensive: also strip any path separators in case a bypass happens.
    safe = student_id.replace("/", "_").replace("\\", "_").strip()
    return _STORE_DIR / f"{safe}.json"


# Module-level lock guarding atomic read-modify-write under concurrent
# session_end events. Cheap (per-process) — sufficient for the thesis
# scale where the FastAPI process is single-threaded for the LangGraph
# graph but ainvoke runs nodes on a thread pool. If we ever shard
# students across multiple processes this needs to become a per-file
# lock; not a concern today.
_LOCK = RLock()


def _heuristic_session_score(
    *,
    outcome: str,
    hints: int,
    turns: int,
) -> float:
    """Deterministic 0.0-1.0 score from session outcome metrics.

    See module docstring for the mapping. Bounds-clamped on input
    so a malformed state can't produce a negative or >1 score.
    """
    h = max(0, min(3, int(hints or 0)))
    t = max(0, int(turns or 0))
    if outcome == "reached":
        return [0.95, 0.80, 0.65, 0.50][h]
    # not_reached
    # "few turns" = the student abandoned early — less signal of mastery
    # OR signal of confusion. Treat both as low-but-not-floor.
    # "many turns" = sustained engagement, did not reach — slightly
    # higher because effort was made.
    return 0.35 if t >= 6 else 0.20


def score_session(state: dict) -> float:
    """Public entry point. State fields used:
        student_reached_answer, hint_level, turn_count.

    Falls back to 0.20 on missing fields (treats as a low-signal
    not-reached session)."""
    outcome = "reached" if state.get("student_reached_answer") else "not_reached"
    return _heuristic_session_score(
        outcome=outcome,
        hints=int(state.get("hint_level", 0) or 0),
        turns=int(state.get("turn_count", 0) or 0),
    )


class MasteryStore:
    """File-backed per-student mastery tracker.

    Public API:
      load(student_id)        -> dict[path, ConceptRecord]
      get(student_id, path)   -> ConceptRecord | None
      update(student_id, path, score, outcome)
      weak_subsections(student_id, threshold=0.5, limit=3) -> list
      delete_student(student_id) -> int   # for "forget me"
    """

    EWMA_NEW = 0.6   # weight on new session's score
    EWMA_OLD = 0.4   # weight on prior stored mastery

    def __init__(self) -> None:
        _ensure_dir()

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------
    def load(self, student_id: str) -> dict:
        """Return the full concepts dict for a student.

        Returns {} for a student with no prior data (file doesn't exist).
        Returns {} on parse error too — better to treat a corrupted file
        as "no data" than crash a session start.
        """
        with _LOCK:
            path = _student_path(student_id)
            if not path.exists():
                return {}
            try:
                data = json.loads(path.read_text())
            except Exception:
                return {}
            concepts = data.get("concepts") if isinstance(data, dict) else None
            return concepts if isinstance(concepts, dict) else {}

    def get(self, student_id: str, subsection_path: str) -> Optional[dict]:
        """Return the record for one subsection, or None if not seen yet."""
        return self.load(student_id).get(subsection_path)

    def weak_subsections(
        self,
        student_id: str,
        threshold: float = 0.5,
        limit: int = 3,
    ) -> list[dict]:
        """Return up to `limit` subsections with mastery < threshold,
        sorted ascending by mastery (weakest first).

        Each entry is a dict with keys:
          path, mastery, sessions, last_seen, last_outcome,
          chapter_num, chapter_title, section_title, subsection_title

        chapter/section/subsection are parsed from the path
        ('ChN|section|subsection') — store callers don't need to look
        these up separately.
        """
        rows: list[dict] = []
        for path, rec in self.load(student_id).items():
            if not isinstance(rec, dict):
                continue
            mastery = float(rec.get("mastery", 1.0))
            if mastery >= threshold:
                continue
            chapter_num, chapter_title, section_title, subsection_title = (
                _parse_path(path)
            )
            rows.append({
                "path": path,
                "mastery": mastery,
                "sessions": int(rec.get("sessions", 0) or 0),
                "last_seen": rec.get("last_seen", ""),
                "last_outcome": rec.get("last_outcome", ""),
                "chapter_num": chapter_num,
                "chapter_title": chapter_title,
                "section_title": section_title,
                "subsection_title": subsection_title,
            })
        rows.sort(key=lambda r: r["mastery"])
        return rows[:limit]

    def stats(self, student_id: str) -> dict:
        """Aggregate counters for the /mastery dashboard's header.

        Returns:
            {
              "touched": int,            # subsections with at least one session
              "mastered": int,           # mastery >= 0.8
              "avg_mastery": float,      # mean across touched subsections, 0-1
            }
        """
        concepts = self.load(student_id)
        if not concepts:
            return {"touched": 0, "mastered": 0, "avg_mastery": 0.0}
        masteries = [
            float(rec.get("mastery", 0.0))
            for rec in concepts.values()
            if isinstance(rec, dict)
        ]
        if not masteries:
            return {"touched": 0, "mastered": 0, "avg_mastery": 0.0}
        return {
            "touched": len(masteries),
            "mastered": sum(1 for m in masteries if m >= 0.8),
            "avg_mastery": round(sum(masteries) / len(masteries), 3),
        }

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------
    def update(
        self,
        student_id: str,
        subsection_path: str,
        session_score: float,
        outcome: str,
        session_date: Optional[str] = None,
    ) -> dict:
        """Apply an EWMA update to a subsection's mastery and persist.

        If the subsection has no prior record, the EWMA collapses to
        just the session score (prior=0 doesn't make sense — first
        observation is the initial belief). We do this by skipping the
        blend when sessions=0.

        Args:
            student_id:        validated student id
            subsection_path:   "ChN|section|subsection" — the path stored
                               by dean.py on state['locked_topic'].path
            session_score:     0.0-1.0 from score_session(state)
            outcome:           "reached" | "not_reached"
            session_date:      ISO date string; defaults to today

        Returns:
            The updated record dict (also persisted to disk).
        """
        if not subsection_path or "|" not in subsection_path:
            # Sessions that never locked a topic produce no mastery update.
            return {}
        if session_date is None:
            session_date = datetime.now().strftime("%Y-%m-%d")

        with _LOCK:
            path_file = _student_path(student_id)
            if path_file.exists():
                try:
                    data = json.loads(path_file.read_text())
                    if not isinstance(data, dict):
                        data = {"concepts": {}}
                except Exception:
                    data = {"concepts": {}}
            else:
                data = {"concepts": {}}
            concepts = data.setdefault("concepts", {})

            prior = concepts.get(subsection_path)
            if isinstance(prior, dict) and prior.get("sessions"):
                blended = (
                    self.EWMA_NEW * float(session_score)
                    + self.EWMA_OLD * float(prior.get("mastery", 0.0))
                )
                sessions = int(prior.get("sessions", 0)) + 1
            else:
                blended = float(session_score)
                sessions = 1

            # Clamp + round to 3 decimals — cosmetic for the dashboard.
            blended = max(0.0, min(1.0, blended))
            record = {
                "mastery": round(blended, 3),
                "sessions": sessions,
                "last_seen": session_date,
                "last_outcome": outcome,
            }
            concepts[subsection_path] = record

            self._atomic_write(path_file, data)
            return record

    def delete_student(self, student_id: str) -> int:
        """Remove the student's mastery file. Used by the "forget me"
        action so a per-student wipe affects ALL stores (mem0 +
        mastery), not just one.

        Returns:
            1 if a file was deleted, 0 if there was nothing to delete.
        """
        with _LOCK:
            path = _student_path(student_id)
            if path.exists():
                path.unlink()
                return 1
            return 0

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        # Tempfile in the same dir → os.replace is atomic on POSIX.
        # If write or replace fails the existing file is untouched.
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_name = tmp.name
        os.replace(tmp_name, path)


def _parse_path(path: str) -> tuple[int, str, str, str]:
    """Parse a topic path 'Ch20|Circulatory Pathways|Overview of Systemic Veins'
    into (chapter_num, chapter_title, section_title, subsection_title).

    chapter_title is empty here because the path stores chapter as a
    number prefix only — the human-readable title lives on
    state['locked_topic']['chapter']. Callers that need the title
    should pass it through directly (the API layer does this).
    """
    parts = (path or "").split("|", 2)
    chapter_num = 0
    chapter_title = ""
    section_title = parts[1] if len(parts) >= 2 else ""
    subsection_title = parts[2] if len(parts) >= 3 else ""
    head = parts[0] if parts else ""
    if head.startswith("Ch") and head[2:].isdigit():
        chapter_num = int(head[2:])
    return chapter_num, chapter_title, section_title, subsection_title
