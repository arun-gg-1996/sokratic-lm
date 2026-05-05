"""
backend/api/sessions.py
-----------------------
M5 (Analysis View) — per-session detail endpoints.

Routes:
  GET  /api/sessions/{thread_id}                    → metadata + key_takeaways
  GET  /api/sessions/{thread_id}/transcript         → full message log
  POST /api/sessions/{thread_id}/analysis_chat      → scoped read-only chat
  POST /api/sessions/{thread_id}/regenerate_takeaways → retry close LLM

The transcript fetch reads from the JSON artifacts written by
nodes._log_conversation (data/artifacts/conversations/*.json). Files are
named `{student_id}_{thread_suffix}_turn_N.json`. We glob by thread_suffix
so the API doesn't need a name-format migration (per M5 D5).

Analysis chat behavior (per M5 D1):
  Step 1: Haiku scope check (~$0.0003) — is this question about THIS
          session's subsection? If NO → refusal, no Sonnet call.
  Step 2: If YES → Sonnet with (transcript + locked Q/A + chunks +
          mem0 filtered by subsection_path + this analysis history).
  Step 3: NO DB writes (read-only meta-discussion). History lives only
          in the request payload — ephemeral per visit (D2).

Regenerate takeaways: rebuilds the close-LLM input from the transcript
artifact + sessions row, re-fires Teacher.draft(mode="close"), parses
the JSON output, UPDATEs sessions.key_takeaways. Same code path as
memory_update_node._draft_close_message.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import cfg

router = APIRouter()


# ─── Models ─────────────────────────────────────────────────────────────────

class TranscriptMessage(BaseModel):
    role: str
    content: str
    phase: Optional[str] = None
    metadata: Optional[dict] = None


class TranscriptResponse(BaseModel):
    thread_id: str
    student_id: Optional[str] = None
    messages: list[TranscriptMessage] = []


class AnalysisChatRequest(BaseModel):
    message: str
    history: list[dict] = []   # ephemeral [{role, content}, ...]


class AnalysisChatResponse(BaseModel):
    thread_id: str
    reply: str
    in_scope: bool
    cost_estimate_usd: float = 0.0


class RegenerateResponse(BaseModel):
    thread_id: str
    success: bool
    key_takeaways: Optional[dict] = None
    error: Optional[str] = None


# ─── Transcript fetch ────────────────────────────────────────────────────────

def _resolve_transcript_path(thread_id: str) -> Optional[Path]:
    """Glob conversations/ for the latest snapshot of this thread.

    Filenames are `{student_id}_{thread_suffix}_turn_N.json` (per
    nodes._log_conversation) — we don't have the student_id yet when
    looking up by thread_id, but the thread_id format
    `{student_id}_{uuid8}` means thread_suffix is the last 8 chars.
    Glob matches any filename containing the suffix.
    """
    artifacts_dir = Path(cfg.paths.artifacts) / "conversations"
    if not artifacts_dir.is_absolute():
        artifacts_dir = Path(__file__).resolve().parent.parent.parent / cfg.paths.artifacts / "conversations"
    if not artifacts_dir.exists():
        return None
    suffix = thread_id.split("_")[-1] if "_" in thread_id else thread_id
    matches = sorted(artifacts_dir.glob(f"*{suffix}*_turn_*.json"))
    if not matches:
        return None
    # Pick the highest turn number (= latest snapshot).
    def _turn_num(p: Path) -> int:
        try:
            return int(p.stem.split("_turn_")[-1])
        except ValueError:
            return 0
    matches.sort(key=_turn_num)
    return matches[-1]


@router.get("/sessions/{thread_id}/transcript", response_model=TranscriptResponse)
async def get_session_transcript(thread_id: str) -> TranscriptResponse:
    """Read the most recent conversation snapshot for this thread."""
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")

    p = _resolve_transcript_path(tid)
    if p is None:
        # No artifact — session may not have produced one (very short
        # session, or older session before logging was added).
        return TranscriptResponse(thread_id=tid, messages=[])

    try:
        d = json.loads(p.read_text())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"transcript read error: {type(e).__name__}: {str(e)[:120]}")

    student_id = d.get("student_id") or None
    raw_messages = d.get("messages") or []
    out: list[TranscriptMessage] = []
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        content = str(m.get("content") or "")
        if not role:
            continue
        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else None
        out.append(TranscriptMessage(
            role=role, content=content,
            phase=str(m.get("phase") or "") or None,
            metadata=meta,
        ))
    return TranscriptResponse(thread_id=tid, student_id=student_id, messages=out)


# ─── Analysis chat ───────────────────────────────────────────────────────────

_SCOPE_CHECK_SYSTEM = """\
You are a scope guard for a session-analysis chat. The student is
reviewing a PAST tutoring session about a SPECIFIC subsection. They
should ONLY ask questions about THIS session — what they answered,
why they got something wrong, what the locked question was, etc.

If the question is about THIS session or its subsection content →
verdict="in_scope".

If the question is asking to learn a new topic (e.g. "what is the
mitochondria?" when the session was about the spleen) →
verdict="off_scope".

Output STRICT JSON only:
{
  "verdict": "in_scope" | "off_scope",
  "rationale": "<1-line explanation>"
}
"""


def _scope_check(student_message: str, locked_subsection: str) -> dict:
    from conversation.classifiers import _haiku_call, _cached_system_block, _extract_json
    user_text = (
        f"LOCKED SUBSECTION: {locked_subsection or '(unknown)'}\n\n"
        f"STUDENT'S QUESTION:\n{student_message}"
    )
    try:
        raw = _haiku_call(_cached_system_block(_SCOPE_CHECK_SYSTEM), user_text)
    except Exception as e:
        # Fail-open — let Sonnet handle it (better than hard refusal).
        return {"verdict": "in_scope", "rationale": f"haiku_err: {type(e).__name__}"}
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return {"verdict": "in_scope", "rationale": "parse_fail"}
    v = str(parsed.get("verdict", "in_scope")).strip().lower()
    if v not in {"in_scope", "off_scope"}:
        v = "in_scope"
    return {"verdict": v, "rationale": str(parsed.get("rationale", ""))[:200]}


_ANALYSIS_SYSTEM = """\
You are a Socratic tutor reviewing a PAST session with the student.
You see the full transcript of that session, the locked question +
textbook answer, and chunks from the locked subsection. You may also
see prior messages in this analysis chat.

Your job: answer the student's question about WHAT HAPPENED in that
session. Reference SPECIFIC turns when useful ("on turn 4 you said
X..."). Help them understand why they got stuck or what they could
have done better.

Strict rules:
- 2-4 sentences max. Conversational.
- Reference the transcript by turn number when useful.
- Do NOT teach the wider topic — just analyze THIS session.
- Do NOT propose a new exercise; this is read-only meta-discussion.
"""


@router.post("/sessions/{thread_id}/analysis_chat", response_model=AnalysisChatResponse)
async def analysis_chat(thread_id: str, req: AnalysisChatRequest) -> AnalysisChatResponse:
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message required")

    # Load session metadata + transcript
    from memory.sqlite_store import SQLiteStore
    store = SQLiteStore()
    sess = store.get_session(tid)
    if not sess:
        raise HTTPException(status_code=404, detail=f"unknown thread_id: {tid!r}")

    locked_subsection = (
        sess.get("locked_subsection_path") or sess.get("locked_topic_path") or ""
    )
    # Take the leaf for prompt readability (full path is hierarchical).
    locked_sub_leaf = ""
    if locked_subsection:
        # Format examples vary; both "Ch1|sec|sub" and "Ch... > ... > ..." occur.
        for sep in (" > ", "|", "/"):
            if sep in locked_subsection:
                locked_sub_leaf = locked_subsection.rsplit(sep, 1)[-1].strip()
                break
        if not locked_sub_leaf:
            locked_sub_leaf = locked_subsection

    # Scope check (Haiku, ~$0.0003)
    scope = _scope_check(msg, locked_sub_leaf)
    if scope["verdict"] == "off_scope":
        return AnalysisChatResponse(
            thread_id=tid,
            reply=(
                f"This is your {locked_sub_leaf or 'session'} review — to learn "
                "other topics, start a new session from My Mastery."
            ),
            in_scope=False,
            cost_estimate_usd=0.0003,
        )

    # Load transcript for prompt context
    transcript_resp = await get_session_transcript(tid)
    transcript_lines = []
    for i, m in enumerate(transcript_resp.messages):
        if m.role not in {"tutor", "student"}:
            continue
        prefix = "STUDENT" if m.role == "student" else "TUTOR"
        transcript_lines.append(f"[turn {i}] {prefix}: {m.content[:600]}")
    transcript_block = "\n".join(transcript_lines) or "(no transcript)"

    locked_q = sess.get("locked_question") or ""
    locked_a = sess.get("locked_answer") or sess.get("full_answer") or ""

    # Append prior analysis-chat history (D2 ephemeral — caller carries it)
    prior_history = "\n".join(
        f"{(h.get('role') or '').upper()}: {(h.get('content') or '')[:400]}"
        for h in (req.history or [])
        if isinstance(h, dict) and h.get("content")
    )

    user_prompt = (
        f"LOCKED SUBSECTION: {locked_sub_leaf}\n"
        f"LOCKED QUESTION:   {locked_q}\n"
        f"TEXTBOOK ANSWER:   {locked_a}\n\n"
        f"PAST SESSION TRANSCRIPT:\n{transcript_block}\n\n"
        + (f"\nPRIOR ANALYSIS CHAT:\n{prior_history}\n" if prior_history else "")
        + f"\nSTUDENT'S CURRENT QUESTION:\n{msg}\n\n"
        "Respond in 2-4 sentences. Reference turn numbers when useful."
    )

    from conversation.llm_client import make_anthropic_client, resolve_model
    client = make_anthropic_client()
    try:
        resp = client.messages.create(
            model=resolve_model(cfg.models.teacher),
            max_tokens=400,
            messages=[{"role": "user", "content": user_prompt}],
            system=[{"type": "text", "text": _ANALYSIS_SYSTEM}],
        )
        text = (resp.content[0].text or "").strip() if resp.content else ""
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"analysis chat LLM error: {type(e).__name__}: {str(e)[:120]}",
        )

    return AnalysisChatResponse(
        thread_id=tid,
        reply=text or "(no response)",
        in_scope=True,
        cost_estimate_usd=0.005,  # rough — Sonnet ~$3/M tokens, ~1.5K tokens
    )


# ─── Regenerate takeaways ───────────────────────────────────────────────────

@router.post("/sessions/{thread_id}/regenerate_takeaways", response_model=RegenerateResponse)
async def regenerate_takeaways(thread_id: str) -> RegenerateResponse:
    """M5 — re-fire the close LLM for a session whose key_takeaways is null
    or stale. Reads transcript + sessions row, calls Teacher with mode=close,
    parses JSON output, UPDATEs the sessions row in place.
    """
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")

    from memory.sqlite_store import SQLiteStore
    from conversation.lifecycle_v2 import _draft_close_message
    from conversation.state import initial_state, TutorState

    store = SQLiteStore()
    sess = store.get_session(tid)
    if not sess:
        raise HTTPException(status_code=404, detail=f"unknown thread_id: {tid!r}")

    transcript = await get_session_transcript(tid)
    rebuilt_messages = [
        {"role": m.role, "content": m.content, "phase": m.phase or "tutoring"}
        for m in transcript.messages
        if m.role in {"tutor", "student"}
    ]
    student_id = sess.get("student_id") or transcript.student_id or ""

    # Build a minimal TutorState shell so _draft_close_message has what it needs.
    state: TutorState = initial_state(student_id, cfg)  # type: ignore[arg-type]
    state["thread_id"] = tid
    state["messages"] = rebuilt_messages
    state["locked_question"] = sess.get("locked_question") or ""
    state["locked_answer"] = sess.get("locked_answer") or sess.get("full_answer") or ""
    state["full_answer"] = sess.get("full_answer") or ""
    state["student_reached_answer"] = bool(sess.get("reach_status"))
    locked_path = sess.get("locked_subsection_path") or sess.get("locked_topic_path") or ""
    state["locked_topic"] = {
        "path": locked_path,
        "subsection": locked_path.rsplit("|", 1)[-1] if "|" in locked_path else locked_path,
    }

    # Pick a reason. Default heuristic for regenerate.
    if state["student_reached_answer"]:
        reason = "reach_skipped"
    else:
        reason = "tutoring_cap"

    payload = _draft_close_message(state, reason)
    if not payload.get("message"):
        return RegenerateResponse(
            thread_id=tid, success=False,
            error=payload.get("_error") or "empty close draft",
        )
    takeaways = {
        "demonstrated": payload.get("demonstrated") or "",
        "needs_work": payload.get("needs_work") or "",
        "close_reason": reason,
        "regenerated": True,
    }
    try:
        store.update_session(tid, key_takeaways=takeaways)
    except Exception as e:
        return RegenerateResponse(
            thread_id=tid, success=False,
            error=f"sqlite update error: {type(e).__name__}: {str(e)[:120]}",
        )
    return RegenerateResponse(
        thread_id=tid, success=True, key_takeaways=takeaways,
    )
