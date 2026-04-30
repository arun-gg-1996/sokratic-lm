"""
evaluation/quality/penalties.py
-------------------------------
Critical / Major penalty checks. These are SEPARATE from dimension scores —
a session with any Critical penalty is `failed_critical_penalty` regardless
of how the dimensions look.

See docs/EVALUATION_FRAMEWORK.md §4.
"""

from __future__ import annotations
from typing import Any, Optional
import re

from .schema import SessionView


# Severity tiers
CRITICAL = "critical"
MAJOR = "major"


def compute_penalties(
    view: SessionView,
    deterministic_results: dict[str, Any],
    llm_per_turn: Optional[dict],
    llm_synthesis: Optional[dict],
) -> list[dict]:
    """Return a list of penalty records, each with:
      {code, severity, evidence, turn_id (optional), source (det|llm)}
    """
    out: list[dict] = []

    # ---------------------------------------------- LEAK_DETECTED (Critical)
    # Source: per-turn LLM saying no_reveal=0, OR our deterministic regex
    # matching a leak.
    if llm_per_turn and llm_per_turn.get("turns"):
        for t in llm_per_turn["turns"]:
            no_reveal = t.get("no_reveal")
            if no_reveal is not None and no_reveal < 0.5:
                tid = t.get("turn_id")
                # Find the actual turn for evidence
                tutor_msg = ""
                for vt in view.turns:
                    if vt.turn_id == tid:
                        tutor_msg = vt.tutor_msg
                        break
                out.append({
                    "code": "LEAK_DETECTED",
                    "severity": CRITICAL,
                    "evidence": f"turn {tid}: no_reveal={no_reveal} — {tutor_msg[:200]}",
                    "turn_id": tid,
                    "source": "llm",
                })

    # ---------------------------------------------- INVARIANT_VIOLATION (Critical)
    # Change (2026-04-30, post-18-convo eval review): emit ONE penalty per
    # session regardless of how many violation entries exist in
    # state.debug.invariant_violations. The previous implementation
    # appended one penalty per violation, which produced 14-19 critical
    # penalties on long timeout sessions and dominated the penalty
    # histogram (110/155 events across the 18-convo batch). Same severity,
    # same code, but the count now reflects "session has any invariant
    # violation" not "session has N violations". Detail is preserved in
    # the evidence string + the raw count.
    if view.invariant_violations:
        kinds = sorted({
            str(v.get("kind", "unknown")) for v in view.invariant_violations
            if isinstance(v, dict)
        })
        n = len(view.invariant_violations)
        out.append({
            "code": "INVARIANT_VIOLATION",
            "severity": CRITICAL,
            "evidence": (
                f"{n} invariant violation(s) on this session "
                f"(kinds: {', '.join(kinds) if kinds else 'unknown'}); "
                f"first: kind={view.invariant_violations[0].get('kind') if isinstance(view.invariant_violations[0], dict) else 'n/a'} "
                f"turn={view.invariant_violations[0].get('turn') if isinstance(view.invariant_violations[0], dict) else 'n/a'}"
            ),
            "raw_count": n,
            "source": "det",
        })

    # ---------------------------------------------- FABRICATION_AT_REACHED_FALSE (Critical)
    fab_turns = deterministic_results.get("fabrication_turns") or []
    for f in fab_turns:
        out.append({
            "code": "FABRICATION_AT_REACHED_FALSE",
            "severity": CRITICAL,
            "evidence": f"turn {f.get('turn_id')}: '{f.get('evidence')}' — {f.get('tutor_msg', '')[:160]}",
            "turn_id": f.get("turn_id"),
            "source": "det",
        })
    # ALSO check LLM fabrication detection (catches subtler cases regex missed)
    if llm_per_turn and llm_per_turn.get("turns"):
        for t in llm_per_turn["turns"]:
            if bool(t.get("fabrication_detected")):
                tid = t.get("turn_id")
                # Avoid duplicate entry for the same turn already flagged by regex
                already = any(
                    p.get("code") == "FABRICATION_AT_REACHED_FALSE" and p.get("turn_id") == tid
                    for p in out
                )
                if not already:
                    out.append({
                        "code": "FABRICATION_AT_REACHED_FALSE",
                        "severity": CRITICAL,
                        "evidence": f"turn {tid}: LLM-detected — '{t.get('fabrication_evidence', '')[:160]}'",
                        "turn_id": tid,
                        "source": "llm",
                    })

    # ---------------------------------------------- OFF_TOPIC_DRIFT_NOT_REDIRECTED (Major)
    # If off-topic was attempted AND llm_synthesis off_topic_drift_score < 0.7
    if llm_synthesis is not None:
        ot_score = llm_synthesis.get("off_topic_drift_score")
        if ot_score is not None and ot_score < 0.7:
            ot_turns = deterministic_results.get("off_topic_turns") or []
            out.append({
                "code": "OFF_TOPIC_DRIFT_NOT_REDIRECTED",
                "severity": MAJOR,
                "evidence": f"off_topic_drift_score={ot_score}; turns with off-topic content: {ot_turns}",
                "source": "llm",
            })

    # ---------------------------------------------- MASTERY_OVERCLAIM (Major)
    # Triggers if: (a) student didn't reach (b) mastery > 0.7
    # OR if llm_synthesis flagged mastery_overclaim_semantic
    if (not view.final_student_reached_answer
            and view.mastery_score is not None
            and view.mastery_score > 0.7):
        out.append({
            "code": "MASTERY_OVERCLAIM",
            "severity": MAJOR,
            "evidence": f"mastery_score={view.mastery_score} but final reached=False",
            "source": "det",
        })
    if llm_synthesis is not None and bool(llm_synthesis.get("mastery_overclaim_semantic")):
        out.append({
            "code": "MASTERY_OVERCLAIM",
            "severity": MAJOR,
            "evidence": "LLM-flagged mastery overclaim in session-end summary",
            "source": "llm",
        })

    # ---------------------------------------------- HELP_ABUSE_RESPONDED_WITH_ANSWER (Major)
    if llm_synthesis is not None and bool(llm_synthesis.get("help_abuse_responded_with_answer")):
        out.append({
            "code": "HELP_ABUSE_RESPONDED_WITH_ANSWER",
            "severity": MAJOR,
            "evidence": "LLM-flagged: tutor revealed/paraphrased locked_answer in response to help-abuse pattern",
            "source": "llm",
        })

    return out


def compute_verdict(
    primary: dict[str, Any],
    dimensions: dict[str, dict[str, Any]],
    penalties: list[dict],
) -> str:
    """Combine the three signals into a single verdict string.
    Possible values: 'passed' | 'warning' | 'failed_critical_penalty' | 'failed_threshold'
    """
    if any(p.get("severity") == CRITICAL for p in penalties):
        return "failed_critical_penalty"

    # Count failed dimensions (score < threshold)
    failed_dims = [name for name, d in dimensions.items() if d.get("passes") is False]
    has_major = any(p.get("severity") == MAJOR for p in penalties)
    primary_passing = (
        (primary.get("EULER", {}).get("passes_all_thresholds") is not False)
        and (primary.get("RAGAS", {}).get("passes_all_thresholds") is not False)
    )

    if not primary_passing or failed_dims:
        return "failed_threshold" if not primary_passing else "warning"
    if has_major:
        return "warning"
    return "passed"
