"""
conversation/preflight.py
─────────────────────────
Pre-flight Haiku layer per L44 + L45 + L55 + L56 + L58 (Track 4.3).

Three cheap Haiku checks fire in parallel BEFORE Dean's Sonnet planner.
If any catches the turn, Dean is SKIPPED and Teacher writes a redirect /
nudge / confirm message via the standard TurnPlan contract.

  haiku_help_abuse   — L55: catches "just tell me", "idk" stalling
  haiku_off_domain   — L56: full off-domain (#1 — chitchat / jailbreak)
                       (REUSES existing classifiers.haiku_off_domain_check)
  haiku_deflection   — L58: catches session-end requests
                       ("let's stop", "I'm done", "can we wrap up?")

Cost: ~$0.0003 / turn (3× Haiku) vs ~$0.038 / turn for full Dean Sonnet
when pre-flight skip applies. Median session has ~30% turns where one
pre-flight fires (per Nidhi's eval), saving ~$0.012 / session.

Counters per L55 / L56 / L58:
  * help_abuse_count    — strike-based; at strike 4 → force hint advance
  * off_topic_count     — strike-based; at strike 4 → graceful end
  * deflection         — NO counter; each detection independently confirmed

Public entry point:
  run_preflight(state, student_message, *, client) → PreflightResult

PreflightResult.fired tells the caller whether to skip Dean. The
result also carries (a) which check fired (b) the updated counters
(c) a "should_force_hint_advance" flag (per L55 strike-4 logic) and
(d) a "should_end_session" flag (per L56 strike-4 logic).

This module is the WIRING. The actual Haiku calls live in
classifiers.py (haiku_off_domain_check exists today; help_abuse and
deflection are added below as dedicated functions but follow the same
pattern as the existing classifiers — single-shot, cached system block,
strict JSON, evidence-quote validation).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from conversation import classifiers as C
from conversation.preflight_classifier import haiku_intent_classify_unified

# Strike thresholds per L55 / L56
HELP_ABUSE_HINT_ADVANCE_STRIKE = 4    # at strike 4 → force hint_level += 1
OFF_TOPIC_END_SESSION_STRIKE = 4      # at strike 4 → graceful end


# ─────────────────────────────────────────────────────────────────────────────
# NEW classifier — haiku_help_abuse (L55)
# ─────────────────────────────────────────────────────────────────────────────

_HELP_ABUSE_SYSTEM = """\
You are a teaching-quality classifier for a Socratic tutor. Your sole
task: detect HELP-ABUSE patterns where the student is trying to
short-circuit the Socratic process instead of attempting reasoning.

HELP-ABUSE patterns (verdict="help_abuse"):
  * Direct demand: "just tell me", "what's the answer", "give me the
    answer", "stop quizzing me, what is it"
  * Total non-engagement: "idk", "no idea", "i don't know" with no
    attempt at partial reasoning
  * Repeated stalling: "i forget", "i don't remember", "skip", "next"
    with no engagement signal
  * Demanding simplification: "make it easier", "this is too hard,
    just explain it"

NOT help-abuse (verdict="legitimate_engagement"):
  * Partial answers: "is it the heart?", "maybe the SA node?", even
    if wrong
  * Asking for clarification: "what do you mean by impulse?",
    "rephrase the question?"
  * Hedging while reasoning: "i think it might be... not sure"
  * Asking about adjacent concepts: "what about the AV node?"

Output STRICT JSON only — no markdown, no preamble:
{
  "verdict": "help_abuse" | "legitimate_engagement",
  "evidence": "<verbatim substring of message that triggered help_abuse; empty otherwise>",
  "rationale": "<1-sentence explanation>"
}
"""


_HELP_ABUSE_USER_TEMPLATE = """\
STUDENT MESSAGE:
{message}
"""


def haiku_help_abuse_check(student_message: str) -> dict:
    """L55 — single Haiku call detecting help-abuse patterns.

    Returns dict (legacy verdict-string shape, normalize via
    classifiers.to_universal_check_result if needed):
      verdict:    "help_abuse" | "legitimate_engagement"
      evidence:   verbatim substring (empty if legitimate)
      rationale:  1-sentence explanation
      _elapsed_s: wall time
      _raw:       raw response (debug)
      _error:     "parse_fail" | "evidence_invalid" | "" (empty on success)

    Safe defaults on error: verdict="legitimate_engagement" (don't
    false-fire — would block legit engagement).
    """
    t0 = time.time()
    if not student_message or not student_message.strip():
        return {
            "verdict": "legitimate_engagement", "evidence": "",
            "rationale": "empty message",
            "_elapsed_s": 0.0, "_raw": "", "_error": "",
        }
    user_text = _HELP_ABUSE_USER_TEMPLATE.format(message=student_message)
    try:
        raw = C._haiku_call(C._cached_system_block(_HELP_ABUSE_SYSTEM), user_text)
    except Exception as e:
        return {
            "verdict": "legitimate_engagement", "evidence": "",
            "rationale": f"haiku_call_error: {type(e).__name__}",
            "_elapsed_s": round(time.time() - t0, 3),
            "_raw": "", "_error": "haiku_error",
        }
    elapsed = round(time.time() - t0, 3)
    parsed = C._extract_json(raw)
    if parsed is None:
        return {
            "verdict": "legitimate_engagement", "evidence": "",
            "rationale": "json_parse_fail",
            "_elapsed_s": elapsed, "_raw": raw, "_error": "parse_fail",
        }
    verdict = str(parsed.get("verdict", "legitimate_engagement")).strip().lower()
    if verdict not in {"help_abuse", "legitimate_engagement"}:
        verdict = "legitimate_engagement"
    evidence = str(parsed.get("evidence", "") or "")
    rationale = str(parsed.get("rationale", "") or "")[:240]
    error = ""
    if verdict == "help_abuse" and evidence and not C._validate_evidence(evidence, student_message):
        verdict = "legitimate_engagement"
        evidence = ""
        error = "evidence_invalid"
    return {
        "verdict": verdict,
        "evidence": evidence,
        "rationale": rationale,
        "_elapsed_s": elapsed,
        "_raw": raw,
        "_error": error,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW classifier — haiku_deflection (L58)
# ─────────────────────────────────────────────────────────────────────────────

_DEFLECTION_SYSTEM = """\
You are a teaching-quality classifier for a Socratic tutor. Your sole
task: detect DEFLECTION patterns where the student is signaling they
want to end or decline the session.

DEFLECTION patterns (verdict="deflection"):
  * Explicit end-request: "let's stop", "I want to end this",
    "can we finish?", "I'm done", "let's wrap up", "stop"
  * Implicit fatigue + end signal: "I'm tired, can we stop?",
    "this is taking too long, let's wrap up", "can we be done"
  * Time signals: "I have to go", "I need to leave", "gotta run"
  * Decline-to-engage at session start: "no thanks, not today",
    "not really", "maybe another time", "no I'm good", "I'll pass" —
    treat as deflection when student declines the tutor's offer to
    begin a session, even without explicit "stop" wording. This is
    the rapport-decline path: student doesn't want to engage at all.

NOT deflection (verdict="continuing"):
  * Just expressing fatigue without end-request: "this is hard"
  * Asking for break in pacing: "can we slow down"
  * Frustration without end-request: "I don't get it"
  * Tangent questions: "what about X?"
  * Declining ONE option but still engaging: "not that topic, how
    about Y instead" — student wants to continue, just on a different
    topic.

Output STRICT JSON only — no markdown, no preamble:
{
  "verdict": "deflection" | "continuing",
  "evidence": "<verbatim substring of message that triggered deflection; empty otherwise>",
  "rationale": "<1-sentence explanation>"
}
"""


_DEFLECTION_USER_TEMPLATE = """\
STUDENT MESSAGE:
{message}
"""


def haiku_deflection_check(student_message: str) -> dict:
    """L58 — single Haiku call detecting session-end signals.

    Returns dict (legacy verdict-string shape):
      verdict:    "deflection" | "continuing"
      evidence:   verbatim substring (empty if continuing)
      rationale:  1-sentence explanation
      _elapsed_s, _raw, _error: same diagnostics as other checks

    Safe defaults on error: verdict="continuing".
    """
    t0 = time.time()
    if not student_message or not student_message.strip():
        return {
            "verdict": "continuing", "evidence": "", "rationale": "empty message",
            "_elapsed_s": 0.0, "_raw": "", "_error": "",
        }
    user_text = _DEFLECTION_USER_TEMPLATE.format(message=student_message)
    try:
        raw = C._haiku_call(C._cached_system_block(_DEFLECTION_SYSTEM), user_text)
    except Exception as e:
        return {
            "verdict": "continuing", "evidence": "",
            "rationale": f"haiku_call_error: {type(e).__name__}",
            "_elapsed_s": round(time.time() - t0, 3),
            "_raw": "", "_error": "haiku_error",
        }
    elapsed = round(time.time() - t0, 3)
    parsed = C._extract_json(raw)
    if parsed is None:
        return {
            "verdict": "continuing", "evidence": "",
            "rationale": "json_parse_fail",
            "_elapsed_s": elapsed, "_raw": raw, "_error": "parse_fail",
        }
    verdict = str(parsed.get("verdict", "continuing")).strip().lower()
    if verdict not in {"deflection", "continuing"}:
        verdict = "continuing"
    evidence = str(parsed.get("evidence", "") or "")
    rationale = str(parsed.get("rationale", "") or "")[:240]
    error = ""
    if verdict == "deflection" and evidence and not C._validate_evidence(evidence, student_message):
        verdict = "continuing"
        evidence = ""
        error = "evidence_invalid"
    return {
        "verdict": verdict,
        "evidence": evidence,
        "rationale": rationale,
        "_elapsed_s": elapsed,
        "_raw": raw,
        "_error": error,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight orchestrator (L44 + L55 + L56 + L58)
# ─────────────────────────────────────────────────────────────────────────────


PreflightCategory = Literal[
    "none",                # all 3 checks passed → Dean runs
    "help_abuse",          # → Teacher writes redirect, counter increments
    "off_domain",          # → Teacher writes nudge (escalating tone), counter increments
    "deflection",          # → Teacher writes confirm message + UI button
]


@dataclass
class PreflightResult:
    """Outcome of running the 3 parallel checks + counter logic.

    `fired` is the canonical signal — if True, caller skips Dean and
    drives Teacher with the suggested mode/tone. If False, all 3 checks
    passed and Dean runs normally.

    Counter updates are NOT applied to state by this class — caller
    persists `new_help_abuse_count` / `new_off_topic_count` back into
    state. Keeps this orchestrator pure / easy to test.
    """
    fired: bool
    category: PreflightCategory
    evidence: str = ""
    rationale: str = ""

    # Counter state after this turn (caller writes back to state)
    new_help_abuse_count: int = 0
    new_off_topic_count: int = 0

    # Teacher rendering hints — caller fills TurnPlan from these
    suggested_mode: str = ""           # "redirect" | "nudge" | "confirm_end"
    suggested_tone: str = "neutral"    # "neutral" → "firm" → "honest" with strikes

    # Strike-driven actions per L55 / L56
    should_force_hint_advance: bool = False  # L55: at help_abuse strike 4
    should_end_session: bool = False         # L56: at off_topic strike 4

    # Per-check raw results for trace
    checks: dict = field(default_factory=dict)

    # Wall-clock for the whole pre-flight
    elapsed_s: float = 0.0


def _select_tone_for_strike(category: PreflightCategory, strike: int) -> str:
    """Per L56 / L55: tone escalates with strike count.
    strike 1 = neutral, strike 2 = firm, strike 3 = firm, strike 4 = honest (terminal).
    Help-abuse uses neutral throughout (the hint advance does the work)."""
    if category == "deflection":
        return "neutral"
    if category == "help_abuse":
        return "neutral"  # Teacher's redirect prompt handles the gentle nudge
    if category == "off_domain":
        if strike >= 4:
            return "honest"  # graceful end uses honest-tone close
        if strike >= 2:
            return "firm"
        return "neutral"
    return "neutral"


def run_preflight(
    state: dict,
    student_message: str,
    *,
    locked_topic: Optional[dict] = None,
    parallel: bool = True,
) -> PreflightResult:
    """M7 — single Haiku unified intent classifier (replaces 3 separate
    Haiku calls). Sees locked_topic + last 2 turn pairs + phase so it
    can disambiguate context-dependent words ("yes"/"no"/topic mentions).

    Strike counters decay on on_topic_engaged turns to prevent a single
    misclassification 10 turns ago from killing a session at strike 4.

    The `parallel` kwarg is kept for backward compat but ignored (single
    call now).
    """
    _ = parallel  # kept for backwards compat
    t0 = time.time()
    help_count = int(state.get("help_abuse_count") or 0)
    off_count = int(state.get("off_topic_count") or 0)

    locked_subsection = ""
    if locked_topic:
        locked_subsection = (
            locked_topic.get("subsection")
            or locked_topic.get("section")
            or ""
        )

    # Build last 2 (tutor, student) turn pairs from messages history.
    history_pairs: list[tuple[str, str]] = []
    msgs = list(state.get("messages") or [])
    # Walk from end, collect tutor msg + the immediately following student msg.
    # Limit to last 4 exchanges.
    pairs_buf: list[dict] = []
    for m in msgs[-8:]:
        pairs_buf.append(m)
    cur_tutor = ""
    for m in pairs_buf:
        role = (m or {}).get("role") or ""
        content = str((m or {}).get("content") or "").strip()
        if role == "tutor":
            cur_tutor = content
        elif role == "student" and cur_tutor:
            history_pairs.append((cur_tutor, content))
            cur_tutor = ""
    history_pairs = history_pairs[-2:]

    # BLOCK 14 (S4) — deterministic rapport-decline shortcut.
    # When phase=rapport AND message clearly declines the session
    # (NOT just declining one topic), short-circuit directly to close.
    # No modal — student hasn't invested any progress to confirm-exit
    # over. The unified classifier is conservative on ambiguous "no
    # thanks" at the rapport stage and under-fires; this shortcut
    # catches the clear decline patterns deterministically.
    # Detect "rapport-stage" — phase technically transitions to "tutoring"
    # after rapport_node runs the greeting, but no topic is locked yet.
    # That's the moment "no thanks" should mean "decline session" not
    # "decline this topic".
    cur_phase = str(state.get("phase") or "tutoring").lower()
    is_rapport_stage = (
        cur_phase == "rapport"
        or (cur_phase == "tutoring"
            and not state.get("topic_confirmed")
            and not (state.get("locked_topic") or {}).get("path"))
    )
    if is_rapport_stage:
        msg_lower = (student_message or "").strip().lower()
        # Patterns that mean "I don't want to do this session"
        # (not "I want a different topic")
        rapport_decline_patterns = [
            "no thanks",
            "no thank you",
            "not today",
            "maybe later",
            "maybe another time",
            "i'll pass",
            "i will pass",
            "no i'm good",
            "no im good",
            "not interested",
        ]
        if any(p in msg_lower for p in rapport_decline_patterns):
            # Force-route to memory_update with exit_intent close (no save).
            # Caller in nodes.py rapport_node respects state.phase.
            state["phase"] = "memory_update"
            state["exit_intent_pending"] = True
            state["close_reason"] = "exit_intent"
            return PreflightResult(
                fired=True,
                category="deflection",
                evidence=student_message[:120],
                rationale="rapport-decline pattern (BLOCK 14 deterministic shortcut, direct-to-close)",
                new_help_abuse_count=help_count,
                new_off_topic_count=off_count,
                suggested_mode="honest_close",
                suggested_tone="neutral",
                should_end_session=True,
                checks={"rapport_decline_shortcut": True},
                elapsed_s=round(time.time() - t0, 3),
            )

    result = haiku_intent_classify_unified(
        student_message,
        history_pairs=history_pairs,
        locked_subsection=locked_subsection,
        locked_question=str(state.get("locked_question") or ""),
        phase=str(state.get("phase") or "tutoring"),
    )
    verdict = str(result.get("verdict", "on_topic_engaged"))
    elapsed = round(time.time() - t0, 3)
    # Trace bag — keep the 3-key shape so existing dashboards still work.
    checks = {
        "unified": result,
        # Synthesize per-check shape so legacy trace consumers don't break:
        "help_abuse": {"verdict": "help_abuse" if verdict == "help_abuse" else "legitimate_engagement"},
        "off_domain": {"verdict": "off_domain" if verdict == "off_domain" else "clean"},
        "deflection": {"verdict": "deflection" if verdict == "deflection" else "continuing"},
    }

    if verdict == "deflection":
        return PreflightResult(
            fired=True,
            category="deflection",
            evidence=result.get("evidence", ""),
            rationale=result.get("rationale", ""),
            new_help_abuse_count=help_count,
            new_off_topic_count=off_count,
            suggested_mode="confirm_end",
            suggested_tone="neutral",
            checks=checks,
            elapsed_s=elapsed,
        )

    if verdict == "off_domain":
        new_off = off_count + 1
        end_session = new_off >= OFF_TOPIC_END_SESSION_STRIKE
        return PreflightResult(
            fired=True,
            category="off_domain",
            evidence=result.get("evidence", ""),
            rationale=result.get("rationale", ""),
            new_help_abuse_count=help_count,
            new_off_topic_count=new_off,
            suggested_mode="honest_close" if end_session else "nudge",
            suggested_tone=_select_tone_for_strike("off_domain", new_off),
            should_end_session=end_session,
            checks=checks,
            elapsed_s=elapsed,
        )

    if verdict == "help_abuse":
        new_help = help_count + 1
        force_hint = new_help >= HELP_ABUSE_HINT_ADVANCE_STRIKE
        # 2026-05-05: when threshold fires, reset the counter so the student
        # gets a fresh warning chain before the NEXT hint-advance. Without
        # this, every strike past 4 immediately re-fires force_hint_advance
        # — burning the student's 3-hint allotment in 3 consecutive strikes
        # and surfacing a confusing "Help-abuse: 5/4" UI. Tone selection
        # uses the pre-reset count so the message still escalates correctly.
        reported_count = new_help
        if force_hint:
            new_help = 0
        return PreflightResult(
            fired=True,
            category="help_abuse",
            evidence=result.get("evidence", ""),
            rationale=result.get("rationale", ""),
            new_help_abuse_count=new_help,
            new_off_topic_count=off_count,
            suggested_mode="redirect",
            suggested_tone=_select_tone_for_strike("help_abuse", reported_count),
            should_force_hint_advance=force_hint,
            checks=checks,
            elapsed_s=elapsed,
        )

    # BLOCK 6 (S1) — low_effort: passive non-engagement ("idk", "i don't
    # know"). Increment consecutive_low_effort_count so Dean (BLOCK 7)
    # can escalate strategy after the 2nd-3rd in a row. NOT firing
    # preflight intervention itself — Dean still plans normally but with
    # awareness of the streak.
    if verdict == "low_effort":
        prev_streak = int(state.get("consecutive_low_effort_count", 0) or 0)
        state["consecutive_low_effort_count"] = prev_streak + 1
        return PreflightResult(
            fired=False,         # Dean still plans; this is a soft signal
            category="low_effort",  # but trace + history annotation reflect it
            evidence=result.get("evidence", ""),
            rationale=result.get("rationale", ""),
            new_help_abuse_count=help_count,
            new_off_topic_count=off_count,
            checks=checks,
            elapsed_s=elapsed,
        )

    # on_topic_engaged or opt_in_* in preflight context — let Dean handle.
    # M7 strike decay: on engagement, decrement off_topic_count (max 0)
    # so a single old misclassification doesn't accumulate to strike 4.
    # BLOCK 6: also reset consecutive_low_effort_count on real engagement.
    new_off = max(0, off_count - 1)
    state["consecutive_low_effort_count"] = 0
    return PreflightResult(
        fired=False,
        category=verdict if verdict in {"on_topic_engaged", "opt_in_yes", "opt_in_no", "opt_in_ambiguous"} else "none",
        new_help_abuse_count=0,        # reset (existing L55 behavior)
        new_off_topic_count=new_off,   # M7 decay
        checks=checks,
        elapsed_s=elapsed,
    )
