"""
conversation/lifecycle_v2.py
─────────────────────────────
Session-lifecycle graph nodes that V2 inherits from V1 (rapport opener,
memory-update close) plus the routing edges that wire the LangGraph.

Origin: ported from V1 conversation/nodes.py + conversation/edges.py
during D1 (V1 → V2 consolidation). V1 nodes that V2 has its own
implementation of (`dean_node` → `dean_node_v2`, `assessment_node` →
`assessment_node_v2`) are NOT here — they were deleted during the port.
The remaining nodes have no V2 equivalent and are needed by graph.py.

Nodes:
  rapport_node        — greet student, load cross-session memory, suggest topics
  memory_update_node  — flush session to mem0/SQLite, run close-LLM, clear state

Edges (routing logic):
  after_rapport       — rapport → dean / assessment / memory_update
  after_dean          — dean → assessment / memory_update / END
  after_assessment    — assessment → memory_update / END

Helpers retained because rapport_node / memory_update_node use them:
  _derive_close_reason       — picks close_reason from state signals
  _draft_close_message       — runs Teacher draft in mode="close" (uses TeacherV2)
  _persist_session_end_to_sqlite — L21 SQLiteStore session row + mastery upsert
  _session_topic_name, _upsert_weak_topic, _latest_student_message — utilities

V1 callsites that have been refactored away during the port:
  - rapport_node previously called V1 `teacher.draft_rapport(...)`; now
    builds a `TurnPlan(mode="rapport")` and calls `TeacherV2.draft(...)`.
  - `from conversation.teacher import fire_activity` callsites were
    redirected to `conversation.streaming.fire_activity` (callback
    registry now lives in `conversation/streaming.py`).
"""

import json
from datetime import datetime
from pathlib import Path
from langgraph.graph import END
from conversation.state import TutorState
from conversation.summarizer import maybe_summarize
from retrieval.topic_suggester import TopicSuggester
from config import cfg

_topic_suggester = TopicSuggester()


def rapport_node(state: TutorState, teacher, memory_manager) -> dict:
    """
    Phase 1 — Rapport.

    - Loads cross-session memory via memory_manager.load(student_id) (mem0/Qdrant).
      Returning students get a brief reference to one prior topic; new students
      get a fresh greeting.
    - weak_topics is still empty here (legacy slot reserved for future
      knowledge-tracing-derived weak concepts).
    - TopicSuggester provides initial topic suggestions from textbook_structure.json.
    - Teacher generates personalized greeting.
    - Transition phase to "tutoring".

    Note: teacher not dean — rapport never uses Dean.
    """
    # Only run once — skip if already past rapport phase
    if state.get("phase") != "rapport":
        return {}

    # Legacy slot — kept for knowledge-tracing wiring (D.3).
    weak_topics: list[dict] = []

    # Pull cross-session context from SQL (per L1 — session_summary +
    # open_thread now live in the sessions table, not mem0).
    #
    # Two derived signals fold into past_memories for the rapport prompt:
    #   1. "recent_summary" — most recent COMPLETED session's locked topic
    #      (sessions.ended_at IS NOT NULL AND status='completed')
    #   2. "open_thread"    — any session left unresolved
    #      (ended_at IS NULL OR status IN
    #         ('abandoned_no_lock','ended_off_domain','ended_turn_limit'))
    # Both are formatted as memory-shaped dicts so draft_rapport treats
    # them identically to a mem0 entry. Misconception + learning_style
    # narratives stay in mem0 (per L1) — those are tutor-internal,
    # NOT material for an opener.
    student_id = state.get("student_id", "") or ""
    memory_enabled = bool(state.get("memory_enabled", True))
    past_memories: list[dict] = []
    weak_subsections: list[dict] = []
    if student_id and memory_enabled:
        from conversation.streaming import fire_activity as _fa_rapport
        _fa_rapport(
            "Loading your previous session memory",
            detail=(
                "Reading SQLite (recent sessions, weak subsections, open threads) "
                "and mem0 (cross-session memory: misconceptions, learning style "
                "cues) to personalize the greeting."
            ),
        )
        try:
            from memory.sqlite_store import SQLiteStore
            store = SQLiteStore()
            # Most recent completed session — surface as "session summary"
            recent = store.list_sessions(student_id, limit=1, completed_only=True)
            for s in recent:
                topic = (
                    s.get("locked_subsection_path")
                    or s.get("locked_topic_path")
                    or ""
                )
                if not topic:
                    continue
                tier = s.get("mastery_tier") or "not_assessed"
                past_memories.append({
                    "memory": (
                        f"[Recent session] Last covered: {topic}. "
                        f"Mastery tier: {tier}. "
                        f"Reach: {'yes' if s.get('reach_status') else 'partial/no'}."
                    )
                })
            # Open threads — sessions with no ended_at OR unresolved-status
            open_sessions = store.list_sessions(
                student_id,
                limit=3,
                status=("abandoned_no_lock", "ended_off_domain", "ended_turn_limit"),
            )
            in_progress = store.list_sessions(student_id, limit=3, status="in_progress")
            for s in (in_progress + open_sessions)[:3]:
                topic = (
                    s.get("locked_subsection_path")
                    or s.get("locked_topic_path")
                    or ""
                )
                if not topic:
                    continue
                past_memories.append({
                    "memory": (
                        f"[Open thread] Student was working on: {topic}. "
                        f"Status: {s.get('status', 'in_progress')}. "
                        f"Resume this topic if helpful."
                    )
                })
        except Exception:
            # SQL unreachable — degrade silently. Cold-start opener still works.
            pass

        # D.3: pull the student's weakest subsections so the rapport
        # opener can reference one *quantitatively* (not just "we worked
        # on X last time" but "X is at 32% mastery, want to revisit?").
        # Synthesize as additional bullets in past_session_memories so
        # the existing prompt rules apply unchanged — the LLM can still
        # only reference ONE item, no recap, no list.
        try:
            from memory.mastery_store import MasteryStore
            ms = MasteryStore()
            weak_subsections = ms.weak_subsections(
                student_id, threshold=0.5, limit=2
            )
            for w in weak_subsections:
                sub = w.get("subsection_title") or "?"
                pct = int(round(float(w.get("mastery", 0.0)) * 100))
                ch = w.get("chapter_num") or "?"
                outcome = w.get("last_outcome") or ""
                # Format as a "memory-shaped" dict so draft_rapport's
                # existing field-resolution path treats it identically
                # to a mem0 entry. The "Mastery cue:" prefix gives the
                # LLM a hook to recognize it as a quantitative signal.
                past_memories.append({
                    "memory": (
                        f"Mastery cue: {sub} (Ch{ch}) is at {pct}% mastery "
                        f"(last outcome: {outcome or 'unknown'}). The "
                        f"student would benefit from revisiting this "
                        f"subsection."
                    )
                })
        except Exception:
            weak_subsections = []

    # Initial suggestions: weak topics first for returning students,
    # explore picks for fresh students. suggest_for_student gracefully
    # falls back to plain suggest() when there's no mastery data.
    if student_id and memory_enabled:
        try:
            from memory.mastery_store import MasteryStore
            initial_suggestions = _topic_suggester.suggest_for_student(
                MasteryStore(), student_id, n=6
            )
        except Exception:
            initial_suggestions = _topic_suggester.suggest(n=6)
    else:
        initial_suggestions = _topic_suggester.suggest(n=6)

    # M4 (B6) — when the session was prelocked from My Mastery, the rapport
    # opener should ACKNOWLEDGE the subsection by name instead of asking
    # "what topic?" and then auto-injecting one. Skip the generic LLM
    # rapport call entirely (saves 1 Sonnet call) and emit a short
    # deterministic acknowledgment. This is interface chrome (the cards
    # below carry the user's choice) — not a templated tutor reply per M-FB.
    locked_for_rapport = state.get("locked_topic") or {}
    prelocked_sub = str(locked_for_rapport.get("subsection") or "").strip()
    if prelocked_sub:
        client_hour_val = state.get("client_hour")
        try:
            hr = int(client_hour_val) if client_hour_val is not None else datetime.now().hour
        except (TypeError, ValueError):
            hr = datetime.now().hour
        if hr < 12:
            tod = "morning"
        elif hr < 17:
            tod = "afternoon"
        else:
            tod = "evening"
        greeting = (
            f"Good {tod}. Picking up on {prelocked_sub} — "
            f"pick how you'd like to start below."
        )
    else:
        # V2 rapport draft via TeacherV2(mode="rapport"). Replaces the V1
        # `teacher.draft_rapport(...)` call. The V2 prompt template
        # (teacher_v2.py:79) reads time_of_day from TeacherPromptInputs and
        # prior-topic context from TurnPlan.carryover_notes, so V1's
        # past_session_memories list is flattened into a bulleted string.
        from conversation.teacher_v2 import TeacherV2, TeacherPromptInputs
        from conversation.turn_plan import TurnPlan
        from conversation.llm_client import make_anthropic_client, resolve_model
        from config import cfg as _cfg

        # Resolve time_of_day from client_hour (frontend) with server-time fallback.
        client_hour_val = state.get("client_hour")
        try:
            hr = int(client_hour_val) if client_hour_val is not None else datetime.now().hour
        except (TypeError, ValueError):
            hr = datetime.now().hour
        if hr < 12:
            tod = "morning"
        elif hr < 17:
            tod = "afternoon"
        else:
            tod = "evening"

        # Flatten past_session_memories list → a bulleted string for
        # carryover_notes. Cap at 8 entries to keep the prompt compact.
        if past_memories:
            bullets: list[str] = []
            for m in past_memories[:8]:
                if isinstance(m, dict):
                    text = (
                        m.get("memory")
                        or m.get("data")
                        or m.get("text")
                        or ""
                    )
                else:
                    text = str(m)
                text = str(text).strip()
                if text:
                    bullets.append(f"  - {text}")
            carryover_notes = "\n".join(bullets) if bullets else "No previous history — new student."
        else:
            carryover_notes = "No previous history — new student."

        plan = TurnPlan(
            scenario="rapport_open",
            hint_text="",
            mode="rapport",
            tone="encouraging",  # closest TONES value to "warm" for the rapport opener
            forbidden_terms=[],
            permitted_terms=[],
            shape_spec={"max_sentences": 4, "exactly_one_question": False},
            carryover_notes=carryover_notes,
        )
        inputs = TeacherPromptInputs(
            chunks=[],
            history=state.get("messages", []),
            locked_subsection="",
            locked_question="",
            domain_name=getattr(_cfg.domain, "name", "this subject"),
            domain_short=getattr(_cfg.domain, "short", "subject"),
            student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
            time_of_day=tod,
        )
        client = make_anthropic_client()
        teacher_v2 = TeacherV2(client, model=resolve_model(_cfg.models.teacher))
        from conversation.streaming import fire_activity as _fa_greet
        _fa_greet(
            "Drafting your greeting",
            detail=(
                f"Teacher generating a warm rapport opener "
                f"(mode=rapport, time_of_day={tod}). Streaming via Sonnet. "
                + (f"Carrying {len(past_memories)} prior-session memory item(s)."
                   if past_memories else "Cold-start (no prior memory).")
            ),
        )
        draft = teacher_v2.draft(plan, inputs)
        greeting = (draft.text or "").strip()

    messages = list(state.get("messages", []))
    messages.append({"role": "tutor", "content": greeting, "phase": "rapport"})

    return {
        "weak_topics": weak_topics,
        "initial_suggestions": initial_suggestions,
        "messages": messages,
        "phase": "tutoring",
    }






# M1 — close-reason buckets. Drives both the close-LLM tone AND the
# save/no-save decision at memory_update_node.
_NO_SAVE_REASONS = {"exit_intent", "off_domain_strike"}
_VALID_CLOSE_REASONS = {
    "reach_full", "reach_skipped", "clinical_cap",
    "hints_exhausted", "tutoring_cap",
    "off_domain_strike", "exit_intent",
}


def _derive_close_reason(state: TutorState) -> str:
    """M1 — pick the close reason from state signals at session end.

    Priority order:
      1. exit_intent_pending or session_ended_off_domain → explicit triggers
      2. clinical_completed + clinical reach → reach_full
      3. clinical_completed + cap hit → clinical_cap
      4. tutoring student_reached_answer + opt_in_no → reach_skipped
      5. hint_level > max_hints → hints_exhausted
      6. turn_count >= max_turns → tutoring_cap
      7. else → tutoring_cap (defensive)
    """
    # Explicit triggers from upstream
    if state.get("exit_intent_pending"):
        return "exit_intent"
    if state.get("session_ended_off_domain"):
        return "off_domain_strike"

    # Clinical phase outcomes
    if state.get("clinical_completed"):
        clinical_state = state.get("clinical_state") or ""
        if clinical_state in {"correct", "partial_correct"}:
            return "reach_full"
        return "clinical_cap"

    # Tutoring outcomes
    if state.get("student_reached_answer") and state.get("clinical_opt_in") is False:
        return "reach_skipped"

    hint_level = int(state.get("hint_level", 0) or 0)
    max_hints = int(state.get("max_hints", 0) or 0)
    if hint_level > max_hints:
        return "hints_exhausted"

    turn_count = int(state.get("turn_count", 0) or 0)
    max_turns = int(state.get("max_turns", 0) or 0)
    if max_turns and turn_count >= max_turns:
        return "tutoring_cap"

    return "tutoring_cap"


def _draft_close_message(state: TutorState, close_reason: str) -> dict:
    """M1 — single Sonnet close call. Returns dict with message + takeaways.

    Output shape (from the close-mode prompt JSON):
      {
        "message":      "<tutor goodbye>",
        "demonstrated": "<short>",
        "needs_work":   "<short>",
      }

    Empty fields on LLM failure (no templated tutor text per M-FB).
    """
    import json as _json
    from conversation.teacher_v2 import TeacherV2, TeacherPromptInputs
    from conversation.turn_plan import TurnPlan
    from conversation.llm_client import make_anthropic_client, resolve_model
    from conversation.streaming import fire_activity
    from config import cfg as _cfg

    fire_activity(
        "Reflecting on your progress",
        detail=(
            "Drafting the close-LLM message + computing 'demonstrated' / "
            "'needs_work' takeaways from the full session history. Sees "
            "engagement metrics (low-effort, help-abuse, hint advances) so "
            "judgment is grounded in actual student behavior, not tutor scaffolding."
        ),
    )

    locked = state.get("locked_topic") or (state.get("debug") or {}).get("locked_topic_snapshot") or {}
    # 2026-05-05: build an engagement-metrics line so the close-LLM has hard
    # signals alongside the history. Without this, it mines patterns from raw
    # messages and over-credits the student for tutor scaffolding text.
    _debug_for_metrics = state.get("debug") or {}
    _events = _debug_for_metrics.get("system_events") or []
    _hint_advances = sum(1 for e in _events if (e or {}).get("kind") == "hint_advance")
    _hint_advance_triggers = sorted({
        (e.get("payload") or {}).get("trigger", "") for e in _events
        if (e or {}).get("kind") == "hint_advance"
    } - {""})
    _student_msgs = [
        m for m in (state.get("messages") or [])
        if (m or {}).get("role") == "student"
    ]
    metrics_line = (
        f"close_reason: {close_reason}\n"
        f"final_hint_level: {int(state.get('hint_level', 0) or 0)}/3\n"
        f"hint_advances_fired: {_hint_advances}"
        + (f" (triggers: {','.join(_hint_advance_triggers)})" if _hint_advance_triggers else "")
        + f"\nfinal_help_abuse_count: {int(state.get('help_abuse_count', 0) or 0)}/4\n"
        f"final_consecutive_low_effort: {int(state.get('consecutive_low_effort_count', 0) or 0)}/4\n"
        f"total_low_effort_turns: {int(state.get('total_low_effort_turns', 0) or 0)}\n"
        f"total_off_topic_turns: {int(state.get('total_off_topic_turns', 0) or 0)}\n"
        f"student_reached_answer: {bool(state.get('student_reached_answer', False))}\n"
        f"student_message_count: {len(_student_msgs)}\n"
    )
    plan = TurnPlan(
        scenario=f"close:{close_reason}",
        hint_text="",
        mode="close",
        tone="neutral",
        forbidden_terms=[],
        permitted_terms=[],
        # close JSON output is unconstrained sentences; loose shape
        shape_spec={"max_sentences": 6, "exactly_one_question": False},
        carryover_notes=metrics_line,
    )
    # 2026-05-05: pass snapshots + system_events so the close-LLM sees
    # help_abuse_count / consecutive_low_effort / hint_advance events
    # and can ground demonstrated/needs_work in actual engagement signals
    # rather than hallucinating credit from tutor scaffolding text.
    _debug_obj = state.get("debug") or {}
    inputs = TeacherPromptInputs(
        chunks=[],
        history=state.get("messages", []),
        locked_subsection=str(locked.get("subsection") or ""),
        locked_question=str(state.get("locked_question") or ""),
        domain_name=getattr(_cfg.domain, "name", "this subject"),
        domain_short=getattr(_cfg.domain, "short", "subject"),
        student_descriptor=getattr(_cfg.domain, "student_descriptor", "student"),
        snapshots=_debug_obj.get("per_turn_snapshots", []) or [],
        system_events=_debug_obj.get("system_events", []) or [],
    )
    client = make_anthropic_client()
    teacher_v2 = TeacherV2(client, model=resolve_model(_cfg.models.teacher))

    # Single attempt with internal silent retry — M-FB pattern.
    text = ""
    error = ""
    for attempt in (1, 2):
        try:
            draft = teacher_v2.draft(plan, inputs)
            text = (draft.text or "").strip()
            if text:
                error = ""
                break
            error = "empty_text"
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:120]}"

    state["debug"]["turn_trace"].append({
        "wrapper": "teacher_v2.close_draft",
        "close_reason": close_reason,
        "attempts": attempt,
        "error": error,
        "text_len": len(text),
    })
    if not text:
        return {"message": "", "demonstrated": "", "needs_work": "", "_error": error}

    # Strip ```json fences if present
    s = text
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        parsed = _json.loads(s)
    except _json.JSONDecodeError:
        # Fallback: try to extract first JSON object substring
        import re as _re
        m = _re.search(r"\{.*\}", s, _re.DOTALL)
        parsed = None
        if m:
            try:
                parsed = _json.loads(m.group(0))
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        # If JSON parse fails, treat the whole text as the message and
        # leave takeaways empty (no fake/templated takeaways).
        return {
            "message": text,
            "demonstrated": "",
            "needs_work": "",
            "_error": "json_parse_fail",
        }
    return {
        "message": str(parsed.get("message") or "").strip(),
        "demonstrated": str(parsed.get("demonstrated") or "").strip(),
        "needs_work": str(parsed.get("needs_work") or "").strip(),
        "_error": "",
    }


def _write_transcript_snapshot(state: TutorState) -> str:
    """Persist the session's full message log as a per-turn JSON file.

    2026-05-05: D1 cleanup deleted conversation/nodes.py which contained
    _log_conversation — the function that wrote per-turn snapshot files
    at data/artifacts/conversations/{student_id}_{thread_suffix}_turn_N.json.
    Without it, the V2 stack never persists transcripts and the
    /api/sessions/{thread_id}/transcript endpoint always returns empty,
    even though the session metadata (mastery, takeaways) saves correctly.

    Re-implements the same naming convention so the existing endpoint
    keeps working without further changes.

    Returns a status string for turn_trace.
    """
    import json
    from pathlib import Path
    try:
        from config import cfg
    except Exception as e:
        return f"config_import_error: {type(e).__name__}: {str(e)[:80]}"

    thread_id = (state.get("thread_id") or "").strip()
    student_id = (state.get("student_id") or "").strip()
    if not thread_id or not student_id:
        return "skipped_no_thread_or_student"

    artifacts_dir = Path(cfg.paths.artifacts) / "conversations"
    if not artifacts_dir.is_absolute():
        # Mirror the resolution logic in backend/api/sessions.py:_resolve_transcript_path
        artifacts_dir = (
            Path(__file__).resolve().parent.parent
            / cfg.paths.artifacts
            / "conversations"
        )
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"mkdir_error: {type(e).__name__}: {str(e)[:80]}"

    # File naming: {student_id}_{thread_suffix}_turn_N.json — matches
    # the glob in /api/sessions/{thread_id}/transcript.
    thread_suffix = thread_id.split("_", 1)[-1] if "_" in thread_id else thread_id
    messages = list(state.get("messages") or [])
    turn_n = len([m for m in messages if (m or {}).get("role") == "student"])
    out_path = artifacts_dir / f"{student_id}_{thread_suffix}_turn_{turn_n}.json"

    payload = {
        "student_id": student_id,
        "thread_id": thread_id,
        "phase": state.get("phase"),
        "close_reason": state.get("close_reason"),
        "session_ended": bool(state.get("session_ended", False)),
        "locked_topic": state.get("locked_topic") or {},
        "locked_question": state.get("locked_question") or "",
        "locked_answer": state.get("locked_answer") or "",
        "hint_level": int(state.get("hint_level", 0) or 0),
        "messages": messages,
    }
    try:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return f"wrote {out_path.name} ({len(messages)} msgs)"
    except Exception as e:
        return f"write_error: {type(e).__name__}: {str(e)[:80]}"



def memory_update_node(state: TutorState, dean, memory_manager) -> dict:
    """
    Phase 4 — Memory Update + Close.

    M1 redesign:
      1. Determine close_reason from state signals
      2. Draft the LLM close message (structured JSON)
      3. Append close message to chat history
      4. If close_reason is no-save (exit_intent, off_domain_strike):
         skip mem0 / mastery / sqlite writes
      5. Otherwise: full save + record key_takeaways from the close JSON
      6. Stamp session_ended=True so frontend disables input

    No templated tutor fallbacks (M-FB). On close-LLM failure, the message
    is empty (frontend renders an error card).
    """
    state["debug"]["current_node"] = "memory_update_node"
    from conversation.streaming import fire_activity

    # ── M1: derive close reason + draft close message ─────────────────────
    close_reason = state.get("close_reason") or _derive_close_reason(state)
    state["close_reason"] = close_reason
    close_payload = _draft_close_message(state, close_reason)
    close_message = close_payload.get("message") or ""
    takeaways = {
        "demonstrated": close_payload.get("demonstrated") or "",
        "needs_work": close_payload.get("needs_work") or "",
        "close_reason": close_reason,
    }

    # Append close message to chat (only if non-empty — avoid blank
    # tutor bubble; frontend will surface the error card instead).
    new_messages = list(state.get("messages", []) or [])
    if close_message:
        new_messages.append({
            "role": "tutor",
            "content": close_message,
            "phase": "memory_update",
            "metadata": {
                "mode": "close",
                "close_reason": close_reason,
                "source": "memory_update_node",
            },
        })
    else:
        # M-FB: surface error card payload — frontend renders distinct UI
        new_messages.append({
            "role": "system",
            "content": "",
            "phase": "memory_update",
            "metadata": {
                "kind": "error_card",
                "component": "Teacher.close_draft",
                "error_class": "EmptyOrParseFail",
                "message": close_payload.get("_error") or "close draft empty",
                "retry_handler": "close",
            },
        })

    # 2026-05-05: persist the transcript regardless of save/no-save bucket.
    # The mastery/mem0/sqlite writes are conditional on close_reason, but the
    # student should always be able to review what was said in the session.
    state["messages"] = new_messages  # ensure close message is in the snapshot
    transcript_status = _write_transcript_snapshot(state)
    state["debug"]["turn_trace"].append({
        "wrapper": "memory_update_node.transcript_snapshot",
        "result": transcript_status,
    })

    # ── M1: no-save bucket → skip all persistence ─────────────────────────
    if close_reason in _NO_SAVE_REASONS:
        state["debug"]["turn_trace"].append({
            "wrapper": "memory_update_node.no_save",
            "close_reason": close_reason,
            "result": "skipped_save_per_M1_design",
        })
        return {
            "phase": "memory_update",
            "messages": new_messages,
            "session_ended": True,
            "close_reason": close_reason,
            "exit_intent_pending": False,
            # Bug #2a: clear stale pending_user_choice (e.g. anchor_pick)
            # so the frontend doesn't keep showing cards over the close UI.
            "pending_user_choice": {},
            "debug": state["debug"],
        }

    # ── Save bucket — existing mem0 + mastery + sqlite flow ───────────────
    fire_activity(
        "Saving session memory",
        detail=(
            "Flushing student-specific session summary to mem0 (long-term "
            "cross-session memory: learning style cues, misconceptions, "
            "weak topics). Skipped when close_reason is exit_intent / "
            "off_domain_strike (no-save bucket)."
        ),
    )
    student_id = state.get("student_id", "") or ""
    flush_status = "skipped_no_student_id"
    flushed = False
    if student_id:
        try:
            flushed = memory_manager.flush(student_id, state, summary_text="")
            flush_status = getattr(memory_manager, "last_flush_status", "ok")
        except Exception as e:
            flush_status = f"error: {type(e).__name__}: {str(e)[:80]}"
    state["debug"]["turn_trace"].append({
        "wrapper": "memory_manager.flush",
        "result": flush_status,
        "flushed": flushed,
    })

    # D.3: per-concept knowledge tracing via LLM-based scoring.
    # Updates two signals per session for the locked subsection:
    #   mastery     — point estimate (BKT-style P(L))
    #   confidence  — coverage estimate (how thoroughly probed)
    # See memory/mastery_store.py module docstring for the literature
    # grounding (extends Corbett & Anderson 1995 BKT).
    #
    # No heuristic fallback by design: the LLM is the model. If the
    # call fails after retries, this session's mastery simply doesn't
    # update (logged in turn_trace) — degrades visibly rather than
    # silently substituting a worse signal.
    mastery_status = "skipped"
    judgment: dict | None = None  # captured for SQLite dual-write below
    if student_id:
        try:
            from memory.mastery_store import MasteryStore, score_session_llm
            from config import cfg as _cfg
            import anthropic
            # Same sticky-snapshot fallback as MemoryManager._topic_metadata
            # — state["locked_topic"] can end up None at this node even
            # when a topic was locked earlier; debug.locked_topic_snapshot
            # is the dean's write-once record that survives merges.
            locked = state.get("locked_topic") or {}
            if not locked:
                locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}
            locked_path = str(locked.get("path", "") or "")
            if locked_path:
                fire_activity(
                    "Scoring concept mastery",
                    detail=(
                        "LLM scoring the session via Bayesian Knowledge Tracing "
                        "(extends Corbett & Anderson 1995 BKT). Produces mastery "
                        "+ confidence + tier — written to subsection_mastery in "
                        "SQLite and surfaced in My Mastery."
                    ),
                )
                ms = MasteryStore()
                priors = ms.recent_rationales(student_id, locked_path, limit=3)
                from conversation.llm_client import make_anthropic_client, resolve_model
                client = make_anthropic_client()
                judgment = score_session_llm(
                    state,
                    prior_rationales=priors,
                    client=client,
                    model=resolve_model(_cfg.models.dean),
                )
                if judgment is None:
                    mastery_status = "llm_scorer_failed_skipped_update"
                else:
                    outcome = (
                        "reached" if state.get("student_reached_answer")
                        else "not_reached"
                    )
                    rec = ms.update(
                        student_id=student_id,
                        subsection_path=locked_path,
                        mastery_score=float(judgment["mastery"]),
                        confidence_score=float(judgment["confidence"]),
                        outcome=outcome,
                        rationale=str(judgment.get("rationale", "")),
                    )
                    mastery_status = (
                        f"updated mastery={rec.get('mastery')} "
                        f"confidence={rec.get('confidence')} "
                        f"sessions={rec.get('sessions')}"
                    )
            else:
                mastery_status = "skipped_no_locked_topic"
        except Exception as e:
            mastery_status = f"error: {type(e).__name__}: {str(e)[:80]}"
    state["debug"]["turn_trace"].append({
        "wrapper": "mastery_store.update",
        "result": mastery_status,
    })

    # L21 + L1/L2/L3: dual-write the session-end row + subsection_mastery
    # into the per-domain SQLite store. This populates the new data layer
    # alongside the legacy JSON MasteryStore. Reads switch to SQLite in
    # the next commit (track 1.9). Best-effort — never blocks return.
    # M1 — also writes key_takeaways from the close-LLM JSON.
    state["_close_takeaways_pending"] = takeaways
    sqlite_status = _persist_session_end_to_sqlite(state, judgment)
    state.pop("_close_takeaways_pending", None)
    state["debug"]["turn_trace"].append({
        "wrapper": "sqlite_store.session_end",
        "result": sqlite_status,
    })

    return {
        "phase": "memory_update",
        "messages": new_messages,
        "session_ended": True,
        "close_reason": close_reason,
        "exit_intent_pending": False,
        # Bug #2a: clear stale pending_user_choice on save-bucket close too.
        "pending_user_choice": {},
        "debug": state["debug"],
    }






def _latest_student_message(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "student":
            return str(msg.get("content", "")).strip()
    return ""


_OPT_IN_YES_PREFIXES = (
    "yes", "yeah", "yep", "yup", "sure", "ok ", "okay", "alright",
    "go ahead", "let's", "lets go", "i'll try", "ill try", "i would",
    "absolutely", "definitely", "please", "ready", "sounds good",
)
_OPT_IN_NO_PREFIXES = (
    "no", "nope", "nah", "skip", "pass", "not now", "maybe later",
    "i'm good", "im good", "not really", "not today",
)


_BANNED_LEAD_PREFIXES = (
    "i can see", "i notice", "i hear you", "that's okay", "that is okay",
    "i understand", "i see that", "it sounds like", "i hear that",
)






def _session_topic_name(state: TutorState) -> str:
    """
    Canonical TOC-node name for this session — used for weak_topics and
    memory write-back. Prefers the locked TOC entry (set by dean.run_turn
    after TopicMatcher resolves the student's request) so we never persist
    a raw free-text selection or a menu-option label.
    Returns "" when the session was never grounded to a TOC node; callers
    should skip weak-topic updates in that case (P0.6 grading guard).
    """
    locked = state.get("locked_topic") or {}
    node = (locked.get("subsection") or locked.get("section") or locked.get("chapter") or "").strip()
    if node:
        return node[:120]
    # No TOC lock — fall back to topic_selection only when it looks like a
    # canonical label (short, no numeric prefix). Otherwise return empty so
    # the caller treats the session as ungraded.
    selected = str(state.get("topic_selection", "") or "").strip()
    if selected and len(selected.split()) <= 10 and not selected[:1].isdigit():
        return selected[:120]
    return ""


def _upsert_weak_topic(weak_topics: list[dict], topic_name: str, difficulty: str = "moderate", bump: int = 1) -> list[dict]:
    if not topic_name:
        return weak_topics
    out = list(weak_topics or [])
    key = " ".join(topic_name.strip().lower().split())
    for wt in out:
        wt_topic = " ".join(str(wt.get("topic", "")).strip().lower().split())
        if wt_topic == key:
            wt["failure_count"] = int(wt.get("failure_count", 0)) + int(max(bump, 1))
            if difficulty and wt.get("difficulty") != "hard":
                wt["difficulty"] = difficulty
            return out
    out.append({
        "topic": topic_name,
        "difficulty": difficulty or "moderate",
        "failure_count": int(max(bump, 1)),
    })
    return out

def _persist_session_end_to_sqlite(
    state: TutorState,
    judgment: dict | None,
) -> str:
    """Update the L21 SQLite session row + upsert subsection_mastery (per L1, L2, L3).

    Coexists with the legacy MasteryStore JSON write — dual-write phase. The
    next commit (track 1.9) flips reads to SQLite and drops the JSON path.

    Returns a short status string for turn_trace.
    """
    thread_id = state.get("thread_id") or ""
    student_id = state.get("student_id") or ""
    if not thread_id or not student_id:
        return "skipped_no_thread_or_student"

    try:
        from memory.sqlite_store import (
            SQLiteStore,
            normalize_subsection_path,
            score_to_tier,
        )
    except Exception as e:
        return f"sqlite_import_error: {type(e).__name__}: {str(e)[:80]}"

    # --- Resolve final session metadata from state ---
    locked = state.get("locked_topic") or {}
    if not locked:
        locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}
    legacy_path = str(locked.get("path") or "")
    canonical_path = normalize_subsection_path(legacy_path) if legacy_path else ""
    locked_question = state.get("locked_question") or None
    locked_answer = state.get("locked_answer") or None
    full_answer = state.get("full_answer") or None

    # Determine status (per L21 enum). Heuristics — L43-L62 tutor refactor
    # will tighten these with explicit termination signals.
    reach = state.get("student_reached_answer")
    turn_count = len([m for m in (state.get("messages") or []) if m.get("role") == "student"])
    if not canonical_path:
        status = "abandoned_no_lock"
    elif reach is True:
        status = "completed"
    elif state.get("session_ended_off_domain"):
        status = "ended_off_domain"
    elif state.get("session_ended_by_student"):
        status = "ended_by_student"
    elif turn_count >= 25:
        status = "ended_turn_limit"
    else:
        status = "completed"

    # Mastery tier projection from the LLM judgment (if scoring ran).
    score = float(judgment["mastery"]) if judgment and "mastery" in judgment else None
    tier = score_to_tier(score) if score is not None else "not_assessed"

    # --- Write the session row + upsert subsection_mastery ---
    try:
        store = SQLiteStore()
        store.update_session(
            thread_id,
            ended_at=None and None,  # sentinel: end_session sets ended_at via utc_now
        )
        # M1 — pull takeaways from the close LLM JSON (set by memory_update_node)
        takeaways_payload = state.get("_close_takeaways_pending") or {}
        end_kwargs = dict(
            status=status,
            locked_topic_path=canonical_path or None,
            locked_subsection_path=canonical_path or None,
            locked_question=locked_question,
            locked_answer=locked_answer,
            full_answer=full_answer,
            reach_status=bool(reach) if reach is not None else None,
            mastery_tier=tier,
            core_mastery_tier=tier,
            clinical_mastery_tier="not_assessed",
            core_score=score,
            clinical_score=None,
            hint_level_final=int(state.get("hint_level") or 0),
            turn_count=turn_count,
        )
        if takeaways_payload and (
            takeaways_payload.get("demonstrated") or takeaways_payload.get("needs_work")
        ):
            end_kwargs["key_takeaways"] = takeaways_payload
        store.end_session(thread_id, **end_kwargs)
        if canonical_path and score is not None:
            outcome = "reached" if reach else (
                "partial" if (state.get("student_reach_coverage") or 0.0) > 0 else "not_reached"
            )
            store.upsert_subsection_mastery(
                student_id,
                canonical_path,
                fresh_score=score,
                outcome=outcome,
            )
        return f"sqlite_session_end ok status={status} score={score} path_set={bool(canonical_path)}"
    except Exception as e:
        return f"sqlite_write_error: {type(e).__name__}: {str(e)[:120]}"



# ─────────────────────────────────────────────────────────────────────
# Routing edges (ported from V1 conversation/edges.py during D1).
# Pure logic — no agent calls. Each takes TutorState and returns the
# next node name (or langgraph.END).
# ─────────────────────────────────────────────────────────────────────


def after_rapport(state: TutorState) -> str:
    """Route after rapport_node (which skips if phase != 'rapport').
    - If phase already memory_update (M1 explicit-exit fired between turns) →
      memory_update_node so close fires immediately, no Dean call.
    - If in assessment phase waiting for student input (opt-in or clinical) → assessment_node
    - Otherwise → dean_node
    """
    if state.get("phase") == "memory_update":
        return "memory_update_node"
    if state.get("phase") == "assessment" and state.get("assessment_turn") in (1, 2):
        return "assessment_node"
    return "dean_node"


def after_dean(state: TutorState) -> str:
    """
    After Dean delivers a response:
    - Move to assessment if student answered, hints exhausted, or turn limit hit.
    - When assessment_style == "none", skip assessment entirely and go
      straight to memory update.
    - Otherwise END — Streamlit will call invoke() again on next student message.

    IMPORTANT (revised 2026-05-01): hint_level > max_hints DOES route to
    assessment. The earlier attempt to let tutoring continue past hint 3
    failed because the Dean's early-exit at hint-exhaustion (dean.py:~1792)
    skips Teacher draft entirely — so the session would loop with no new
    tutor message. The architecture assumes hint exhaustion = session-end
    trigger; honour that.
    """
    assessment_style = getattr(cfg.session, "assessment_style", "clinical")

    if state.get("phase") == "memory_update":
        return "memory_update_node"
    if state.get("student_reached_answer"):
        if assessment_style == "none":
            return "memory_update_node"
        return "assessment_node"
    # M1 — hint-exhausted goes STRAIGHT to memory_update with honest_close
    # tone. Asking opt_in for a clinical bonus when the student didn't even
    # reach the core answer is bad UX (and produced wrong reach_close text).
    if int(state.get("hint_level", 0) or 0) > int(state.get("max_hints", 0) or 0):
        return "memory_update_node"
    if int(state.get("turn_count", 0) or 0) >= int(state.get("max_turns", 0) or 0):
        return "assessment_node"
    return END


def after_assessment(state: TutorState) -> str:
    """
    After assessment_node runs:
    - assessment_turn in (1, 2): waiting for student's answer (END).
    - assessment_turn == 3: assessment complete — move to memory update.
    """
    if int(state.get("assessment_turn", 0) or 0) == 3:
        return "memory_update_node"
    return END
