"""
backend/api/mastery.py
-----------------------
Endpoints for the per-concept knowledge tracing dashboard.

Wires MasteryStore (memory/mastery_store.py) to the frontend /mastery
page. Three things the page needs:

  1. Aggregate stats for the header  (touched / mastered / avg)
  2. Per-session log                  (for the Sessions list section)
  3. Full concepts grouped by chapter (for the Chapter tree section)

Rather than three separate endpoints we return one bundled payload —
the page renders all three from one fetch. Lightweight (~tens of KB
even for a heavy user).

The Sessions log piece is reconstructed from mem0 (filter by
category=session_summary, get_all sorted by created_at) joined with
the mastery score for each session's locked subsection. mem0 holds
the per-session narrative; MasteryStore holds the score history. Both
are filtered by the same student_id so isolation matches the rest of
the system.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.users import known_student_id
from backend.dependencies import get_memory_manager
from memory.mastery_store import MasteryStore
from memory.memory_manager import MemoryManager

router = APIRouter(prefix="/mastery", tags=["mastery"])


# --- response models ---

class MasteryHeader(BaseModel):
    touched: int            # subsections with at least one session
    mastered: int           # mastery >= 0.80 AND confidence >= 0.60
    avg_mastery: float      # mean across touched, 0-1
    avg_confidence: float = 0.0   # mean confidence across touched


class MasteryConcept(BaseModel):
    path: str
    chapter_num: int
    chapter_title: str
    section_title: str
    subsection_title: str
    mastery: float
    confidence: float = 0.0
    sessions: int
    last_seen: str
    last_outcome: str
    last_rationale: str = ""


class MasteryChapterRow(BaseModel):
    """One row in the chapter-tree section: chapter + its subsections."""
    chapter_num: int
    chapter_title: str
    avg_mastery: float
    n_subsections_touched: int
    concepts: list[MasteryConcept]


class MasterySessionEntry(BaseModel):
    """One row in the sessions log: a past session with its outcome
    and the subsection's CURRENT mastery (post-EWMA-blend).

    A session can map to a subsection that no longer appears in
    MasteryStore (corruption, deletion). In that case mastery=null
    and the frontend just hides the bar.
    """
    session_date: str
    chapter_num: int
    chapter_title: str
    section_title: str
    subsection_title: str
    subsection_path: str
    outcome: str
    mastery: Optional[float] = None
    summary_text: str


class MasteryDashboardResponse(BaseModel):
    student_id: str
    available: bool          # mastery store + mem0 both readable
    header: MasteryHeader
    chapters: list[MasteryChapterRow]
    sessions: list[MasterySessionEntry]


# --- helpers ---

def _build_chapter_rows(concepts: dict) -> list[MasteryChapterRow]:
    """Group concepts dict (from MasteryStore.load) by chapter."""
    by_ch: dict[int, dict] = defaultdict(lambda: {
        "chapter_num": 0,
        "chapter_title": "",
        "concepts": [],
    })
    for path, rec in concepts.items():
        if not isinstance(rec, dict):
            continue
        # Path: "ChN|section|subsection" — re-parse so the API doesn't
        # need a separate import from mastery_store internals.
        parts = path.split("|", 2)
        chapter_num = 0
        if parts and parts[0].startswith("Ch") and parts[0][2:].isdigit():
            chapter_num = int(parts[0][2:])
        section_title = parts[1] if len(parts) >= 2 else ""
        subsection_title = parts[2] if len(parts) >= 3 else ""
        bucket = by_ch[chapter_num]
        bucket["chapter_num"] = chapter_num
        # We don't have chapter_title in the store (path encodes only
        # number + section + subsection). Section_title is a usable
        # placeholder for the row label until we plumb chapter titles
        # through; the UI displays "Ch{n}" prominently anyway.
        bucket["chapter_title"] = bucket["chapter_title"] or section_title
        bucket["concepts"].append(MasteryConcept(
            path=path,
            chapter_num=chapter_num,
            chapter_title=bucket["chapter_title"],
            section_title=section_title,
            subsection_title=subsection_title,
            mastery=float(rec.get("mastery", 0.0)),
            confidence=float(rec.get("confidence", 0.0) or 0.0),
            sessions=int(rec.get("sessions", 0) or 0),
            last_seen=str(rec.get("last_seen", "") or ""),
            last_outcome=str(rec.get("last_outcome", "") or ""),
            last_rationale=str(rec.get("last_rationale", "") or ""),
        ))

    rows: list[MasteryChapterRow] = []
    for ch_num in sorted(by_ch.keys()):
        b = by_ch[ch_num]
        if not b["concepts"]:
            continue
        masteries = [c.mastery for c in b["concepts"]]
        rows.append(MasteryChapterRow(
            chapter_num=ch_num,
            chapter_title=b["chapter_title"],
            avg_mastery=round(sum(masteries) / len(masteries), 3),
            n_subsections_touched=len(b["concepts"]),
            concepts=sorted(b["concepts"], key=lambda c: c.subsection_title),
        ))
    return rows


def _build_session_log(
    student_id: str,
    mm: MemoryManager,
    concepts: dict,
    limit: int = 20,
) -> list[MasterySessionEntry]:
    """Pull session_summary entries from mem0, join with mastery scores.

    mem0 stores one session_summary per session — we filter to those,
    sort by created_at desc, and produce a Sessions-list-ready record
    per entry. The mastery score is the CURRENT (post-blend) value, so
    a row from 5 sessions ago shows what mastery looks like NOW for
    that subsection — which is what the dashboard wants to display.
    """
    if not mm.persistent.available:
        return []
    try:
        raw = mm.persistent.get(
            student_id,
            query="session summary by date",
            filters={"category": "session_summary"},
        )
    except Exception:
        raw = []

    # Dedupe: mem0 atomizes one session's NL writes into multiple
    # category-tagged atoms, all sharing the same (date, topic_path)
    # metadata. Group by that key. For the representative summary
    # text we prefer category=session_summary atoms (the explicit
    # "this is the session's summary" entries) over learning-style
    # cues or other categories. Falls back to longest text in any
    # category when no session_summary atom exists for the group.
    grouped: dict[tuple, list[dict]] = {}
    for entry in (raw or []):
        if not isinstance(entry, dict):
            continue
        meta = entry.get("metadata") or {}
        date = str(meta.get("session_date") or "")
        path = str(meta.get("topic_path") or "")
        # If both date and path are empty (legacy entries pre-metadata),
        # bucket them together under ("", "") so they don't multiply
        # into one row per atom — better one degraded-but-cohesive row
        # than spam.
        key = (date, path)
        grouped.setdefault(key, []).append(entry)

    rows: list[MasterySessionEntry] = []
    for (date, path), atoms in grouped.items():
        # Pick representative atom + summary text. Preference order:
        #   1. category=session_summary, longest text
        #   2. any category, longest text
        summary_atoms = [
            a for a in atoms
            if (a.get("metadata") or {}).get("category") == "session_summary"
        ]
        pool = summary_atoms or atoms
        rep = max(
            pool,
            key=lambda a: len((a.get("memory") or a.get("text") or "")),
        )
        rep_meta = rep.get("metadata") or {}
        text = (rep.get("memory") or rep.get("text") or "")[:200]

        # Also harvest title fields from ANY atom in the group — some
        # atoms may have empty subsection_title even when a sibling
        # has the proper value (mem0 metadata is shared per write
        # but mem0 sometimes drops fields on certain atomized facts).
        # Find the first non-empty title across the group.
        def _first_nonempty(field: str) -> str:
            for a in atoms:
                v = ((a.get("metadata") or {}).get(field) or "")
                if str(v).strip():
                    return str(v)
            return ""

        rec = concepts.get(path) if isinstance(concepts, dict) else None
        mastery = (
            float(rec.get("mastery")) if isinstance(rec, dict) and "mastery" in rec
            else None
        )
        rows.append(MasterySessionEntry(
            session_date=date,
            chapter_num=int(rep_meta.get("chapter_num") or 0),
            chapter_title=_first_nonempty("chapter_title"),
            section_title=_first_nonempty("section_title"),
            subsection_title=_first_nonempty("subsection_title"),
            subsection_path=path,
            outcome=str(rep_meta.get("outcome") or ""),
            mastery=mastery,
            summary_text=text,
        ))

    # Sort newest-first by session_date (ISO-8601 strings sort correctly).
    rows.sort(key=lambda r: r.session_date, reverse=True)
    return rows[:limit]


# --- endpoint ---

@router.get("/{student_id}", response_model=MasteryDashboardResponse)
async def get_mastery(
    student_id: str,
    mm: MemoryManager = Depends(get_memory_manager),
) -> MasteryDashboardResponse:
    """Bundled dashboard payload (header + chapters + sessions).

    Returns 200 with empty containers when the student has no data
    yet — the frontend treats that as "fresh student" and shows
    appropriate empty-state copy.
    """
    sid = (student_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="student_id required")
    if not known_student_id(sid):
        raise HTTPException(
            status_code=400,
            detail=f"unknown student_id: {sid!r}",
        )

    store = MasteryStore()
    concepts = store.load(sid)
    stats = store.stats(sid)
    chapters = _build_chapter_rows(concepts)
    sessions = _build_session_log(sid, mm, concepts)

    return MasteryDashboardResponse(
        student_id=sid,
        available=True,  # store is always available; mem0 may be down
                         # but we still return the mastery half
        header=MasteryHeader(**stats),
        chapters=chapters,
        sessions=sessions,
    )
