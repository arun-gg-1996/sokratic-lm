"""
ui/app.py
----------
Streamlit demo interface for Socratic-OT.
"""

import sys
import time
import json
import random
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import uuid
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")
import streamlit as st
import streamlit.components.v1 as components
from config import cfg
from conversation.state import initial_state
from conversation.graph import build_graph
from retrieval.retriever import MockRetriever
from memory.memory_manager import MemoryManager

SOKRATIC_FONT_STACK = '"Iowan Old Style", "Palatino", "Times New Roman", serif'

# Load bot avatar — Streamlit requires bytes or PIL.Image, not a file path string
def _load_avatar():
    from PIL import Image
    candidates = [
        Path(__file__).parent / "assets" / "sokratic_bot_icon.png",
        Path(__file__).parent.parent / "socrates-vector-icon-isolated-transparent-background-socrate-transparency-concept-can-be-used-web-mobile-127338010.webp",
    ]
    for p in candidates:
        if p.exists():
            try:
                return Image.open(p)
            except Exception:
                pass
    return "🎓"  # emoji fallback if no image found

AVATAR = _load_avatar()


def _init_session():
    if "graph" not in st.session_state:
        retriever = MockRetriever()
        memory_manager = MemoryManager()
        st.session_state.graph = build_graph(retriever, memory_manager)
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.state = None
        st.session_state.messages_display = []  # list of {role, content, _metrics?, _phase?}
        st.session_state.dean_stream = []        # current turn trace (right panel)
        st.session_state.all_turn_traces = []    # list of {turn, phase, trace} — all history
        st.session_state.last_animated = -1
        st.session_state.profile_id = "S1"
    if "debug_mode" not in st.session_state:
        st.session_state.debug_mode = False


def _start_new_session(profile_id: str):
    student_id = f"{profile_id}_{st.session_state.thread_id[:8]}"
    st.session_state.state = initial_state(student_id, cfg)
    st.session_state.messages_display = []
    st.session_state.dean_stream = []
    st.session_state.all_turn_traces = []
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.last_animated = -1
    st.session_state.profile_id = profile_id

    config = {"configurable": {"thread_id": st.session_state.thread_id}}
    st.session_state.state = st.session_state.graph.invoke(
        st.session_state.state, config=config
    )
    state = st.session_state.state
    turn_trace = list(state.get("debug", {}).get("turn_trace", []))
    metrics_summary = _build_metrics_summary([e for e in turn_trace if "elapsed_s" in e])
    snapshot = _build_state_snapshot(state)

    display = list(state.get("messages", []))
    first_tutor_idx = -1
    for i in reversed(range(len(display))):
        if display[i].get("role") == "tutor":
            if metrics_summary:
                display[i]["_metrics"] = metrics_summary
            display[i]["_state_snapshot"] = snapshot
            display[i]["_phase"] = state.get("phase", "rapport")
            display[i]["_turn"] = state.get("turn_count", 0)
            first_tutor_idx = i
            break
    st.session_state.messages_display = display
    st.session_state.dean_stream = turn_trace
    st.session_state.all_turn_traces = [{
        "turn": state.get("turn_count", 0),
        "phase": state.get("phase", "rapport"),
        "trace": turn_trace,
    }]
    st.session_state.last_animated = first_tutor_idx


def _send_student_message(text: str):
    if st.session_state.state is None:
        return

    state = st.session_state.state
    state["messages"].append({"role": "student", "content": text})
    state["debug"]["turn_trace"] = []

    config = {"configurable": {"thread_id": st.session_state.thread_id}}
    thinking_placeholder = st.empty()
    thinking_placeholder.markdown(_thinking_indicator_html(), unsafe_allow_html=True)
    try:
        state = st.session_state.graph.invoke(state, config=config)
    finally:
        thinking_placeholder.empty()

    st.session_state.state = state
    turn_trace = list(state.get("debug", {}).get("turn_trace", []))
    current_phase = state.get("phase", "tutoring")
    current_turn = state.get("turn_count", 0)

    # Build per-message metrics
    timed_entries = [e for e in turn_trace if "elapsed_s" in e]
    metrics_summary = _build_metrics_summary(timed_entries)
    snapshot = _build_state_snapshot(state)

    # Rebuild messages_display, tagging the last tutor message with metrics + phase
    raw_messages = list(state.get("messages", []))
    display = []
    last_tutor_idx = None
    prev_phase = None
    for i, msg in enumerate(raw_messages):
        m = dict(msg)
        # Carry forward existing metadata from previous display
        if i < len(st.session_state.messages_display):
            prev = st.session_state.messages_display[i]
            for k in ("_metrics", "_phase", "_turn", "_state_snapshot"):
                if k in prev:
                    m[k] = prev[k]
        display.append(m)
        if msg.get("role") == "tutor":
            last_tutor_idx = i

    if last_tutor_idx is not None:
        if metrics_summary:
            display[last_tutor_idx]["_metrics"] = metrics_summary
        display[last_tutor_idx]["_state_snapshot"] = snapshot
        display[last_tutor_idx]["_phase"] = current_phase
        display[last_tutor_idx]["_turn"] = current_turn

    st.session_state.messages_display = display

    # Append to all_turn_traces for right panel history
    st.session_state.all_turn_traces.append({
        "turn": current_turn,
        "phase": current_phase,
        "trace": turn_trace,
    })
    st.session_state.dean_stream = turn_trace
    st.session_state.last_animated = last_tutor_idx if last_tutor_idx is not None else -1


def _build_metrics_summary(timed_entries: list) -> dict:
    """Aggregate timing across all calls this turn."""
    if not timed_entries:
        return {}
    total_elapsed = sum(e.get("elapsed_s", 0) for e in timed_entries)
    total_in = sum(e.get("in_tok", 0) for e in timed_entries)
    total_out = sum(e.get("out_tok", 0) for e in timed_entries)
    total_cost = sum(e.get("cost_usd", 0) for e in timed_entries)
    total_cache_read = sum(e.get("cache_read", 0) for e in timed_entries)
    total_cache_write = sum(e.get("cache_write", 0) for e in timed_entries)
    calls = len(timed_entries)
    return {
        "calls": calls,
        "elapsed_s": round(total_elapsed, 2),
        "in_tok": total_in,
        "out_tok": total_out,
        "cost_usd": round(total_cost, 5),
        "cache_read": total_cache_read,
        "cache_write": total_cache_write,
        "per_call": timed_entries,
    }


def _collect_timed_entries(all_turn_traces: list[dict]) -> list[dict]:
    timed = []
    for turn_item in all_turn_traces or []:
        turn_no = turn_item.get("turn")
        phase = turn_item.get("phase")
        for entry in turn_item.get("trace", []) or []:
            if "elapsed_s" not in entry:
                continue
            e = dict(entry)
            e["_turn"] = turn_no
            e["_phase"] = phase
            timed.append(e)
    return timed


def _build_performance_summary(all_turn_traces: list[dict], debug_state: dict) -> dict:
    timed = _collect_timed_entries(all_turn_traces)
    if not timed:
        return {
            "overall": {
                "calls": 0,
                "elapsed_s_total": 0.0,
                "elapsed_s_avg": 0.0,
                "in_tokens_total": 0,
                "out_tokens_total": 0,
                "cost_usd_total": float(debug_state.get("cost_usd", 0.0)),
                "cache_read_total": 0,
                "cache_write_total": 0,
            },
            "by_wrapper": {},
            "by_phase": {},
        }

    def _agg(entries: list[dict]) -> dict:
        n = len(entries)
        elapsed = [float(e.get("elapsed_s", 0.0)) for e in entries]
        elapsed_sorted = sorted(elapsed)
        p95_idx = max(0, int(0.95 * (n - 1)))
        return {
            "calls": n,
            "elapsed_s_total": round(sum(elapsed), 3),
            "elapsed_s_avg": round(sum(elapsed) / n, 3),
            "elapsed_s_p95": round(elapsed_sorted[p95_idx], 3),
            "in_tokens_total": int(sum(int(e.get("in_tok", 0) or 0) for e in entries)),
            "out_tokens_total": int(sum(int(e.get("out_tok", 0) or 0) for e in entries)),
            "cost_usd_total": round(sum(float(e.get("cost_usd", 0.0) or 0.0) for e in entries), 6),
            "cache_read_total": int(sum(int(e.get("cache_read", 0) or 0) for e in entries)),
            "cache_write_total": int(sum(int(e.get("cache_write", 0) or 0) for e in entries)),
        }

    by_wrapper = {}
    by_phase = {}
    for e in timed:
        w = e.get("wrapper", "unknown")
        p = e.get("_phase", "unknown")
        by_wrapper.setdefault(w, []).append(e)
        by_phase.setdefault(p, []).append(e)

    overall = _agg(timed)
    overall["cost_usd_total"] = round(float(debug_state.get("cost_usd", overall["cost_usd_total"])), 6)
    return {
        "overall": overall,
        "by_wrapper": {k: _agg(v) for k, v in by_wrapper.items()},
        "by_phase": {k: _agg(v) for k, v in by_phase.items()},
    }


def _trace_for_turn(all_turn_traces: list[dict], turn_number, phase=None) -> list[dict]:
    candidates = [t for t in (all_turn_traces or []) if t.get("turn") == turn_number]
    if phase is not None:
        same_phase = [t for t in candidates if t.get("phase") == phase]
        if same_phase:
            candidates = same_phase
    if not candidates:
        return []
    return list(candidates[-1].get("trace", []) or [])


def _latest_student_before(messages_display: list[dict], idx: int) -> str:
    for j in range(idx - 1, -1, -1):
        msg = messages_display[j]
        if msg.get("role") == "student":
            return str(msg.get("content", "") or "")
    return ""


def _build_eval_turn_records(messages_display: list[dict], all_turn_traces: list[dict]) -> list[dict]:
    records = []
    for idx, msg in enumerate(messages_display or []):
        if msg.get("role") != "tutor":
            continue
        phase = msg.get("_phase")
        turn_no = msg.get("_turn")
        snapshot = msg.get("_state_snapshot", {}) or {}
        turn_trace = _trace_for_turn(all_turn_traces, turn_no, phase=phase)
        contexts = snapshot.get("retrieved_chunks_for_eval", []) or []
        student_input = _latest_student_before(messages_display, idx)
        tutor_output = msg.get("content", "")
        locked_answer = snapshot.get("locked_answer", "")
        records.append({
            "record_id": f"turn_{turn_no}_{idx}",
            "message_index": idx,
            "turn_number": turn_no,
            "phase": phase,
            "student_input": student_input,
            "tutor_output": tutor_output,
            "locked_answer": locked_answer,
            "student_state": snapshot.get("student_state"),
            "hint_level": snapshot.get("hint_level"),
            "max_hints": snapshot.get("max_hints"),
            "topic_selection": snapshot.get("topic_selection", ""),
            "core_mastery_tier": snapshot.get("core_mastery_tier", "not_assessed"),
            "clinical_mastery_tier": snapshot.get("clinical_mastery_tier", "not_assessed"),
            "mastery_tier": snapshot.get("mastery_tier", "not_assessed"),
            "metrics": msg.get("_metrics", {}) or {},
            "retrieved_contexts": contexts,
            "dean_teacher_trace": turn_trace,
            # RAGAS-friendly aliases (no mapping required).
            "ragas": {
                "user_input": student_input,
                "response": tutor_output,
                "contexts": [c.get("text", "") for c in contexts if c.get("text")],
                "reference": locked_answer,
            },
            # EULER-friendly aliases.
            "euler": {
                "student_message": student_input,
                "tutor_response": tutor_output,
                "locked_answer": locked_answer,
                "phase": phase,
            },
        })
    return records


def _build_export_coverage(payload: dict) -> dict:
    conv = payload.get("conversation", {})
    eval_data = payload.get("evaluation", {})
    perf = payload.get("performance", {})
    state = payload.get("state", {}).get("current_state", {}) or {}

    records = eval_data.get("turn_records", []) or []
    messages = conv.get("messages", []) or []
    display_msgs = conv.get("messages_display", []) or []
    all_trace = payload.get("dean", {}).get("all_turn_traces", []) or []

    has_trace_per_turn = all(len(r.get("dean_teacher_trace", [])) > 0 for r in records) if records else False
    has_contexts_per_turn = all(len(r.get("retrieved_contexts", [])) > 0 for r in records if r.get("phase") in {"tutoring", "assessment"}) if records else False
    has_perf = bool(perf.get("summary", {}).get("overall", {}).get("calls", 0) >= 0)

    return {
        "messages_present": len(messages) > 0,
        "messages_display_present": len(display_msgs) > 0,
        "turn_records_present": len(records) > 0,
        "dean_traces_present": len(all_trace) > 0,
        "trace_linked_per_turn": has_trace_per_turn,
        "retrieval_contexts_present_per_turn": has_contexts_per_turn,
        "locked_answer_present": bool(state.get("locked_answer", "")),
        "phase_labels_present": all(bool(r.get("phase")) for r in records) if records else False,
        "metrics_present_per_turn": all(bool(r.get("metrics")) for r in records) if records else False,
        "performance_summary_present": has_perf,
        "ready_for_euler": len(records) > 0 and has_trace_per_turn,
        "ready_for_ragas": len(records) > 0 and has_contexts_per_turn,
    }


def _build_conversation_scoring_prompt() -> str:
    return (
        "You are an evaluation judge for OT Socratic tutoring conversations.\n"
        "Score the FULL conversation and output STRICT JSON only.\n\n"
        "Required output sections:\n"
        "1) score (main):\n"
        "   - overall_score_0_to_100\n"
        "   - euler_subscores_0_to_100: question_present, relevance, helpfulness, no_reveal\n"
        "   - ragas_subscores_0_to_100: context_precision, faithfulness, answer_relevance, context_recall\n"
        "   - process_subscores_0_to_100: socratic_quality, hint_progression, clinical_multi_turn_quality\n"
        "2) diagnosis (mandatory):\n"
        "   - top_issues (ranked)\n"
        "   - for each issue: severity, evidence_turn_ids, impact_on_learning\n"
        "   - owner_for_each_issue: teacher | dean | retrieval | ui_flow | mixed\n"
        "3) fixes (mandatory):\n"
        "   - precise_fix_list with owner, implementation_hint, expected_score_gain\n"
        "4) operations (additional):\n"
        "   - speed: latency hotspots and p95 drivers\n"
        "   - cost: token/cost hotspots and cache utilization\n"
        "   - performance: throughput and reliability observations\n\n"
        "Scoring constraints:\n"
        "- Be evidence-based only from provided export data.\n"
        "- Penalize disconnected clinical flow and single-turn clinical closure.\n"
        "- Penalize strong affirmation when student is wrong/partial.\n"
        "- Reward clean non-revealing Socratic guidance and effective recovery.\n\n"
        "Return JSON schema:\n"
        "{\n"
        "  \"score\": {...},\n"
        "  \"diagnosis\": {...},\n"
        "  \"fixes\": [...],\n"
        "  \"operations\": {...},\n"
        "  \"summary\": \"short narrative\"\n"
        "}\n"
    )


def _build_state_snapshot(state: dict) -> dict:
    """Point-in-time internal state snapshot to attach to emitted tutor responses."""
    chunks = state.get("retrieved_chunks", []) or []
    eval_chunks = []
    for c in chunks[:5]:
        eval_chunks.append({
            "score": c.get("score"),
            "section_title": c.get("section_title"),
            "subsection_title": c.get("subsection_title"),
            "text": c.get("text", ""),
        })
    return {
        "phase": state.get("phase"),
        "assessment_turn": state.get("assessment_turn"),
        "clinical_opt_in": state.get("clinical_opt_in"),
        "clinical_turn_count": state.get("clinical_turn_count"),
        "clinical_max_turns": state.get("clinical_max_turns"),
        "clinical_completed": state.get("clinical_completed"),
        "clinical_state": state.get("clinical_state"),
        "clinical_confidence": state.get("clinical_confidence"),
        "turn_count": state.get("turn_count"),
        "hint_level": state.get("hint_level"),
        "max_hints": state.get("max_hints"),
        "student_state": state.get("student_state"),
        "student_reached_answer": state.get("student_reached_answer"),
        "student_answer_confidence": state.get("student_answer_confidence"),
        "student_mastery_confidence": state.get("student_mastery_confidence"),
        "confidence_samples": state.get("confidence_samples"),
        "help_abuse_count": state.get("help_abuse_count"),
        "dean_retry_count": state.get("dean_retry_count"),
        "locked_answer": state.get("locked_answer", ""),
        "topic_selection": state.get("topic_selection", ""),
        "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
        "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
        "mastery_tier": state.get("mastery_tier", "not_assessed"),
        "retrieved_chunks_for_eval": eval_chunks,
        "routing": state.get("debug", {}).get("last_routing", ""),
        "node": state.get("debug", {}).get("current_node", ""),
    }


def _stream_text(text: str):
    """Generator that yields text in an uneven, natural typing cadence."""
    words = text.split(" ")
    for i, word in enumerate(words):
        yield word + (" " if i < len(words) - 1 else "")
        delay = random.uniform(0.03, 0.09)
        if any(p in word for p in (".", "!", "?")):
            delay += random.uniform(0.10, 0.22)  # brief pause at sentence end
        elif any(p in word for p in (",", ";", ":")):
            delay += random.uniform(0.04, 0.10)  # lighter pause mid-sentence
        time.sleep(min(delay, 0.35))


def _thinking_indicator_html() -> str:
    """Claude-style animated thinking indicator with cycling dots."""
    return """
    <style>
      .cc-thinking-wrap {
        margin: 0.25rem 0 0.6rem 0;
        padding: 0.45rem 0.7rem;
        border-radius: 10px;
        background: rgba(120,120,120,0.12);
        border: 1px solid rgba(160,160,160,0.25);
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: #d7d7d7;
        font-size: 0.92rem;
      }
      .cc-thinking-dots {
        display: inline-flex;
        align-items: flex-end;
        min-width: 20px;
      }
      .cc-thinking-dots span {
        width: 4px;
        height: 4px;
        margin-right: 2px;
        border-radius: 50%;
        background: #d7d7d7;
        opacity: 0.2;
        animation: cc-dot 1.2s infinite ease-in-out;
      }
      .cc-thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
      .cc-thinking-dots span:nth-child(3) { animation-delay: 0.4s; margin-right: 0; }
      @keyframes cc-dot {
        0%, 80%, 100% { opacity: 0.2; transform: translateY(0); }
        40% { opacity: 1; transform: translateY(-1px); }
      }
    </style>
    <div class="cc-thinking-wrap">
      <span>Thinking</span>
      <span class="cc-thinking-dots"><span></span><span></span><span></span></span>
    </div>
    """


def _inject_ui_styles(debug_mode: bool = False):
    """Apply safe visual styling. Uses position:fixed on stBottom (the chat input wrapper)
    which is a stable Streamlit testid and safe to target directly."""
    # Sidebar is ~21rem wide in non-debug mode; 0 in debug mode (no sidebar)
    sidebar_w = "0rem" if debug_mode else "21rem"
    st.markdown(
        f"""
        <style>
          :root {{
            --sok-chat-max-width: 780px;
            --sok-bg: #f5f6f8;
            --sok-text: #1f2937;
            --sok-muted: #6b7280;
            --sok-border: #e5e7eb;
            --sok-panel: #ffffff;
            --sok-panel-soft: #f8fafc;
            --sok-accent: #2563eb;
            --sok-chip: #eef2ff;
            --sok-sidebar-w: {sidebar_w};
          }}

          [data-testid="stApp"],
          [data-testid="stAppViewContainer"] {{
            background: var(--sok-bg);
          }}
          [data-testid="stHeader"] {{ display: none; }}
          [data-testid="stToolbar"] {{ display: none; }}
          [data-testid="stDecoration"] {{ display: none; }}
          [data-testid="stMainBlockContainer"] {{
            max-width: 1400px;
            padding-top: 0.35rem;
            padding-left: 2rem;
            padding-right: 2rem;
            /* Reserve space at bottom so messages don't hide under fixed input */
            padding-bottom: 180px;
          }}

          /* ── Chat input: always pinned to bottom of viewport ── */
          div[data-testid="stBottom"] {{
            position: fixed !important;
            bottom: 0;
            left: var(--sok-sidebar-w);
            right: 0;
            padding: 0.5rem 2rem 0.85rem 2rem;
            background: linear-gradient(
              to bottom,
              rgba(245,246,248,0) 0%,
              rgba(245,246,248,0.96) 28%,
              rgba(245,246,248,1) 100%
            );
            z-index: 100;
          }}
          /* Input fills the full width of its fixed container, left-aligned with messages */
          div[data-testid="stBottom"] div[data-testid="stChatInput"] {{
            max-width: 860px;
            margin-left: 0;
            margin-right: auto;
          }}
          div[data-testid="stBottom"] div[data-testid="stChatInput"] > div {{
            background: var(--sok-panel);
            border: 1.5px solid var(--sok-border);
            border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.07);
            padding: 0.5rem 0.75rem;
          }}
          div[data-testid="stBottom"] div[data-testid="stChatInputContainer"] textarea {{
            min-height: 88px !important;
            font-size: 1rem !important;
            line-height: 1.5 !important;
            padding: 0.6rem 0.5rem !important;
            resize: none;
          }}

          /* Sidebar flex layout — pins Arun account block to bottom */
          [data-testid="stSidebarContent"] {{
            display: flex !important;
            flex-direction: column !important;
            height: 100% !important;
            min-height: 100vh;
          }}
          .sok-sidebar-spacer {{
            flex: 1;
          }}
          * {{
            color: var(--sok-text);
            font-family: "Iowan Old Style", "Palatino", "Times New Roman", serif;
          }}

          .sok-app-title {{
            font-weight: 700;
            font-size: 1.15rem;
            letter-spacing: 0.2px;
            margin-top: 0.1rem;
          }}
          .sok-top-meta {{
            color: var(--sok-muted);
            font-size: 0.88rem;
          }}

          /* Chat bubbles */
          div[data-testid="stChatMessage"] {{
            width: 100%;
            max-width: var(--sok-chat-max-width);
            margin-left: 0;
            margin-right: auto;
          }}
          div[data-testid="stChatMessageContent"] {{
            width: 100%;
            min-width: 0;
          }}
          div[data-testid="stChatMessageContent"] p {{
            word-break: normal;
            overflow-wrap: break-word;
            line-height: 1.55;
          }}
          [data-testid="stChatMessageContent"] > div {{
            background: var(--sok-panel);
            border: 1px solid var(--sok-border);
            border-radius: 14px;
            padding: 0.75rem 0.9rem;
          }}
          div[data-testid="stChatMessage"] [data-testid="stAvatar"] img {{
            width: 68px !important;
            height: 68px !important;
            object-fit: contain;
            background: transparent !important;
          }}
          div[data-testid="stChatMessage"] [data-testid="stAvatar"] {{
            margin-top: 22px;
            min-width: 68px !important;
          }}

          .sok-side-title {{
            font-weight: 700;
            font-size: 2.05rem;
            letter-spacing: 0.1px;
            margin: 0.08rem 0 0.55rem 0;
            color: #1f2937;
          }}
          .sok-side-menu {{
            color: #374151;
            font-size: 1.02rem;
            margin-bottom: 0.35rem;
          }}
          .sok-side-item {{
            color: #2f3a49;
            font-size: 1.02rem;
            line-height: 1.3;
            padding: 0.38rem 0.42rem;
            border-radius: 10px;
            margin-bottom: 0.14rem;
            transition: background 120ms ease, transform 120ms ease;
          }}
          .sok-side-item:hover {{
            background: #d8dde7;
            transform: translateX(1px);
          }}
          .sok-side-spacer {{
            height: 0.4rem;
          }}
          .sok-user-row {{
            display: flex;
            align-items: center;
            gap: 0.55rem;
            margin-top: 0.35rem;
          }}
          .sok-user-avatar {{
            width: 34px;
            height: 34px;
            border-radius: 999px;
            background: #d1d5db;
            color: #111827;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 0.98rem;
            border: 1px solid #c5ccd6;
          }}
          .sok-user-name {{
            font-size: 0.92rem;
            color: #1f2937;
            font-weight: 600;
            line-height: 1.1;
          }}
          .sok-user-sub {{
            font-size: 0.8rem;
            color: #6b7280;
            line-height: 1.1;
          }}

          /* Left rail cards */
          .sok-rail-card {{
            background: var(--sok-panel);
            border: 1px solid var(--sok-border);
            border-radius: 12px;
            padding: 0.7rem 0.75rem;
            margin-bottom: 0.65rem;
          }}
          .sok-rail-title {{
            font-size: 0.9rem;
            font-weight: 600;
            margin-bottom: 0.25rem;
          }}
          .sok-rail-line {{
            font-size: 0.86rem;
            color: var(--sok-muted);
            margin: 0.12rem 0;
          }}

          .sok-phase-chip {{
            display: inline-block;
            background: var(--sok-chip);
            color: #334155;
            border: 1px solid #dbe4ff;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 600;
            padding: 0.2rem 0.55rem;
            margin: 0.25rem 0 0.35rem 0;
          }}

          .sok-clinical-card {{
            background: var(--sok-panel-soft);
            border: 1px solid var(--sok-border);
            border-radius: 12px;
            padding: 0.7rem 0.8rem;
            margin-bottom: 0.45rem;
            box-shadow: 0 4px 16px rgba(0,0,0,0.05);
            font-size: 0.9rem;
            color: #334155;
          }}
          [data-testid="stButton"] > button,
          [data-testid="stDownloadButton"] > button {{
            border-radius: 10px;
            border: 1px solid var(--sok-border);
            background: var(--sok-panel);
            color: #111827;
          }}
          [data-testid="stButton"] > button[kind="primary"] {{
            background: #2563eb;
            border-color: #2563eb;
            color: white;
          }}
          /* Popover trigger button — remove Streamlit's red border */
          [data-testid="stPopover"] > button {{
            border: 1px solid var(--sok-border) !important;
            border-radius: 12px !important;
            background: transparent !important;
            box-shadow: none !important;
          }}
          [data-testid="stPopover"] > button:hover {{
            background: rgba(0,0,0,0.04) !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_chat(messages: list[dict], animate_idx: int = -1, show_debug: bool = False):
    """Render chat messages. Animate the message at animate_idx."""
    prev_phase = "tutoring"
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        metrics = msg.get("_metrics")
        snapshot = msg.get("_state_snapshot")
        msg_phase = msg.get("_phase", "tutoring")

        # Show phase transition banner
        if role == "tutor" and msg_phase != prev_phase:
            if msg_phase == "assessment":
                st.markdown("<span class='sok-phase-chip'>Clinical Application</span>", unsafe_allow_html=True)
            elif msg_phase == "memory_update":
                st.markdown("<span class='sok-phase-chip'>Session Complete</span>", unsafe_allow_html=True)
            prev_phase = msg_phase

        if role == "tutor":
            with st.chat_message("assistant", avatar=AVATAR):
                if i == animate_idx:
                    st.write_stream(_stream_text(content))
                else:
                    st.write(content)
                if show_debug and metrics:
                    with st.expander(
                        f"time {metrics['elapsed_s']}s  |  in:{metrics['in_tok']} out:{metrics['out_tok']}"
                        f"  |  ${metrics['cost_usd']:.5f}"
                        + f"  |  cache_read:{metrics.get('cache_read', 0)} cache_write:{metrics.get('cache_write', 0)}",
                        expanded=False
                    ):
                        for entry in metrics["per_call"]:
                            parts = [f"**{entry['wrapper']}**"]
                            if "elapsed_s" in entry:
                                parts.append(f"`{entry['elapsed_s']}s`")
                            if "in_tok" in entry:
                                parts.append(f"in:{entry['in_tok']} out:{entry['out_tok']}")
                            parts.append(f"cache_read:{entry.get('cache_read', 0)} cache_write:{entry.get('cache_write', 0)}")
                            if "cost_usd" in entry:
                                parts.append(f"${entry['cost_usd']:.5f}")
                            st.markdown("  |  ".join(parts))
                if show_debug and snapshot:
                    with st.expander("Internal State At This Response", expanded=False):
                        st.json(snapshot, expanded=False)
        elif role == "student":
            with st.chat_message("user"):
                st.write(content)
        elif role == "system":
            with st.chat_message("assistant"):
                st.caption(content)


def _timing_badge(entry: dict) -> str:
    parts = []
    if "elapsed_s" in entry:
        parts.append(f"time:{entry['elapsed_s']}s")
    if "in_tok" in entry:
        parts.append(f"in:{entry['in_tok']} out:{entry['out_tok']}")
    parts.append(f"cache_hit:{entry.get('cache_read', 0)}")
    parts.append(f"cache_write:{entry.get('cache_write', 0)}")
    if "cost_usd" in entry:
        parts.append(f"${entry['cost_usd']:.5f}")
    return "  |  ".join(parts)


def _render_copy_button(text: str, key: str, label: str = "Copy"):
    """Client-side clipboard copy button for debug cards."""
    safe_text = json.dumps(text or "")
    safe_label = json.dumps(label)
    btn_id = f"copy_btn_{key}"
    html = f"""
    <div style="display:flex;justify-content:flex-end;align-items:center;">
      <button id="{btn_id}" style="
        border:1px solid #4a4a4a;
        background:#1f1f1f;
        color:#e8e8e8;
        border-radius:6px;
        padding:4px 10px;
        font-size:12px;
        cursor:pointer;
      ">{label}</button>
    </div>
    <script>
      const btn = document.getElementById({json.dumps(btn_id)});
      if (btn) {{
        btn.onclick = async () => {{
          try {{
            await navigator.clipboard.writeText({safe_text});
            const old = btn.textContent;
            btn.textContent = "Copied";
            setTimeout(() => btn.textContent = JSON.parse({json.dumps(safe_label)}), 1000);
          }} catch (e) {{
            btn.textContent = "Copy failed";
            setTimeout(() => btn.textContent = JSON.parse({json.dumps(safe_label)}), 1200);
          }}
        }};
      }}
    </script>
    """
    components.html(html, height=34)


def _stringify_message_content(content) -> str:
    safe = _to_json_safe(content)
    if isinstance(safe, (dict, list)):
        return json.dumps(safe, indent=2, default=str)
    return str(safe)


def _to_json_safe(obj):
    """
    Convert SDK objects (e.g., Anthropic ToolUseBlock) into JSON-safe structures
    for debug rendering/copy actions.
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_to_json_safe(v) for v in obj]

    # Pydantic-like SDK objects
    if hasattr(obj, "model_dump"):
        try:
            return _to_json_safe(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _to_json_safe(obj.dict())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return _to_json_safe(vars(obj))
        except Exception:
            pass

    # Final fallback
    return str(obj)


def _entry_actor(entry: dict) -> str:
    wrapper = (entry.get("wrapper") or "").lower()
    if wrapper.startswith("dean"):
        return "Dean"
    if wrapper.startswith("teacher"):
        return "Teacher"
    return "Other"


def _entry_status(entry: dict) -> str:
    result = str(entry.get("result", "")).upper()
    if "FAIL" in result:
        return "fail"
    if "PASS" in result:
        return "pass"
    return "info"


def _group_status(entries: list[tuple[int, dict]]) -> str:
    statuses = [_entry_status(e) for _, e in entries]
    if "fail" in statuses:
        return "fail"
    if "pass" in statuses:
        return "pass"
    return "info"


def _status_chip(status: str, label: str) -> str:
    palette = {
        "pass": ("#193d2f", "#3ddc97", "PASS"),
        "fail": ("#4a1f1f", "#ff6b6b", "FAIL"),
        "info": ("#1e2f4d", "#79a8ff", "INFO"),
    }
    bg, fg, txt = palette.get(status, palette["info"])
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
        f"background:{bg};color:{fg};font-size:12px;font-weight:600;margin-left:8px;'>{txt}</span>"
        f"<span style='margin-left:10px;font-size:13px;color:#d8d8d8;'>{label}</span>"
    )


def _render_trace_cards(entries: list[tuple[int, dict]], turn_number: int, actor: str, trace_id: int):
    for orig_idx, entry in entries:
        wrapper = entry.get("wrapper", "")
        system_prompt = entry.get("system_prompt", "")
        messages_sent = entry.get("messages_sent", [])
        response_text = entry.get("response_text", "")
        tool_calls = entry.get("tool_calls_made", [])
        result = entry.get("result", "")
        badge = _timing_badge(entry)

        # Icon
        if "setup_call" in wrapper:
            icon = "[setup]"
        elif "quality_check" in wrapper:
            icon = "[qc-pass]" if result and "PASS" in result else ("[qc-fail]" if result else "[qc]")
        elif "teacher" in wrapper:
            icon = "[teacher]"
        elif "fallback" in wrapper:
            icon = "[fallback]"
        elif "assessment" in wrapper:
            icon = "[assessment]"
        elif "memory" in wrapper:
            icon = "[memory]"
        else:
            icon = "[call]"

        with st.container(border=True):
            h1, h2 = st.columns([3, 2])
            with h1:
                st.markdown(f"**{icon} `{wrapper}`**")
            with h2:
                if badge:
                    st.caption(badge)

            if result:
                if "PASS" in str(result):
                    st.success(result)
                elif "FAIL" in str(result):
                    st.error(result)
                else:
                    st.caption(result)

            tabs = st.tabs(["System", "Input", "Tools", "Output"])

            with tabs[0]:
                st.markdown("**System prompt**")
                if system_prompt:
                    _render_copy_button(
                        system_prompt,
                        key=f"sys_{trace_id}_{turn_number}_{actor}_{orig_idx}",
                        label="Copy system",
                    )
                    st.code(system_prompt, language=None)
                else:
                    st.caption("(none)")

            with tabs[1]:
                st.markdown("**Messages sent**")
                if not messages_sent:
                    st.caption("(none)")
                for msg_idx, msg in enumerate(messages_sent):
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    content_str = _stringify_message_content(content)
                    st.markdown(f"**{msg_idx + 1}. `{role}`**")
                    _render_copy_button(
                        content_str,
                        key=f"in_{trace_id}_{turn_number}_{actor}_{orig_idx}_{msg_idx}",
                        label="Copy input",
                    )
                    if isinstance(content, list):
                        st.json(_to_json_safe(content), expanded=False)
                    else:
                        st.code(content_str, language=None)

            with tabs[2]:
                st.markdown("**Tool calls**")
                if not tool_calls:
                    st.caption("(none)")
                for tool_idx, tc in enumerate(tool_calls):
                    tool_name = tc.get("name", "unknown_tool")
                    st.markdown(f"tool `{tool_name}()`")
                    tc_input = tc.get("input", {})
                    safe_tc_input = _to_json_safe(tc_input)
                    input_str = json.dumps(safe_tc_input, indent=2, default=str)
                    _render_copy_button(
                        input_str,
                        key=f"tool_{trace_id}_{turn_number}_{actor}_{orig_idx}_{tool_idx}",
                        label="Copy tool input",
                    )
                    st.json(safe_tc_input, expanded=False)

            with tabs[3]:
                st.markdown("**Raw response**")
                if response_text:
                    _render_copy_button(
                        response_text,
                        key=f"out_{trace_id}_{turn_number}_{actor}_{orig_idx}",
                        label="Copy output",
                    )
                    st.code(response_text, language=None)
                else:
                    st.caption("(none)")
                if entry.get("dean_text"):
                    st.markdown("**Dean intermediate text block**")
                    dean_text = entry["dean_text"]
                    _render_copy_button(
                        dean_text,
                        key=f"deantxt_{trace_id}_{turn_number}_{actor}_{orig_idx}",
                        label="Copy dean text",
                    )
                    st.code(dean_text, language=None)


def _render_dean_stream(turn_trace: list[dict], turn_number: int, trace_id: int):
    """
    Render a Claude-Code-like timeline: one card per API call with
    prompt, input messages, tool calls, and output.
    """
    if not turn_trace:
        st.caption("No activity yet — send a message.")
        return

    groups = {"Dean": [], "Teacher": [], "Other": []}
    for idx, entry in enumerate(turn_trace):
        groups[_entry_actor(entry)].append((idx, entry))

    for actor in ("Dean", "Teacher", "Other"):
        entries = groups[actor]
        if not entries:
            continue
        status = _group_status(entries)
        chip = _status_chip(status, f"{actor} turn {turn_number}")

        c1, c2 = st.columns([5, 1.2])
        with c1:
            st.markdown(chip, unsafe_allow_html=True)
        with c2:
            show_group = st.toggle(
                "Show",
                key=f"group_{trace_id}_{turn_number}_{actor}",
                value=(actor == "Dean"),
                label_visibility="collapsed",
            )
        if show_group:
            _render_trace_cards(entries, turn_number=turn_number, actor=actor, trace_id=trace_id)


def _render_state_snapshot(state: dict):
    if not state:
        return
    with st.expander("State snapshot", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            st.write(f"**phase:** {state.get('phase', '-')}")
            st.write(f"**node:** {state.get('debug', {}).get('current_node', '-')}")
            st.write(f"**routing:** {state.get('debug', {}).get('last_routing', '-')}")
            st.write(f"**locked_answer:** {state.get('locked_answer') or '(not set)'}")
        with col2:
            st.write(f"**student_state:** {state.get('student_state') or '-'}")
            st.write(f"**hint:** {state.get('hint_level', 1)}/{state.get('max_hints', 3)}")
            st.write(
                f"**answer confidence:** {float(state.get('student_answer_confidence', 0.0)):.3f}"
                f"  |  **mastery confidence:** {float(state.get('student_mastery_confidence', 0.0)):.3f}"
            )
            st.write(f"**help_abuse:** {state.get('help_abuse_count', 0)}/{cfg.dean.help_abuse_threshold}")
            st.write(f"**retries:** {state.get('dean_retry_count', 0)}/{cfg.dean.max_teacher_retries}")

    debug = state.get("debug", {})
    with st.expander("API usage this session", expanded=False):
        calls = debug.get("api_calls", 0)
        in_tok = debug.get("input_tokens", 0)
        out_tok = debug.get("output_tokens", 0)
        cost = float(debug.get("cost_usd", (in_tok * 3 + out_tok * 15) / 1_000_000))
        st.write(f"Calls: {calls}  |  In: {in_tok:,}  |  Out: {out_tok:,}  |  ~${cost:.4f}")
        st.write(f"Interventions: {debug.get('interventions', 0)}")

    chunks = state.get("retrieved_chunks", [])
    if chunks:
        with st.expander("Retrieved chunks", expanded=False):
            for chunk in chunks:
                score = chunk.get("score", 0)
                section = chunk.get("section_title", "")
                subsection = chunk.get("subsection_title", "")
                location = " > ".join(filter(None, [section, subsection]))
                st.caption(f"[{score:.2f}] {chunk.get('element_type', 'para')} — {location}")


def _build_export_payload(profile_id: str) -> dict:
    """
    Build a full-fidelity export payload for analysis.
    Includes chat, per-turn metadata, dean traces, and current runtime state.
    """
    state = st.session_state.get("state", {}) or {}
    messages_display = st.session_state.get("messages_display", []) or []
    all_turn_traces = st.session_state.get("all_turn_traces", []) or []
    current_turn_trace = st.session_state.get("dean_stream", []) or []
    debug_state = state.get("debug", {}) or {}

    tutor_turns = []
    for idx, msg in enumerate(messages_display):
        if msg.get("role") != "tutor":
            continue
        tutor_turns.append({
            "index": idx,
            "phase": msg.get("_phase"),
            "turn": msg.get("_turn"),
            "content": msg.get("content", ""),
            "metrics": msg.get("_metrics", {}),
            "state_snapshot": msg.get("_state_snapshot", {}),
        })

    turn_records = _build_eval_turn_records(messages_display, all_turn_traces)
    performance_summary = _build_performance_summary(all_turn_traces, debug_state)

    payload = {
        "export_version": "1.1",
        "exported_at_local": datetime.now().isoformat(timespec="seconds"),
        "session": {
            "thread_id": st.session_state.get("thread_id"),
            "profile_id": profile_id,
            "student_id": state.get("student_id"),
            "phase": state.get("phase"),
            "assessment_turn": state.get("assessment_turn"),
        },
        "conversation": {
            "messages": state.get("messages", []),
            "messages_display": messages_display,
            "tutor_turns": tutor_turns,
        },
        "dean": {
            "all_turn_traces": all_turn_traces,
            "current_turn_trace": current_turn_trace,
        },
        "evaluation": {
            "turn_records": turn_records,
            "scoring_prompt_overall": _build_conversation_scoring_prompt(),
            "notes": {
                "required_outputs": [
                    "score",
                    "diagnosis",
                    "fixes",
                    "operations",
                ],
                "owner_labels": ["teacher", "dean", "retrieval", "ui_flow", "mixed"],
            },
        },
        "performance": {
            "summary": performance_summary,
        },
        "state": {
            "current_state": state,
            "retrieved_chunks": state.get("retrieved_chunks", []),
            "weak_topics": state.get("weak_topics", []),
        },
        "debug": debug_state,
    }
    payload["coverage"] = _build_export_coverage(payload)
    return _to_json_safe(payload)


def _render_clean_left_rail(state: dict):
    """Compact left rail for non-debug mode."""
    if not state:
        return
    turn = int(state.get("turn_count", 0))
    max_turns = int(state.get("max_turns", 25))
    hint = int(state.get("hint_level", 1))
    max_hints = int(state.get("max_hints", 3))
    hint_icons = "●" * max(0, hint) + "○" * max(0, (max_hints - hint))
    weak = state.get("weak_topics", []) or []

    st.markdown("<div class='sok-rail-card'><div class='sok-rail-title'>Session</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='sok-rail-line'>Turn {turn}/{max_turns}</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='sok-rail-line'>Hints {hint_icons} ({hint}/{max_hints})</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='sok-rail-card'><div class='sok-rail-title'>Focus</div>", unsafe_allow_html=True)
    if weak:
        for wt in weak[:3]:
            st.markdown(
                f"<div class='sok-rail-line'>• {wt['topic']} ({wt.get('failure_count', 0)}x)</div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown("<div class='sok-rail-line'>No weak topics yet.</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def _render_topic_option_cards(state: dict) -> str | None:
    """
    Render clickable topic scoping options when topic is not confirmed yet.
    Returns selected option text (if any), else None.
    """
    if not state:
        return None
    if state.get("topic_confirmed", False):
        return None
    options = state.get("topic_options", []) or []
    if not options:
        return None

    # Keep cards aligned with chat lane (same left alignment, narrower width).
    cards_col, _spacer = st.columns([3, 2], gap="small")
    selected = None
    with cards_col:
        for idx, opt in enumerate(options):
            if st.button(
                f"{idx + 1}. {opt}",
                key=f"topic_option_{idx}",
                use_container_width=True,
            ):
                selected = opt
                break
    return selected


def main():
    st.set_page_config(page_title="Socratic-OT", layout="wide")
    _init_session()
    debug_mode = st.session_state.get("debug_mode", False)
    _inject_ui_styles(debug_mode=debug_mode)
    profile_id = st.session_state.get("profile_id", "S1")

    # --- Top bar (debug only) ---
    if debug_mode:
        top_bar = st.container()
        with top_bar:
            st.markdown("<div class='sok-topbar-anchor'></div>", unsafe_allow_html=True)
            c1, c2, c3, c4, c5 = st.columns([2.0, 1.2, 1.2, 2.0, 1.6])
            with c1:
                profile_id = st.selectbox(
                    "Profile", ["S1", "S2", "S3", "S4", "S5", "S6"],
                    index=["S1", "S2", "S3", "S4", "S5", "S6"].index(st.session_state.get("profile_id", "S1")),
                    label_visibility="collapsed",
                    key="profile_select_debug",
                )
                st.session_state.profile_id = profile_id
                st.caption({
                    "S1": "Strong — early answer", "S2": "Moderate — 1-2 hints",
                    "S3": "Weak — all hints", "S4": "Overconfident/Wrong",
                    "S5": "Disengaged", "S6": "Anxious/Correct",
                }.get(profile_id, ""))
            with c2:
                debug_mode = st.toggle("Debug mode", value=True, key="debug_mode")
            with c3:
                if st.button("New Session", type="primary", use_container_width=True):
                    _start_new_session(profile_id)
                    st.rerun()
            state = st.session_state.get("state")
            with c4:
                if state:
                    turn = state.get("turn_count", 0)
                    max_turns = state.get("max_turns", 25)
                    hint = state.get("hint_level", 1)
                    max_hints = state.get("max_hints", 3)
                    hint_label = "█" * hint + "░" * (max_hints - hint)
                    st.caption(f"Turn {turn}/{max_turns}  |  Hint {hint_label} {hint}/{max_hints}")
                    st.progress(turn / max_turns if max_turns > 0 else 0)
            with c5:
                if state:
                    export_payload = _build_export_payload(profile_id)
                    export_json = json.dumps(export_payload, indent=2, default=str)
                    student_id = state.get("student_id", "session")
                    fname_ts = time.strftime("%Y%m%d_%H%M%S")
                    st.download_button(
                        "Export Chat",
                        data=export_json,
                        file_name=f"sokratic_export_{student_id}_{fname_ts}.json",
                        mime="application/json",
                        use_container_width=True,
                    )
            st.divider()

    # --- Auto-start ---
    if st.session_state.state is None:
        _start_new_session(profile_id)
        st.rerun()

    state = st.session_state.state
    profile_id = st.session_state.get("profile_id", "S1")
    animate_idx = st.session_state.get("last_animated", -1)

    # --- Non-debug sidebar (left rail) ---
    if not debug_mode:
        with st.sidebar:
            st.markdown("<div class='sok-side-title'>Sokratic</div>", unsafe_allow_html=True)
            if st.button("+ New chat", use_container_width=True):
                _start_new_session(profile_id)
                st.rerun()
            st.markdown("<div class='sok-side-menu'>Session overview</div>", unsafe_allow_html=True)
            _render_clean_left_rail(state)
            # Spacer pushes account section to bottom
            st.markdown("<div class='sok-sidebar-spacer'></div>", unsafe_allow_html=True)
            st.divider()
            # Account section — pinned to bottom
            if hasattr(st, "popover"):
                with st.popover("Arun", use_container_width=True):
                    st.markdown("**Arun**")
                    st.caption("arun@example.com")
                    st.divider()
                    st.toggle("Debug mode", key="debug_mode")
                    st.button(
                        "Learning Insights",
                        key="open_learning_insights",
                        use_container_width=True,
                        disabled=True,
                    )
            else:
                if st.button("Arun ↗", use_container_width=True):
                    st.session_state.debug_mode = not st.session_state.get("debug_mode", False)
                    st.rerun()

    # --- Layout ---
    if debug_mode:
        left_col, right_col = st.columns([1, 1])
    else:
        left_col = st.container()
        right_col = None

    with left_col:
        _render_chat(st.session_state.messages_display, animate_idx=animate_idx, show_debug=debug_mode)
        # Clear animation flag after render so it doesn't re-animate on reruns
        st.session_state.last_animated = -1

        for chunk in state.get("retrieved_chunks", []):
            if chunk.get("element_type") == "diagram" and chunk.get("image_filename"):
                img_path = Path(cfg.paths.diagrams) / chunk["image_filename"]
                if img_path.exists():
                    st.image(str(img_path), caption=chunk.get("section_title", ""),
                             use_container_width=True)

        phase = state.get("phase", "tutoring")
        if phase == "memory_update":
            st.markdown("<span class='sok-phase-chip'>Session complete</span>", unsafe_allow_html=True)
            st.caption("Memory updated. Start a new session when ready.")
        else:
            assessment_turn = state.get("assessment_turn", 0)
            scenario = None

            if phase == "assessment" and state.get("student_reached_answer") and assessment_turn == 1:
                st.markdown(
                    "<div class='sok-clinical-card'>"
                    "Apply this concept clinically? Choose one to continue."
                    "</div>",
                    unsafe_allow_html=True,
                )
                c_yes, c_skip = st.columns([1, 1])
                if c_yes.button("Yes, clinical question", use_container_width=True):
                    scenario = "Yes, ask me a clinical application question."
                if c_skip.button("Skip clinical question", use_container_width=True):
                    scenario = "No, skip the clinical question."
            elif phase == "tutoring" and debug_mode:
                b_cols = st.columns(4)
                if b_cols[0].button("Correct", use_container_width=True):
                    scenario = state.get("locked_answer") or "I believe I know the answer."
                if b_cols[1].button("Wrong", use_container_width=True):
                    scenario = "I think it's the radial nerve."
                if b_cols[2].button("Don't know", use_container_width=True):
                    scenario = "I don't know."
                if b_cols[3].button("Off-topic", use_container_width=True):
                    scenario = "Can you tell me about something else?"

            if scenario:
                _send_student_message(scenario)
                st.rerun()

            # Topic scoping cards (click-to-select) before normal tutoring starts.
            topic_choice = _render_topic_option_cards(state)
            if topic_choice:
                _send_student_message(topic_choice)
                st.rerun()

            input_prompt = (
                "Reply yes/no for optional clinical question..."
                if phase == "assessment" and state.get("student_reached_answer") and assessment_turn == 1
                else "Pick a focus card, or write something else you want to explore..."
                if (phase == "tutoring" and not state.get("topic_confirmed", False) and (state.get("topic_options") or []))
                else "Type your response..."
            )
            # st.chat_input for all modes — clears itself automatically after submit
            user_input = st.chat_input(input_prompt)
            if user_input:
                _send_student_message(user_input)
                st.rerun()

    if debug_mode and right_col is not None:
        with right_col:
            st.subheader("Dean internal stream")
            all_traces = st.session_state.get("all_turn_traces", [])
            if not all_traces:
                st.caption("No activity yet — send a message.")
            else:
                for trace_idx, t in enumerate(all_traces):
                    with st.expander(
                        f"Turn {t['turn']}  ·  {t['phase']}",
                        expanded=(t is all_traces[-1])
                    ):
                        _render_dean_stream(t["trace"], turn_number=t["turn"], trace_id=trace_idx)
            st.divider()
            _render_state_snapshot(state)


if __name__ == "__main__":
    main()
