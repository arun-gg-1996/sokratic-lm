"""
Helper for fetching the locked questions a student has already worked
through on a given subsection. The Dean uses the result to vary the
anchor question on repeat visits instead of re-asking the same thing.

  fetch_prior_locked_questions(student_id, subsection_path) -> list[str]

Returns most-recent first. Empty list on first visit or on any error
(this helper never raises).
"""

from __future__ import annotations

from typing import Optional

# Cap on rows we pull. Beyond ~8 the LLM context bloat outweighs the
# avoid signal — and a student who's done 8+ sessions on one subsection
# is in the reframe fallback anyway.
_MAX_PRIOR_QUESTIONS = 8

def fetch_prior_locked_questions(
    student_id: str,
    subsection_path: str,
    *,
    store: Optional[object] = None,
) -> list[str]:
    """Return distinct locked_questions for this student × subsection
 newest-first.

 Never raises. Returns on any error or when the inputs are
 missing.

 Parameters
student_id : str
 Required. Empty/None → return .
 subsection_path : str
 Required. The canonical "Chapter X > Section > Subsection"
 path. Empty/None → return .
 store : optional
 Optional SQLiteStore instance for tests. Defaults to the
 module-level singleton via fresh import.

 Returns
list[str]
 Distinct locked_questions, most-recent-first. Capped at
 _MAX_PRIOR_QUESTIONS. Empty list if no prior history.
"""
    sid = (student_id or "").strip()
    path = (subsection_path or "").strip()
    if not sid or not path:
        return []

    try:
        if store is None:
            from memory.sqlite_store import SQLiteStore
            store = SQLiteStore()
        rows = store.list_sessions(
            sid,
            subsection_path=path,
            limit=_MAX_PRIOR_QUESTIONS * 2,  # over-fetch then dedupe
        )
    except Exception:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        q = (r.get("locked_question") or "").strip()
        if not q:
            continue
        # Case-insensitive dedupe on a normalized key, but preserve the
        # original casing from the most-recent row.
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= _MAX_PRIOR_QUESTIONS:
            break
    return out
