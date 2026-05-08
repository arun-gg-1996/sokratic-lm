"""
Per-session detail endpoints (the analysis view).

  GET  /api/sessions/{thread_id}                       — metadata + takeaways
  GET  /api/sessions/{thread_id}/transcript            — full message log
  POST /api/sessions/{thread_id}/analysis_chat         — scoped read-only chat
  POST /api/sessions/{thread_id}/regenerate_takeaways  — retry the close LLM

Transcript fetches read the per-turn JSON artifacts. Analysis chat
runs a Haiku scope check first to ensure questions stay on the locked
subsection; out-of-scope questions get refused without spending a
Sonnet call. The chat is read-only — no database writes happen.
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
    """Read the persisted conversation transcript for one session.

  Source of truth post-migration 003 is the SQL `messages` table. Falls
  back to the legacy per-turn JSON snapshot file only when SQL has no
  rows (older sessions written before chat persistence was wired).
  """
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")

    # SQL first.
    from memory.sqlite_store import SQLiteStore
    store = SQLiteStore()
    sql_msgs = store.list_messages(tid)
    if sql_msgs:
        sess = store.get_session(tid)
        student_id = (sess or {}).get("student_id")
        out: list[TranscriptMessage] = [
            TranscriptMessage(
                role=str(m.get("role") or ""),
                content=str(m.get("content") or ""),
                phase=(m.get("metadata") or {}).get("phase") if isinstance(m.get("metadata"), dict) else None,
                metadata=m.get("metadata") if isinstance(m.get("metadata"), dict) else None,
            )
            for m in sql_msgs
            if str(m.get("role") or "").strip()
        ]
        return TranscriptResponse(thread_id=tid, student_id=student_id, messages=out)

    # Legacy fallback: JSON snapshot files written before migration 003.
    p = _resolve_transcript_path(tid)
    if p is None:
        return TranscriptResponse(thread_id=tid, messages=[])

    try:
        d = json.loads(p.read_text())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"transcript read error: {type(e).__name__}: {str(e)[:120]}")

    student_id = d.get("student_id") or None
    raw_messages = d.get("messages") or []
    out = []
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        if not role:
            continue
        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else None
        out.append(TranscriptMessage(
            role=role, content=str(m.get("content") or ""),
            phase=str(m.get("phase") or "") or None,
            metadata=meta,
        ))
    return TranscriptResponse(thread_id=tid, student_id=student_id, messages=out)

# ─── Analysis chat ───────────────────────────────────────────────────────────

_SCOPE_CHECK_SYSTEM = """\
You are a scope guard for a session-analysis chat. The student is
reviewing a PAST tutoring session about a SPECIFIC subsection. The
allowed conversation surface is META-DISCUSSION about that session.

The classification principle:

  in_scope  — the question is ABOUT the past session: what happened,
              what was scored, what the student missed, why the tutor
              acted a certain way, what the locked subsection was,
              where it sits in the textbook, what patterns the tutor
              observed, how to do better next time, etc. The answer
              would naturally cite turns or session-level metadata
              ("on turn 6 you said..." / "your score reflects...").

  off_scope — the student is asking the tutor to TEACH them content
              from a DIFFERENT subsection, navigate to another part
              of the app, or do a fresh tutoring exercise. The answer
              would require leaving the session-review frame.

Default to in_scope. Only mark off_scope when the request clearly
exits the session-review frame.

A few calibration examples covering categorically different shapes
(do NOT match these literally — match the underlying intent):

  Student: "I don't understand why I lost points there"
  → in_scope. Asking about the session's scoring.

  Student: "where does this topic appear in the book?"
  → in_scope. Asking about THIS session's topic — its location in
     the curriculum is metadata about THIS subsection. The student
     is NOT asking you to teach a different chapter.

  Student: "what's your read on how I did?"
  → in_scope. Asking for the tutor's assessment of THIS session.

  Student: "I noticed you sent that note at the end — what was it?"
  → in_scope. Asking about a specific message in the transcript.

  Student: "give me a new exercise on the same topic"
  → off_scope. Asking for a new tutoring session, not analysis.

  Student: "explain photosynthesis to me"
  → off_scope (assuming the session wasn't about photosynthesis).
     Asking to be taught a different subject.

  Student: "next lesson please"
  → off_scope. App navigation, not session analysis.

Edge case: if the student asks a content question about the SAME
subsection they just studied (e.g. "remind me what hyaline cartilage
is" after a cartilage session) — that's in_scope, because clarifying
content from THIS session is part of reviewing it.

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

    # Pull structured per-turn classifier verdicts from the JSON snapshot
    # so the analysis LLM doesn't have to guess intent/abuse categories
    # from message text alone (which it gets wrong — e.g. classifying a
    # legitimate clarifying question as "off-topic"). The snapshot file
    # carries the actual classifier verdicts the dean recorded at runtime.
    counters_block = ""
    snap_path = _resolve_transcript_path(tid)
    if snap_path is not None:
        try:
            snap_data = json.loads(snap_path.read_text())
            per_turn = (snap_data.get("debug") or {}).get("per_turn_snapshots") or []
            lines = []
            for snap in per_turn:
                if not isinstance(snap, dict):
                    continue
                role = snap.get("role")
                idx = snap.get("turn_index", "?")
                phase = snap.get("phase", "")
                if role == "student":
                    intent = snap.get("intent", "")
                    evidence = (snap.get("intent_evidence") or "")[:60]
                    lines.append(
                        f"  turn {idx} [{phase}] student intent={intent!r}"
                        + (f" evidence={evidence!r}" if evidence else "")
                    )
                elif role == "tutor":
                    mode = snap.get("mode", "")
                    tone = snap.get("tone", "")
                    lines.append(
                        f"  turn {idx} [{phase}] tutor mode={mode!r} tone={tone!r}"
                    )
            # Final session-level totals (derived from latest snapshot or row)
            totals = []
            if per_turn:
                last = per_turn[-1]
                for k in (
                    "total_low_effort_turns",
                    "total_help_abuse_turns",
                    "total_off_topic_turns",
                    "total_clinical_help_abuse_turns",
                    "total_clinical_off_topic_turns",
                ):
                    v = last.get(k, 0) or 0
                    if v:
                        totals.append(f"{k}={v}")
            score_line = (
                f"  core_score={sess.get('core_score')}, "
                f"clinical_score={sess.get('clinical_score')}, "
                f"mastery_tier={sess.get('mastery_tier')!r}"
            )
            if lines:
                counters_block = (
                    "\nSTRUCTURED TURN CLASSIFIER VERDICTS "
                    "(from runtime — these are FACTS about what happened, "
                    "not your interpretation of the transcript):\n"
                    + "\n".join(lines)
                    + (f"\n  TOTALS: {', '.join(totals)}" if totals else "")
                    + f"\n{score_line}\n"
                )
        except Exception:
            # Snapshot may not exist for legacy sessions — fall through
            # without the counters block. The transcript is still usable.
            counters_block = ""

    # Append prior analysis-chat history (D2 ephemeral — caller carries it)
    prior_history = "\n".join(
        f"{(h.get('role') or '').upper()}: {(h.get('content') or '')[:400]}"
        for h in (req.history or [])
        if isinstance(h, dict) and h.get("content")
    )

    # query mem0 for prior
    # observations on THIS subsection so the analysis chat can answer
    # questions that span multiple sessions on the same topic. The
    # docstring at the top of this module promised this; the code
    # didn't deliver until now.
    # Filters:
    # subsection_path = locked_subsection (exact match)
    # category in (misconception, learning_style)
    # Excludes the current thread_id so this isn't just echoing back
    # the same session's observations.
    student_id = sess.get("student_id") or ""
    mem0_block = ""
    if student_id and locked_subsection:
        try:
            from memory.mem0_safe import safe_mem0_read
            from memory.persistent_memory import PersistentMemory
            persistent = PersistentMemory()
            if getattr(persistent, "available", False):
                hits = safe_mem0_read(
                    persistent,
                    student_id=student_id,
                    query=msg,  # let semantic similarity surface relevant observations
                    filters={
                        "subsection_path": locked_subsection,
                        "category": ["misconception", "learning_style"],
                    },
                    top_k=5,
                )
                # Drop hits from THIS thread_id — analysis is about
                # cross-session synthesis, and this thread's observations
                # are already implicit in the transcript above.
                lines = []
                for h in hits or []:
                    if not isinstance(h, dict):
                        continue
                    md = h.get("metadata") if isinstance(h.get("metadata"), dict) else {}
                    if str(md.get("thread_id") or "") == tid:
                        continue
                    text = str(h.get("text") or h.get("memory") or "").strip()
                    if not text:
                        continue
                    cat = str(md.get("category") or "").strip()
                    label = (
                        "Misconception" if cat == "misconception"
                        else "Learning style" if cat == "learning_style"
                        else "Note"
                    )
                    when = str(md.get("session_at") or "")[:10] or "earlier"
                    lines.append(f"  - [{label} · {when}] {text[:200]}")
                if lines:
                    mem0_block = (
                        "\nPRIOR-SESSION OBSERVATIONS ON THIS SUBSECTION "
                        "(across all the student's past sessions, NOT "
                        "this thread):\n" + "\n".join(lines)
                    )
        except Exception:
            # Never block the analysis chat on mem0 failure. Trace it
            # via safe wrapper; just continue without the cross-session
            # block.
            pass

    user_prompt = (
        f"LOCKED SUBSECTION: {locked_sub_leaf}\n"
        f"LOCKED QUESTION:   {locked_q}\n"
        f"TEXTBOOK ANSWER:   {locked_a}\n\n"
        f"PAST SESSION TRANSCRIPT:\n{transcript_block}\n"
        + counters_block
        + mem0_block
        + (f"\n\nPRIOR ANALYSIS CHAT:\n{prior_history}" if prior_history else "")
        + f"\n\nSTUDENT'S CURRENT QUESTION:\n{msg}\n\n"
        "Respond in 2-4 sentences. Reference turn numbers when useful. "
        "When citing intent classifications (off-topic, help-abuse, "
        "low-effort), use the STRUCTURED TURN CLASSIFIER VERDICTS above "
        "as ground truth — do NOT re-classify from the transcript. "
        "If cross-session observations above are relevant, weave them "
        "in naturally (e.g. 'in your earlier session you struggled with X')."
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
    """re-fire the close LLM for a session whose key_takeaways is null
 or stale. Reads transcript + sessions row, calls Teacher with mode=close
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

# ─── — Suggest replies (LLM-driven student-profile suggestions) ───────────

class SuggestRepliesRequest(BaseModel):
    profile: Optional[str] = "S2"  # default: Moderate

class SuggestionItem(BaseModel):
    text: str
    kind: str  # "correct" | "partial" | "wrong_engaged" | "low_effort" | "help_abuse" | "off_topic" | "opt_in_yes" | "opt_in_no" | "exit_intent"
    color: str  # CSS class hint (resolved by frontend)
    rationale: Optional[str] = ""

class SuggestRepliesResponse(BaseModel):
    thread_id: str
    profile: str
    suggestions: list[SuggestionItem] = []
    error: Optional[str] = None

# Profile labels (mirrored from evaluation/simulation/profiles.py for
# stand-alone fidelity — the suggester uses these as natural-language
# pedagogical descriptors, not as code dependencies).
_N8_PROFILES = {
    "S1": ("Strong",
           "answers precisely on the first prompt, uses textbook "
           "vocabulary, asks clarifying questions when stuck"),
    "S2": ("Moderate",
           "produces partial answers needing 1-2 hints, sometimes "
           "guesses with adjacent terms, engages but not deeply"),
    "S3": ("Weak",
           "rarely lands the answer unprompted, hedges heavily, "
           "needs hints 2-3 to converge, occasionally asks for help"),
    "S4": ("Overconfident",
           "states wrong answers with high certainty, doubles down "
           "rather than reconsidering, rarely hedges"),
    "S5": ("Disengaged",
           "produces 'idk' / 'just tell me' / one-word responses, "
           "occasionally drifts off-topic, low engagement"),
    "S6": ("Anxious-Correct",
           "knows the answer but adds qualifiers ('I think', 'maybe'), "
           "asks meta-questions about whether they're on track"),
}

_SUGGEST_SYSTEM = """\
You are a SIMULATOR generating realistic student replies for a
Socratic anatomy tutoring app. The user is testing the tutor and has
toggled "Suggest answers" on so they can act as different student
profiles.

Your output is a JSON list of 4 suggested student replies. Each must
have a different intent class so the user can see how the tutor
handles each branch. The intent classes are:

  correct        — the actual locked answer (or close paraphrase)
  partial        — partially correct / on-track but incomplete
  wrong_engaged  — a real attempt that misses (NOT a related concept,
                   ideally an in-domain confusion the profile would make)
  low_effort     — passive non-engagement: "idk", "i don't know",
                   "not sure", one-word filler
  help_abuse     — active demand for the answer: "just tell me",
                   "what's the answer", "skip"
  off_topic      — clearly off-domain (NOT a domain tangent — outside
                   the textbook subject entirely)
  opt_in_yes     — accept the clinical-bonus offer (only valid in opt_in)
  opt_in_no      — decline the clinical-bonus offer
  exit_intent    — "I want to stop", "no thanks", "let's end"

CRITICAL rules:
- The 4 suggestions MUST be intent-class-diverse (no two with the
  same kind). Pick the 4 most-instructive classes for the current
  phase + profile.
- Each suggestion is what a STUDENT would type — short, plausible,
  in the profile's voice. NOT what the tutor would say.
- Profile-weight the mix: a Strong student gets more correct/partial,
  a Disengaged student gets more low_effort/help_abuse/off_topic.
- If phase is "opt_in" (the tutor just asked Yes/No for clinical),
  return ONLY 2 suggestions: opt_in_yes + opt_in_no.
- If phase is "rapport" or pre-lock, suggestions should be topic
  choices ("the heart", "skin layers"), NOT answers to a Q.

Output STRICT JSON only:
{
  "suggestions": [
    {"text": "...", "kind": "correct", "rationale": "..."},
    {"text": "...", "kind": "partial", "rationale": "..."},
    {"text": "...", "kind": "low_effort", "rationale": "..."},
    {"text": "...", "kind": "off_topic", "rationale": "..."}
  ]
}
"""

def _color_for_kind(kind: str) -> str:
    """Map intent class → frontend color token. Mirrored in
 SuggestionBubbles.tsx for consistency."""
    return {
        "correct": "green",
        "partial": "yellow-green",
        "wrong_engaged": "yellow",
        "low_effort": "orange",
        "help_abuse": "red-orange",
        "off_topic": "red",
        "opt_in_yes": "blue",
        "opt_in_no": "blue",
        "exit_intent": "purple",
    }.get(kind, "muted")

@router.post("/sessions/{thread_id}/suggest_replies", response_model=SuggestRepliesResponse)
async def suggest_replies(thread_id: str, request: SuggestRepliesRequest) -> SuggestRepliesResponse:
    """student-profile reply
 suggester for testing.

 Pulls the live thread state from the runtime store, builds a
 context-aware prompt, fires Haiku, returns 4 intent-class-diverse
 student-reply suggestions. Intended for QA / demo use only — every
 call is one Haiku request (~$0.001). No DB writes."""
    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")

    profile_id = (request.profile or "S2").strip().upper()
    if profile_id not in _N8_PROFILES:
        profile_id = "S2"
    profile_name, profile_desc = _N8_PROFILES[profile_id]

    # Pull live state. If the thread isn't in the runtime store, fall
    # back to empty context — suggestions will be generic but still
    # diverse-by-class.
    from backend.dependencies import get_runtime_store
    runtime = get_runtime_store()
    state = runtime.get(tid) or {}

    locked_q = str(state.get("locked_question") or "").strip()
    locked_a = str(state.get("locked_answer") or "").strip()
    full_a = str(state.get("full_answer") or "").strip()
    phase = str(state.get("phase") or "tutoring").strip()
    hint_level = int(state.get("hint_level", 0) or 0)
    pending = state.get("pending_user_choice") or {}
    is_opt_in = pending.get("kind") == "opt_in"

    # Last 3 (tutor, student) pairs for grounding. Skip if no messages.
    msgs = list(state.get("messages") or [])[-6:]
    convo_lines: list[str] = []
    for m in msgs:
        role = (m or {}).get("role") or ""
        content = str((m or {}).get("content") or "").strip()
        if role and content:
            tag = "TUTOR" if role == "tutor" else "STUDENT"
            convo_lines.append(f"{tag}: {content[:240]}")
    convo_block = "\n".join(convo_lines) if convo_lines else "(no prior turns yet)"

    user_prompt = f"""\
PROFILE: {profile_id} — {profile_name}
PROFILE PATTERN: {profile_desc}

PHASE: {phase} (hint_level={hint_level}, opt_in_pending={is_opt_in})

LOCKED QUESTION: {locked_q or '(not yet locked)'}
LOCKED ANSWER (the term the student should reach):
{locked_a or '(not yet locked)'}
FULL ANSWER (richer textbook answer — may be a list):
{full_a or '(not provided)'}

RECENT CONVERSATION (last 3 pairs):
{convo_block}

Generate 4 intent-class-diverse student-reply suggestions per the
system rules. Output strict JSON only.
"""

    # Fire Haiku via the existing LLM client. Wrap in try/except so any
    # error degrades to "no suggestions" rather than crashing the UI.
    try:
        from conversation.llm_client import make_anthropic_client, resolve_model, beta_headers
        client = make_anthropic_client()
        model_id = resolve_model("claude-haiku-4-5-20251001")
        # Some clients accept extra_headers; guard with try/except.
        kwargs = {
            "model": model_id,
            "max_tokens": 600,
            "temperature": 0.7,
            "system": _SUGGEST_SYSTEM,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        try:
            kwargs["extra_headers"] = beta_headers()
        except Exception:
            pass
        resp = client.messages.create(**kwargs)
        raw = (resp.content[0].text or "").strip()
    except Exception as e:
        return SuggestRepliesResponse(
            thread_id=tid, profile=profile_id, suggestions=[],
            error=f"haiku_call_error: {type(e).__name__}: {str(e)[:120]}",
        )

    # Tolerant JSON extraction (LLM may wrap in ```json fences).
    parsed: dict | None = None
    try:
        # Strip code fences if present.
        text = raw
        if "```" in text:
            # Take the content between the first ``` pair.
            parts = text.split("```")
            for chunk in parts:
                chunk_str = chunk.strip()
                if chunk_str.startswith("json"):
                    chunk_str = chunk_str[4:].strip()
                if chunk_str.startswith("{") and chunk_str.endswith("}"):
                    text = chunk_str
                    break
        # Find the first '{' and last '}' as a final fallback.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end + 1])
    except Exception:
        parsed = None

    if not parsed or not isinstance(parsed.get("suggestions"), list):
        return SuggestRepliesResponse(
            thread_id=tid, profile=profile_id, suggestions=[],
            error="suggest_parse_failed",
        )

    items: list[SuggestionItem] = []
    for s in parsed["suggestions"]:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or "").strip()
        kind = str(s.get("kind") or "").strip().lower()
        if not text or not kind:
            continue
        items.append(SuggestionItem(
            text=text[:200],  # hard cap to keep bubbles compact
            kind=kind,
            color=_color_for_kind(kind),
            rationale=str(s.get("rationale") or "")[:140],
        ))

    return SuggestRepliesResponse(
        thread_id=tid, profile=profile_id, suggestions=items[:6],
    )
