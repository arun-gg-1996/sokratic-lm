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
want to end the session.

DEFLECTION patterns (verdict="deflection"):
  * Explicit: "let's stop", "I want to end this", "can we finish?",
    "I'm done", "let's wrap up", "stop"
  * Implicit fatigue: "I'm tired, can we stop?", "this is taking
    too long, let's wrap up", "can we be done with this?"
  * Time signals: "I have to go", "I need to leave"

NOT deflection (verdict="continuing"):
  * Just expressing fatigue without end-request: "this is hard"
  * Asking for break in pacing: "can we slow down"
  * Frustration without end-request: "I don't get it"
  * Tangent questions: "what about X?"

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
    """L44 + L45 + L55 + L56 + L58 — run 3 Haiku checks, apply counter
    logic, return PreflightResult.

    `state` is the live TutorState (read-only here — caller writes back
    counter updates from PreflightResult fields). Reads:
      state.help_abuse_count, state.off_topic_count
    Counters are 0 by default for fresh sessions.

    `student_message` is the latest student utterance (the one being
    classified).

    `locked_topic` (optional) is the current locked subsection — only
    relevant for off_domain detection's locked-topic argument; the
    existing haiku_off_domain_check accepts it.

    `parallel=True` (default) runs all 3 Haiku calls concurrently via a
    thread pool (lowest-latency path). `parallel=False` runs sequentially
    — useful for tests + debugging.
    """
    t0 = time.time()
    help_count = int(state.get("help_abuse_count") or 0)
    off_count = int(state.get("off_topic_count") or 0)

    locked_str = ""
    if locked_topic:
        locked_str = (
            locked_topic.get("subsection")
            or locked_topic.get("section")
            or ""
        )

    def _run_help_abuse():
        return haiku_help_abuse_check(student_message)

    def _run_off_domain():
        # haiku_off_domain_check signature is (student_msg) — no locked_topic
        # arg today. The check is locked-topic-agnostic; we still capture
        # locked_str into trace via the orchestrator for context.
        return C.haiku_off_domain_check(student_message)

    def _run_deflection():
        return haiku_deflection_check(student_message)

    if parallel:
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(_run_help_abuse): "help_abuse",
                pool.submit(_run_off_domain): "off_domain",
                pool.submit(_run_deflection): "deflection",
            }
            for f in as_completed(futures):
                results[futures[f]] = f.result()
    else:
        results = {
            "help_abuse": _run_help_abuse(),
            "off_domain": _run_off_domain(),
            "deflection": _run_deflection(),
        }

    elapsed = round(time.time() - t0, 3)

    # Categorize. Note: existing haiku_off_domain_check (Nidhi's branch)
    # collapses the L56 granular categories (substance/chitchat/jailbreak/
    # answer_demand) into a single "off_domain" verdict. We accept both
    # the granular set (forward-compat for when L56 categories land in
    # the classifier) AND the binary "off_domain" verdict.
    help_fired = results["help_abuse"]["verdict"] == "help_abuse"
    off_fired = results["off_domain"]["verdict"] in {
        "off_domain",  # binary (today)
        "substance", "chitchat", "jailbreak", "answer_demand",  # granular (per L56, future)
    }
    deflect_fired = results["deflection"]["verdict"] == "deflection"

    # Decision: deflection > off_domain > help_abuse > none
    # (deflection is the most user-explicit signal; off_domain has
    # session-end consequences at strike 4; help_abuse is the lightest)
    if deflect_fired:
        return PreflightResult(
            fired=True,
            category="deflection",
            evidence=results["deflection"]["evidence"],
            rationale=results["deflection"]["rationale"],
            new_help_abuse_count=help_count,    # not advanced
            new_off_topic_count=off_count,      # not advanced
            suggested_mode="confirm_end",
            suggested_tone="neutral",
            checks=results,
            elapsed_s=elapsed,
        )

    if off_fired:
        new_off = off_count + 1
        end_session = new_off >= OFF_TOPIC_END_SESSION_STRIKE
        return PreflightResult(
            fired=True,
            category="off_domain",
            evidence=results["off_domain"]["evidence"],
            rationale=results["off_domain"]["rationale"],
            new_help_abuse_count=help_count,    # reset on off_domain to avoid double-penalize
            new_off_topic_count=new_off,
            suggested_mode="honest_close" if end_session else "nudge",
            suggested_tone=_select_tone_for_strike("off_domain", new_off),
            should_end_session=end_session,
            checks=results,
            elapsed_s=elapsed,
        )

    if help_fired:
        new_help = help_count + 1
        force_hint = new_help >= HELP_ABUSE_HINT_ADVANCE_STRIKE
        return PreflightResult(
            fired=True,
            category="help_abuse",
            evidence=results["help_abuse"]["evidence"],
            rationale=results["help_abuse"]["rationale"],
            new_help_abuse_count=new_help,
            new_off_topic_count=off_count,
            suggested_mode="redirect",
            suggested_tone=_select_tone_for_strike("help_abuse", new_help),
            should_force_hint_advance=force_hint,
            checks=results,
            elapsed_s=elapsed,
        )

    # All 3 checks passed → Dean runs. Reset help_abuse counter (per L55:
    # any non-help-abuse engagement resets the counter). Off-domain counter
    # is independent — only resets on detected on-domain engagement, which
    # isn't reliably knowable until Dean processes the message; conservative
    # default is to leave off_count unchanged.
    return PreflightResult(
        fired=False,
        category="none",
        new_help_abuse_count=0,        # reset
        new_off_topic_count=off_count, # leave alone
        checks=results,
        elapsed_s=elapsed,
    )
