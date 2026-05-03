"""
evaluation/quality/schema.py
----------------------------
Input adapter + output dataclasses for the quality scorer.

Two input formats are supported:

  1. **Test harness export** — produced by scripts/test_reached_gate_e2e.py.
     Has `log` (step-by-step), `outcomes`, `gate_traces`, `expectations`,
     `result`, `debug_summary`. Includes a `final_state` field once we
     extend the harness (Phase 1a-6).

  2. **Production export** — produced by `/api/session/{thread_id}/export`
     in backend/api/session.py. Returns the full TutorState dict.

`load_session(path)` returns a normalized `SessionView` regardless of source.
The scorer reads from SessionView only; it doesn't care which source format.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
import json
import re
from pathlib import Path


# =============================================================================
# Normalized input view — what the scorer reads from
# =============================================================================

@dataclass
class TutorTurn:
    """One tutor message exchange. Pairs the student msg that triggered it
    with the tutor's reply, plus per-turn signals from the dean's traces."""
    turn_id: int                          # 1-indexed turn count (post-rapport)
    phase: str                            # rapport | tutoring | assessment | memory_update
    student_msg: str                      # the student message that triggered this turn
    tutor_msg: str                        # the tutor's response
    student_state: Optional[str] = None   # correct | partial_correct | incorrect | question | irrelevant | low_effort
    student_reached_answer: bool = False  # gate output
    student_answer_confidence: float = 0.0
    hint_level: int = 0
    gate_path: Optional[str] = None       # overlap | paraphrase | hedge_block | etc.
    gate_evidence: str = ""
    qc_pass: Optional[bool] = None        # _quality_check_call result
    qc_reason_codes: list[str] = field(default_factory=list)
    intervention_used: bool = False       # dean fallback fired this turn
    revised_draft_applied: bool = False   # teacher draft was rewritten
    fabrication_keyword_match: list[str] = field(default_factory=list)

    # L39 — v2 stack signals (populated when SOKRATIC_USE_V2_FLOW=1).
    # Empty / default values for legacy sessions so existing scorers
    # keep working without code changes downstream.
    preflight_fired: bool = False
    preflight_category: str = ""             # help_abuse | off_domain | deflection | none
    turn_plan_mode: str = ""                 # socratic | clinical | rapport | opt_in | redirect | nudge | confirm_end | honest_close
    turn_plan_tone: str = ""                 # encouraging | firm | neutral | honest
    dean_v2_used_fallback: bool = False
    retry_final_attempt: int = 0             # 1-3 normal, 4 = safe-generic-probe
    retry_used_safe_probe: bool = False
    retry_used_dean_replan: bool = False
    retry_n_attempts: int = 0


@dataclass
class SessionView:
    """Everything the scorer needs from one saved session.
    Populated by `load_session` from either input format."""
    # Identity
    session_id: str = ""
    test_id: Optional[str] = None         # e.g. "T1_wrong_answer" (test harness only)
    timestamp: str = ""

    # Core anchors
    locked_topic: dict = field(default_factory=dict)  # path, chapter, section, subsection
    locked_question: str = ""
    locked_answer: str = ""
    locked_answer_aliases: list[str] = field(default_factory=list)

    # Retrieval
    retrieved_chunks: list[dict] = field(default_factory=list)  # each: {text, score, subsection_title, ...}

    # Turns
    rapport_message: str = ""
    turns: list[TutorTurn] = field(default_factory=list)

    # Final state
    final_phase: str = ""
    final_student_reached_answer: bool = False
    final_hint_level: int = 0
    max_hints: int = 3
    final_turn_count: int = 0
    max_turns: int = 25

    # L21 + L39 — session lifecycle status from the SQL row. Drives
    # scorer behavior: in_progress → skip; abandoned_no_lock → no-lock
    # verdict (don't critical-penalize); ended_off_domain / ended_turn_limit
    # → don't apply LEAK_DETECTED on the close turn (no answer reveal by
    # design).
    status: str = ""                      # in_progress | completed | ended_off_domain | ended_by_student | ended_turn_limit | abandoned_no_lock

    # Debug rollups (from state["debug"])
    api_calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    interventions: int = 0
    coverage_gap_events: int = 0
    grounded_turns: int = 0
    ungrounded_turns: int = 0
    invariant_violations: list[dict] = field(default_factory=list)
    retrieval_calls: int = 0
    help_abuse_count_max: int = 0  # peak across the session
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    elapsed_s_per_turn: list[float] = field(default_factory=list)

    # Trace history
    all_turn_traces: list[dict] = field(default_factory=list)  # [{turn, phase, trace: [wrappers]}]
    hint_progress: list[dict] = field(default_factory=list)
    hint_plan: list[str] = field(default_factory=list)

    # Mastery
    mastery_score: Optional[float] = None
    mastery_confidence: Optional[float] = None
    mastery_rationale: str = ""
    student_mastery_confidence_first: float = 0.0
    student_mastery_confidence_last: float = 0.0

    # Test harness ground-truth (when available)
    expected_reached: Optional[bool] = None
    expected_no_fabrication_keywords: list[str] = field(default_factory=list)
    description: str = ""


# =============================================================================
# Loader — handles both input formats
# =============================================================================

def load_session(path: str | Path) -> SessionView:
    """Load a session JSON from either format and return a normalized view."""
    path = Path(path)
    raw = json.loads(path.read_text())
    if "final_state" in raw:
        # Test harness with extended export
        return _load_from_test_harness(raw)
    elif "log" in raw and "outcomes" in raw:
        # Test harness without final_state — best-effort reconstruction
        return _load_from_test_harness_partial(raw)
    elif "messages" in raw and "debug" in raw:
        # Production export (raw TutorState)
        return _load_from_production(raw)
    else:
        raise ValueError(
            f"Unrecognized session format at {path}. "
            f"Top-level keys: {sorted(raw.keys())[:10]}"
        )


def _load_from_test_harness(raw: dict) -> SessionView:
    """Test harness export with `final_state` field (post-Phase-1a-6)."""
    state = raw.get("final_state") or {}
    view = _view_from_state(state)
    # Overlay test-specific metadata
    view.session_id = str(raw.get("conv_id") or view.session_id)
    view.test_id = raw.get("name")
    view.timestamp = raw.get("timestamp", "")
    view.description = raw.get("description", "")
    exp = raw.get("expectations") or {}
    view.expected_reached = exp.get("reached")
    view.expected_no_fabrication_keywords = list(exp.get("no_fabrication_keywords") or [])
    return view


def _load_from_test_harness_partial(raw: dict) -> SessionView:
    """Older test harness JSONs (no final_state). Best-effort reconstruction
    from `log`, `outcomes`, `gate_traces`."""
    view = SessionView()
    view.session_id = str(raw.get("conv_id") or "")
    view.test_id = raw.get("name")
    view.timestamp = raw.get("timestamp", "")
    view.description = raw.get("description", "")

    outcomes = raw.get("outcomes") or {}
    view.locked_question = str(outcomes.get("locked_question") or "")
    view.locked_answer = str(outcomes.get("locked_answer") or "")
    view.locked_answer_aliases = list(outcomes.get("locked_answer_aliases") or [])
    view.final_student_reached_answer = bool(outcomes.get("final_reached") or False)
    view.final_phase = str(outcomes.get("phase_final") or "")
    view.final_hint_level = int(outcomes.get("hint_level_final") or 0)
    view.final_turn_count = int(outcomes.get("turn_count") or 0)

    exp = raw.get("expectations") or {}
    view.expected_reached = exp.get("reached")
    view.expected_no_fabrication_keywords = list(exp.get("no_fabrication_keywords") or [])

    # Reconstruct turns from `log`. The log alternates step entries:
    # tutoring_turn_N_student → tutoring_turn_N_after_invoke (tutor reply).
    log = raw.get("log") or []
    pending_student = ""
    pending_turn_id = None
    for entry in log:
        step = entry.get("step", "")
        if step.startswith("tutoring_turn_") and step.endswith("_student"):
            try:
                pending_turn_id = int(step.split("_")[2])
            except Exception:
                pending_turn_id = None
            pending_student = str(entry.get("student") or "")
        elif step.startswith("tutoring_turn_") and step.endswith("_after_invoke"):
            view.turns.append(TutorTurn(
                turn_id=pending_turn_id or len(view.turns) + 1,
                phase=str(entry.get("phase") or "tutoring"),
                student_msg=pending_student,
                tutor_msg=str(entry.get("tutor") or ""),
                student_state=entry.get("student_state"),
                student_reached_answer=bool(entry.get("student_reached_answer") or False),
                student_answer_confidence=float(entry.get("student_answer_confidence") or 0.0),
                hint_level=int(entry.get("hint_level") or 0),
            ))
            pending_student = ""
            pending_turn_id = None

    # Cost rollups
    dbg = raw.get("debug_summary") or {}
    view.api_calls = int(dbg.get("api_calls") or 0)
    view.cost_usd = float(dbg.get("cost_usd") or 0.0)
    view.input_tokens = int(dbg.get("input_tokens") or 0)
    view.output_tokens = int(dbg.get("output_tokens") or 0)

    # Pull gate traces into per-turn fields if turn order matches
    gate_traces = raw.get("gate_traces") or []
    confidence_entries = [e for e in gate_traces if e.get("wrapper") == "dean.confidence_score"]
    for i, t in enumerate(view.turns):
        if i < len(confidence_entries):
            t.gate_path = confidence_entries[i].get("gate_path")
            t.gate_evidence = str(confidence_entries[i].get("gate_evidence") or "")

    return view


def _load_from_production(state: dict) -> SessionView:
    """Production-format session export — full TutorState."""
    return _view_from_state(state)


def _view_from_state(state: dict) -> SessionView:
    """Build a SessionView from a full TutorState dict.
    Used by both the test-harness-with-final-state path and the production
    path."""
    view = SessionView()
    view.session_id = str(state.get("student_id") or "")
    view.locked_topic = dict(state.get("locked_topic") or {})
    view.locked_question = str(state.get("locked_question") or "")
    view.locked_answer = str(state.get("locked_answer") or "")
    view.locked_answer_aliases = list(state.get("locked_answer_aliases") or [])
    view.retrieved_chunks = list(state.get("retrieved_chunks") or [])

    # Walk messages to build turns
    messages = state.get("messages") or []
    rapport_seen = False
    pending_student: Optional[str] = None
    turn_id = 0
    for m in messages:
        role = (m or {}).get("role")
        content = str((m or {}).get("content") or "")
        phase = str((m or {}).get("phase") or "")
        if role == "tutor" and not rapport_seen:
            view.rapport_message = content
            rapport_seen = True
            continue
        if role == "student":
            pending_student = content
            continue
        if role == "tutor":
            turn_id += 1
            view.turns.append(TutorTurn(
                turn_id=turn_id,
                phase=phase or "tutoring",
                student_msg=pending_student or "",
                tutor_msg=content,
            ))
            pending_student = None

    # Final state fields
    view.final_phase = str(state.get("phase") or "")
    view.final_student_reached_answer = bool(state.get("student_reached_answer") or False)
    view.final_hint_level = int(state.get("hint_level") or 0)
    view.max_hints = int(state.get("max_hints") or 3)
    view.final_turn_count = int(state.get("turn_count") or 0)
    view.max_turns = int(state.get("max_turns") or 25)
    # L39 — session lifecycle status. Production exports may carry this
    # at the top level of `state` (set by memory_update_node) or under
    # debug. Default empty so legacy sessions don't break.
    view.status = str(state.get("status") or (state.get("debug") or {}).get("status") or "")
    view.student_mastery_confidence_last = float(state.get("student_mastery_confidence") or 0.0)

    # Debug rollups
    debug = state.get("debug") or {}
    view.api_calls = int(debug.get("api_calls") or 0)
    view.cost_usd = float(debug.get("cost_usd") or 0.0)
    view.input_tokens = int(debug.get("input_tokens") or 0)
    view.output_tokens = int(debug.get("output_tokens") or 0)
    view.interventions = int(debug.get("interventions") or 0)
    view.coverage_gap_events = int(debug.get("coverage_gap_events") or 0)
    view.grounded_turns = int(debug.get("grounded_turns") or 0)
    view.ungrounded_turns = int(debug.get("ungrounded_turns") or 0)
    view.invariant_violations = list(debug.get("invariant_violations") or [])
    view.retrieval_calls = int(debug.get("retrieval_calls") or 0)
    view.all_turn_traces = list(debug.get("all_turn_traces") or [])
    view.hint_progress = list(debug.get("hint_progress") or [])
    view.hint_plan = list(debug.get("hint_plan") or [])

    # Cross-reference gate traces from all_turn_traces back into per-turn fields
    _enrich_turns_from_traces(view, debug)

    return view


def _enrich_turns_from_traces(view: SessionView, debug: dict) -> None:
    """Walk all_turn_traces and the current turn_trace, tag per-turn fields
    on the matching TutorTurn entries (gate_path, qc_pass, intervention, etc.)."""
    # Combine historical + current trace lists, tagged by which turn each ran on
    historical = debug.get("all_turn_traces") or []
    current = debug.get("turn_trace") or []

    # all_turn_traces entries each have a `turn` field. For current, use the
    # final turn_count.
    final_turn = int(debug.get("turn_count") or len(view.turns))

    def apply_trace_to_turn(turn_no: int, trace_entries: list):
        # Find the SessionView turn matching this trace's turn number.
        # Note: SessionView.turns are 1-indexed in the order they appear.
        if turn_no < 1 or turn_no > len(view.turns):
            return
        t = view.turns[turn_no - 1]
        for entry in trace_entries:
            if not isinstance(entry, dict):
                continue
            wrap = entry.get("wrapper", "")
            if wrap == "dean.confidence_score":
                t.gate_path = entry.get("gate_path")
                t.gate_evidence = str(entry.get("gate_evidence") or "")
                # Per-turn `student_reached_answer` is encoded in the
                # `result` string ("reached=True" / "reached=False"). Parse
                # it so penalty checks (FABRICATION_AT_REACHED_FALSE) can
                # correctly skip turns where the gate genuinely fired.
                # Fallback: gate_path of "overlap"/"paraphrase" implies
                # reached=True; anything else implies False.
                result_str = str(entry.get("result") or "")
                if "reached=True" in result_str:
                    t.student_reached_answer = True
                elif "reached=False" in result_str:
                    t.student_reached_answer = False
                elif t.gate_path in {"overlap", "paraphrase"}:
                    t.student_reached_answer = True
                # Same for student_state — the result string isn't tagged
                # with student_state directly, but _setup_call writes it.
                # Look for it in setup_call entries below.
                # Confidence score
                m = re.search(r"answer_conf=([\d.]+)", result_str)
                if m:
                    try:
                        t.student_answer_confidence = float(m.group(1))
                    except ValueError:
                        pass
            elif wrap == "dean.reached_answer_gate":
                # L39 + Track 4.7g — v2 dean_node_v2 stamps reach gate
                # results under this wrapper name (replaces the legacy
                # `dean.confidence_score` "reached=..." string parsing).
                t.gate_path = entry.get("path")
                t.gate_evidence = str(entry.get("evidence") or "")
                if "reached" in entry:
                    t.student_reached_answer = bool(entry.get("reached"))
            elif wrap == "preflight":
                # L39 — v2 pre-flight Haiku trio. category in {help_abuse,
                # off_domain, deflection, none}. When fired, the v2 path
                # short-circuits Dean and Teacher renders a redirect; the
                # scorer should not penalize "no chunk grounding" on these
                # turns because no retrieval fires.
                t.preflight_fired = bool(entry.get("fired", False))
                t.preflight_category = str(entry.get("category") or "")
            elif wrap == "dean_v2.plan":
                # L39 — v2 Dean planning result. Captures mode + tone so
                # downstream dimension scorers can adjust expectations
                # (clinical-mode turns aren't graded as Socratic).
                t.turn_plan_mode = str(entry.get("mode") or "")
                t.turn_plan_tone = str(entry.get("tone") or "")
                t.dean_v2_used_fallback = bool(entry.get("used_fallback", False))
            elif wrap == "retry_orchestrator.run_turn":
                # L39 — v2 retry loop telemetry. Records how many
                # Teacher attempts + checks ran before final text shipped.
                t.retry_final_attempt = int(entry.get("final_attempt") or 0)
                t.retry_used_safe_probe = bool(entry.get("used_safe_generic_probe", False))
                t.retry_used_dean_replan = bool(entry.get("used_dean_replan", False))
                t.retry_n_attempts = int(entry.get("n_attempts") or 0)
            elif wrap == "dean._setup_call":
                # _setup_call writes turn classification result. Look for
                # the `result` summary which has the form
                # "student_state=correct, hint=2".
                result_str = str(entry.get("result") or "")
                m = re.search(r"student_state=(\w+)", result_str)
                if m and not t.student_state:
                    t.student_state = m.group(1)
                m2 = re.search(r"hint=(\d+)", result_str)
                if m2 and not t.hint_level:
                    try:
                        t.hint_level = int(m2.group(1))
                    except ValueError:
                        pass
            elif wrap == "dean._quality_check_call":
                de = entry.get("decision_effect", "")
                if de == "qc_pass":
                    t.qc_pass = True
                elif de == "qc_fail":
                    t.qc_pass = False
                rc = entry.get("reason_codes") or []
                if isinstance(rc, list):
                    t.qc_reason_codes = [str(c) for c in rc]
            elif wrap == "dean.fallback":
                t.intervention_used = True
            elif wrap == "dean.revised_teacher_draft_applied":
                t.revised_draft_applied = True

    for tr in historical:
        if not isinstance(tr, dict):
            continue
        turn_no = int(tr.get("turn") or 0)
        apply_trace_to_turn(turn_no, tr.get("trace") or [])

    if current:
        apply_trace_to_turn(final_turn, current)
