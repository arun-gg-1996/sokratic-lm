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
        "confidence": 0.45,
        "sessions": 3,
        "last_seen": "2026-04-29",
        "last_outcome": "reached",
        "last_rationale": "Student demonstrated motor branch identification but ..."
      },
      ...
    }
  }

Atomic writes via tmp + os.replace so a crash mid-flush can't corrupt.
For thesis scale (~10 students × ~50 sessions each, dozens of unique
subsections per student) one JSON file per student is well-sized; if
the corpus grows we can swap the backend to SQLite without changing
the public API.

Two-signal mastery model — extension of BKT
--------------------------------------------
Classical Bayesian Knowledge Tracing (Corbett & Anderson 1995) collapses
uncertainty into a single posterior P(student has mastered skill k).
That works when each "skill" is small enough to be probed by a single
question. Our subsections (e.g. "Conduction System of the Heart")
contain MULTIPLE concepts that the textbook structure doesn't enumerate
as separate skills, and one tutoring session typically probes only one
or two of them via the anchor question. A perfect single-question
session under naive BKT lifts P(L) toward 0.95 — overconfident.

We extend BKT with a second signal:

  mastery     — point estimate of P(student has mastered the subsection),
                analogous to BKT's P(L)
  confidence  — coverage estimate: how thoroughly has this subsection
                been probed across sessions? Single-anchor session ≈ 0.20;
                3 sessions on different anchors ≈ 0.60.

Both are LLM-derived (see score_session_llm). EWMA-blended across
sessions (0.6 new / 0.4 prior). The "mastered" badge applies the
conjunction:

  mastered  iff  mastery >= 0.80 AND confidence >= 0.60

Thresholds chosen from modern adaptive tutoring practice (Khan Academy,
ASSISTments use 0.80 mastery; original BKT used 0.95). The 0.60
confidence floor is empirically calibrated — single-session perfect
answers stay below it; multi-session multi-concept exposure clears it.

The LLM scorer is the only path. If the Anthropic call fails after
retries, the session simply does not update mastery (logged in
turn_trace). No heuristic fallback — heuristics on outcome+hints+turns
cannot reason about subsection scope, which is the entire point of
the two-signal model.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from config import cfg

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


def _format_transcript(messages: list[dict], max_messages: int = 8) -> str:
    """Render the last N messages for the LLM scorer prompt. Trimmed
    so we don't blow the context budget on long sessions."""
    out: list[str] = []
    for m in (messages or [])[-max_messages:]:
        role = str(m.get("role", "?"))
        content = str(m.get("content", "")).strip()
        if content:
            out.append(f"{role}: {content[:600]}")
    return "\n".join(out) if out else "(no transcript available)"


def _format_prior_rationales(rationales: list[str]) -> str:
    """Format past rationales into a bullet list for the prompt."""
    if not rationales:
        return "(none — this is the first session on this subsection)"
    return "\n".join(f"  - {r.strip()}" for r in rationales if r and r.strip())


def _llm_call_with_retry(
    client: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_attempts: int = 3,
) -> str:
    """Call Anthropic with exponential backoff. Returns response text on
    success, raises the final exception on hard failure.

    No fallback is attempted by the caller — per design, mastery scoring
    skips updates entirely when the LLM is unavailable. Retries here
    are for transient network errors (rate limit, timeout) only."""
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=512,
                temperature=0.0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return (resp.content[0].text or "").strip()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                # Exponential backoff: 1s, 3s, then give up.
                _time.sleep(1.0 * (3 ** attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("LLM scorer: no response after retries")


def _extract_json_object(text: str) -> dict | None:
    """Pull the first valid {...} JSON object from a string.

    Tolerates Anthropic's tendency to wrap JSON in markdown code
    fences (```json ... ```) or trailing commentary even when told
    to return strict JSON. We don't try to parse the fences — we
    just locate the first '{' and matching '}' span and parse that.
    """
    if not text:
        return None
    # The simplest and most robust approach: scan for the first '{'
    # and the LAST '}' — the JSON object is always between them.
    # Code fences can wrap arbitrary characters around it; that's fine
    # because we ignore everything outside the brace span.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start : end + 1]
    try:
        return json.loads(blob)
    except Exception:
        return None


def score_session_llm(
    state: dict,
    *,
    prior_rationales: list[str],
    client: Any,
    model: str,
) -> Optional[dict]:
    """LLM-based session scorer.

    Returns a dict with keys (mastery, confidence, rationale) on success,
    or None if scoring failed (LLM unavailable, malformed JSON after
    retries). Caller treats None as "skip the update for this session."

    Why no heuristic fallback: heuristic scoring on outcome+hints+turns
    cannot reason about subsection scope vs what was probed — the
    central insight from Corbett & Anderson 1995 (BKT extensions) and
    the reason we adopted LLM-as-evaluator. A fallback would silently
    paper over what should be visible degradation.

    Args:
        state:            full TutorState dict
        prior_rationales: up to 3 most-recent rationales for this same
                          subsection from this student
        client:           Anthropic client instance
        model:            model id from cfg.models (the dean's model
                          works fine — same Sonnet tier)

    Returns:
        {mastery: float, confidence: float, rationale: str} OR None.
    """
    locked = state.get("locked_topic") or {}
    subsection = str(locked.get("subsection", "") or "")
    chapter = str(locked.get("chapter", "") or "")
    locked_q = str(state.get("locked_question", "") or "")
    locked_a = str(state.get("locked_answer", "") or "")
    outcome = "reached" if state.get("student_reached_answer") else "not_reached"
    transcript = _format_transcript(state.get("messages") or [])
    prior = _format_prior_rationales(prior_rationales)

    static_prompt = getattr(cfg.prompts, "mastery_scorer_static", "")
    dynamic_template = getattr(cfg.prompts, "mastery_scorer_dynamic", "")
    if not static_prompt or not dynamic_template:
        # Prompt config missing — surface this as a hard failure so it
        # gets noticed rather than silently downgrading.
        return None

    user_prompt = dynamic_template.format(
        subsection_title=subsection or "(unknown)",
        chapter_title=chapter or "(unknown)",
        outcome=outcome,
        turn_count=int(state.get("turn_count", 0) or 0),
        hint_level=int(state.get("hint_level", 0) or 0),
        max_hints=int(state.get("max_hints", 3) or 3),
        locked_question=locked_q or "(no anchor question recorded)",
        locked_answer=locked_a or "(no target answer recorded)",
        transcript=transcript,
        prior_rationales=prior,
    )

    try:
        raw = _llm_call_with_retry(client, model, static_prompt, user_prompt)
    except Exception:
        return None

    parsed = _extract_json_object(raw)
    if not isinstance(parsed, dict):
        return None

    try:
        mastery = float(parsed.get("mastery", -1))
        confidence = float(parsed.get("confidence", -1))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= mastery <= 1.0) or not (0.0 <= confidence <= 1.0):
        return None
    rationale = str(parsed.get("rationale", "") or "").strip()

    return {
        "mastery": mastery,
        "confidence": confidence,
        "rationale": rationale,
    }


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

    # "Mastered" badge thresholds. Single-rule (mastery >= 0.80 alone)
    # would mark any clean 1-session reach as mastered, ignoring that
    # the session probed only one concept of the subsection. The
    # conjunction with confidence prevents that. Calibration:
    #   - First session, perfect answer: mastery ~0.50, confidence ~0.25 → not mastered
    #   - 2 sessions, both clean, different anchors: mastery ~0.75, confidence ~0.50 → not mastered
    #   - 3 sessions, comprehensive: mastery ~0.85, confidence ~0.65 → MASTERED
    # See module docstring for citations.
    MASTERED_THRESHOLD = 0.80
    CONFIDENCE_THRESHOLD = 0.60
    WEAK_THRESHOLD = 0.50

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
        threshold: float | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Return up to `limit` subsections with mastery < threshold,
        sorted ascending by mastery (weakest first).

        threshold defaults to WEAK_THRESHOLD (0.50). The threshold is
        on mastery only — confidence is included in the returned dict
        so callers can show a "low confidence" badge in the UI.

        Each entry is a dict with keys:
          path, mastery, confidence, sessions, last_seen, last_outcome,
          last_rationale, chapter_num, chapter_title, section_title,
          subsection_title
        """
        thresh = self.WEAK_THRESHOLD if threshold is None else float(threshold)
        rows: list[dict] = []
        for path, rec in self.load(student_id).items():
            if not isinstance(rec, dict):
                continue
            mastery = float(rec.get("mastery", 1.0))
            if mastery >= thresh:
                continue
            chapter_num, chapter_title, section_title, subsection_title = (
                _parse_path(path)
            )
            rows.append({
                "path": path,
                "mastery": mastery,
                "confidence": float(rec.get("confidence", 0.0) or 0.0),
                "sessions": int(rec.get("sessions", 0) or 0),
                "last_seen": rec.get("last_seen", ""),
                "last_outcome": rec.get("last_outcome", ""),
                "last_rationale": rec.get("last_rationale", ""),
                "chapter_num": chapter_num,
                "chapter_title": chapter_title,
                "section_title": section_title,
                "subsection_title": subsection_title,
            })
        rows.sort(key=lambda r: r["mastery"])
        return rows[:limit]

    def recent_rationales(
        self, student_id: str, subsection_path: str, limit: int = 3
    ) -> list[str]:
        """Return up to `limit` most-recent rationales for one
        subsection. Used by the LLM scorer to avoid re-rewarding the
        same concept across consecutive sessions.

        The store currently keeps only `last_rationale` per subsection
        (one slot, overwritten each session). For thesis-scale this is
        sufficient — sessions are rare enough that the most-recent one
        is the relevant prior context. If we ever need a longer history
        we extend the schema with a `rationale_log` array per concept.
        """
        rec = self.get(student_id, subsection_path)
        if not isinstance(rec, dict):
            return []
        last = rec.get("last_rationale")
        return [str(last)] if last else []

    def stats(self, student_id: str) -> dict:
        """Aggregate counters for the /mastery dashboard's header.

        Returns:
            {
              "touched": int,        # subsections with at least one session
              "mastered": int,       # mastery >= 0.80 AND confidence >= 0.60
              "avg_mastery": float,  # mean across touched subsections, 0-1
              "avg_confidence": float, # mean confidence
            }
        """
        concepts = self.load(student_id)
        if not concepts:
            return {
                "touched": 0,
                "mastered": 0,
                "avg_mastery": 0.0,
                "avg_confidence": 0.0,
            }
        masteries = [
            float(rec.get("mastery", 0.0))
            for rec in concepts.values()
            if isinstance(rec, dict)
        ]
        confidences = [
            float(rec.get("confidence", 0.0) or 0.0)
            for rec in concepts.values()
            if isinstance(rec, dict)
        ]
        if not masteries:
            return {
                "touched": 0,
                "mastered": 0,
                "avg_mastery": 0.0,
                "avg_confidence": 0.0,
            }
        # "Mastered" applies the conjunction: mastery >= MASTERED_THRESHOLD
        # AND confidence >= CONFIDENCE_THRESHOLD. Iterating concepts.values()
        # fresh here so the count uses the SAME record for both checks
        # (zip(masteries, confidences) would also work since both come from
        # the same iteration — but explicit is clearer).
        n_mastered = 0
        for rec in concepts.values():
            if not isinstance(rec, dict):
                continue
            m = float(rec.get("mastery", 0.0))
            c = float(rec.get("confidence", 0.0) or 0.0)
            if m >= self.MASTERED_THRESHOLD and c >= self.CONFIDENCE_THRESHOLD:
                n_mastered += 1
        return {
            "touched": len(masteries),
            "mastered": n_mastered,
            "avg_mastery": round(sum(masteries) / len(masteries), 3),
            "avg_confidence": round(
                sum(confidences) / max(1, len(confidences)), 3
            ),
        }

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------
    def update(
        self,
        student_id: str,
        subsection_path: str,
        mastery_score: float,
        confidence_score: float,
        outcome: str,
        rationale: str = "",
        session_date: Optional[str] = None,
    ) -> dict:
        """Apply an EWMA update to a subsection's mastery + confidence
        and persist.

        First observation: stored = session value (no prior to blend).
        Subsequent observations: stored = 0.6 * new + 0.4 * old (both
        signals).

        Args:
            student_id:        validated student id
            subsection_path:   "ChN|section|subsection" from
                               state['locked_topic']['path']
            mastery_score:     0.0-1.0 from score_session_llm
            confidence_score:  0.0-1.0 from score_session_llm
            outcome:           "reached" | "not_reached"
            rationale:         LLM's 1-2 sentence explanation; saved as
                               last_rationale and shown in the dashboard
            session_date:      ISO date; defaults to today

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
                blended_mastery = (
                    self.EWMA_NEW * float(mastery_score)
                    + self.EWMA_OLD * float(prior.get("mastery", 0.0))
                )
                blended_confidence = (
                    self.EWMA_NEW * float(confidence_score)
                    + self.EWMA_OLD * float(prior.get("confidence", 0.0) or 0.0)
                )
                sessions = int(prior.get("sessions", 0)) + 1
            else:
                blended_mastery = float(mastery_score)
                blended_confidence = float(confidence_score)
                sessions = 1

            # Clamp + round for storage. Round to 3 decimals so the JSON
            # file stays human-readable.
            blended_mastery = max(0.0, min(1.0, blended_mastery))
            blended_confidence = max(0.0, min(1.0, blended_confidence))
            record = {
                "mastery": round(blended_mastery, 3),
                "confidence": round(blended_confidence, 3),
                "sessions": sessions,
                "last_seen": session_date,
                "last_outcome": outcome,
                "last_rationale": str(rationale or "")[:500],
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
