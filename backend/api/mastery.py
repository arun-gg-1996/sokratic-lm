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


# ─────────────────────────────────────────────────────────────────────────────
# L29-L34 SQLite-backed endpoints (track 1.9 — additive, coexists with the
# legacy /mastery/{student_id} JSON-backed endpoint above)
# ─────────────────────────────────────────────────────────────────────────────


class MasterySubsectionNode(BaseModel):
    subsection: str
    display_label: str
    path: str
    score: Optional[float] = None
    color: str = "grey"
    tier: str = "not_assessed"
    outcome: Optional[str] = None
    last_session_at: Optional[str] = None
    attempt_count: int = 0


class MasterySectionNode(BaseModel):
    section: str
    score: Optional[float] = None
    color: str = "grey"
    tier: str = "not_assessed"
    touched: int = 0
    total: int = 0
    subsections: list[MasterySubsectionNode] = []


class MasteryChapterNode(BaseModel):
    chapter: str
    chapter_num: Optional[int] = None
    score: Optional[float] = None
    color: str = "grey"
    tier: str = "not_assessed"
    touched: int = 0
    total: int = 0
    sections: list[MasterySectionNode] = []


class MasteryTreeResponse(BaseModel):
    student_id: str
    chapters: list[MasteryChapterNode] = []


class MasterySessionRow(BaseModel):
    """One row in the My Mastery sessions list (per L29-L34)."""
    thread_id: str
    student_id: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    locked_topic_path: Optional[str] = None
    locked_subsection_path: Optional[str] = None
    # M5 — surface locked Q/A so the analysis view doesn't need a second fetch.
    locked_question: Optional[str] = None
    locked_answer: Optional[str] = None
    full_answer: Optional[str] = None
    mastery_tier: Optional[str] = None
    core_mastery_tier: Optional[str] = None
    clinical_mastery_tier: Optional[str] = None
    core_score: Optional[float] = None
    clinical_score: Optional[float] = None
    status: str
    turn_count: Optional[int] = None
    reach_status: Optional[bool] = None
    key_takeaways: Optional[dict] = None


class MasterySessionsResponse(BaseModel):
    student_id: str
    sessions: list[MasterySessionRow] = []


def _load_topic_index_for_tree() -> list[dict]:
    """Read the active domain's topic_index for tree rollup.

    Per L78, every domain config has its own `cfg.paths.topic_index_{domain}`
    slot (added in track 0 / 65d5111). Falls back to the legacy
    `data/topic_index.json` only if the per-domain slot is absent — which
    shouldn't happen in production since the slot is mandatory per L78.
    """
    import json
    from pathlib import Path
    from config import cfg as _cfg
    domain = _cfg.domain.retrieval_domain
    slot = f"topic_index_{domain}"
    path_str = getattr(_cfg.paths, slot, None) or "data/topic_index.json"
    p = Path(path_str)
    if not p.is_absolute():
        # Resolve relative to repo root (parent of backend/)
        p = Path(__file__).resolve().parent.parent.parent / path_str
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    return raw if isinstance(raw, list) else list(raw.values())


def _row_to_session_model(row: dict) -> MasterySessionRow:
    """Convert a SQLite session row dict to the API response model."""
    reach = row.get("reach_status")
    return MasterySessionRow(
        thread_id=row["thread_id"],
        student_id=row["student_id"],
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        locked_topic_path=row.get("locked_topic_path"),
        locked_subsection_path=row.get("locked_subsection_path"),
        locked_question=row.get("locked_question"),
        locked_answer=row.get("locked_answer"),
        full_answer=row.get("full_answer"),
        mastery_tier=row.get("mastery_tier"),
        core_mastery_tier=row.get("core_mastery_tier"),
        clinical_mastery_tier=row.get("clinical_mastery_tier"),
        core_score=row.get("core_score"),
        clinical_score=row.get("clinical_score"),
        status=row.get("status") or "in_progress",
        turn_count=row.get("turn_count"),
        reach_status=bool(reach) if reach is not None else None,
        key_takeaways=row.get("key_takeaways") if isinstance(row.get("key_takeaways"), dict) else None,
    )


@router.get("/v2/{student_id}/tree", response_model=MasteryTreeResponse)
async def get_mastery_tree(student_id: str) -> MasteryTreeResponse:
    """Per L29-L34 — full mastery tree rolled up from the per-domain SQLite store.

    Returns nested chapters → sections → subsections with score / color /
    tier / coverage at every level. Untouched nodes report score=None,
    color="grey" so the frontend can render greyed-out cards without
    extra branching.
    """
    sid = (student_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="student_id required")
    if not known_student_id(sid):
        raise HTTPException(status_code=400, detail=f"unknown student_id: {sid!r}")

    from memory.sqlite_store import SQLiteStore
    store = SQLiteStore()  # picks active domain from cfg
    topic_index = _load_topic_index_for_tree()
    tree = store.mastery_tree(sid, topic_index)
    # MasteryChapterNode mirrors mastery_tree's output exactly — pass through.
    return MasteryTreeResponse(student_id=sid, chapters=tree["chapters"])


@router.get("/v2/{student_id}/sessions", response_model=MasterySessionsResponse)
async def get_mastery_sessions(
    student_id: str,
    limit: int = 50,
    completed_only: bool = False,
    subsection_path: Optional[str] = None,
) -> MasterySessionsResponse:
    """Per L29-L34 — list of sessions newest-first, with key fields needed
    by the My Mastery sessions panel.

    M5: optional `subsection_path` query param filters to sessions for one
    subsection (used by the inline session list under each row).
    """
    sid = (student_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="student_id required")
    if not known_student_id(sid):
        raise HTTPException(status_code=400, detail=f"unknown student_id: {sid!r}")

    from memory.sqlite_store import SQLiteStore
    store = SQLiteStore()
    rows = store.list_sessions(
        sid,
        limit=max(1, min(limit, 200)),
        completed_only=completed_only,
        subsection_path=subsection_path,
    )
    return MasterySessionsResponse(
        student_id=sid,
        sessions=[_row_to_session_model(r) for r in rows],
    )


@router.get("/v2/session/{thread_id}", response_model=MasterySessionRow)
async def get_mastery_session(thread_id: str) -> MasterySessionRow:
    """Per L29-L34 — single session detail (used by the Revisit / Analyze view)."""
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")

    from memory.sqlite_store import SQLiteStore
    store = SQLiteStore()
    row = store.get_session(tid)
    if not row:
        raise HTTPException(status_code=404, detail=f"unknown thread_id: {tid!r}")
    return _row_to_session_model(row)
