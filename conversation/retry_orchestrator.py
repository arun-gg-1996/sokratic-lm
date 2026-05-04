"""
conversation/retry_orchestrator.py
──────────────────────────────────
L50 + L62 implementation — bounded retry loop wrapping Teacher draft +
4 parallel Haiku self-policing checks + Dean re-plan + safe-generic-probe
fallback.

Per L50:

  attempt 1: teacher.draft(turn_plan, ...)
    parallel haiku × 4 → if all pass, ship

  attempt 2: teacher.draft(turn_plan, ..., prior_attempt=draft_1, ...)
    parallel haiku × 4 → if all pass, ship

  attempt 3: teacher.draft(turn_plan, ..., prior_attempts=[d1,d2], ...)
    parallel haiku × 4 → if all pass, ship

  all 3 fail → Dean re-plans ONCE:
    new_turn_plan = dean.replan(...)
    draft_4 = teacher.draft(new_turn_plan, ...)
    parallel haiku × 4 →
      if all pass → ship draft_4
      if leak_check FAILS → DO NOT SHIP draft_4 — fall back to safe generic probe
      if other check fails → ship draft_4 anyway (less critical than leak)

CRITICAL SAFETY (Codex round-1 fix #5): never ship a draft that fails
haiku_leak_check. Leaks are the highest-stakes failure mode.

Safe generic probe: deterministic, templated, NO LLM call, GUARANTEED
non-leak. Used only when leak_check still fails after the full retry
chain. Logged to state.debug.turn_trace with leak_cap_fallback_fired=True.
Hint level NOT incremented (the turn is "wasted" but no leak).

Hard timeout per turn: 30 seconds wall-clock. If exceeded → safe-generic-
probe (same fallback path).

Per L62: each attempt's failure detail is fed BACK into the next
attempt's prompt + into Dean's re-plan input.

Cost / latency
--------------
Worst case per turn: 4 Teacher drafts (~$0.018 ea Sonnet) + 16 Haiku
checks (~$0.0002 ea) + 1 Dean re-plan (~$0.018) + maybe 1 fallback
render = ~$0.10 / turn. Average case (turn passes attempt 1): 1
Teacher draft + 4 Haiku = ~$0.020 / turn.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from conversation import classifiers as C
from conversation.dean_v2 import DeanV2
from conversation.teacher_v2 import (
    TeacherDraftResult,
    TeacherPromptInputs,
    TeacherV2,
)
from conversation.turn_plan import TurnPlan


# Bounded by L50
MAX_TEACHER_ATTEMPTS = 3       # Then Dean re-plans + 1 more Teacher attempt
TURN_HARD_TIMEOUT_S = 30.0     # Wall-clock cap

# M-FB compliance: NO templated tutor-text fallback. When the retry
# chain exhausts (Teacher attempts 1-3 + Dean replan + 1 more all fail
# verifier checks OR produce empty drafts), nodes_v2 detects
# used_safe_generic_probe=True and emits an ErrorCard system message
# instead of fake tutor text. The empty string here is a sentinel —
# never reaches the chat surface.
SAFE_GENERIC_PROBE = ""


@dataclass
class TurnAttempt:
    """One Teacher draft + the 4 Haiku checks that judged it."""
    attempt_num: int                 # 1, 2, 3, 4 (4 = post-replan)
    draft: str
    checks: list[dict] = field(default_factory=list)  # universal-schema check results
    elapsed_ms: int = 0
    teacher_tokens_in: int = 0
    teacher_tokens_out: int = 0

    @property
    def all_passed(self) -> bool:
        return bool(self.checks) and all(c.get("pass", False) for c in self.checks)

    @property
    def leak_passed(self) -> bool:
        for c in self.checks:
            if c.get("_check_name") == "haiku_leak_check":
                return bool(c.get("pass", False))
        return True  # absent = treat as passed (no claim of leak)

    def failed_check_names(self) -> list[str]:
        return [c.get("_check_name", "?") for c in self.checks if not c.get("pass", False)]

    def failure_summary(self) -> dict:
        """Compact dict for retry-feedback prompts. Universal-schema fields."""
        for c in self.checks:
            if not c.get("pass", False):
                return {
                    "_check_name": c.get("_check_name", "?"),
                    "reason": c.get("reason", ""),
                    "evidence": c.get("evidence", ""),
                }
        return {}


@dataclass
class TurnRunResult:
    """Final outcome of run_turn — the message to ship + full audit trail."""
    final_text: str
    final_attempt: int               # 1..4, or 5 for safe-generic-probe fallback
    used_safe_generic_probe: bool
    used_dean_replan: bool
    leak_cap_fallback_fired: bool    # True iff probe fired due to leak after replan
    timed_out: bool
    elapsed_ms: int
    attempts: list[TurnAttempt] = field(default_factory=list)
    final_turn_plan: Optional[TurnPlan] = None  # post-replan plan if used


# ─────────────────────────────────────────────────────────────────────────────
# Parallel Haiku quartet
# ─────────────────────────────────────────────────────────────────────────────


def _run_haiku_quartet(
    draft: str,
    *,
    locked_answer: str,
    locked_subsection: str,
    locked_question: str,
    forbidden_terms_aliases: list[str],
    shape_spec: dict,
    hint_level: int,
    hint_text: str,
    prior_tutor_questions: list[str],
    parallel: bool = True,
) -> list[dict]:
    """Run all 4 self-policing Haiku checks (parallel by default).

    Returns list[dict] of universal-schema results in stable order:
      [leak, sycophancy, shape, pedagogy]

    Per L48 these run concurrently — wall-clock for the quartet is
    ~max(individual call times) ≈ 0.5-1.5s.
    """
    def _leak():
        raw = C.haiku_hint_leak_check(draft, locked_answer, aliases=forbidden_terms_aliases)
        return C.to_universal_check_result(raw, check_name="haiku_leak_check")

    def _sycophancy():
        # Existing sycophancy_check signature: (draft, student_state, reach_fired)
        # Use neutral defaults (Track 4.7 graph wiring will pass real values)
        raw = C.haiku_sycophancy_check(draft, "neutral", False)
        return C.to_universal_check_result(raw, check_name="haiku_sycophancy_check")

    def _shape():
        raw = C.haiku_shape_check(
            draft,
            shape_spec=shape_spec, hint_level=hint_level,
            hint_text=hint_text, prior_tutor_questions=prior_tutor_questions,
        )
        return C.to_universal_check_result(raw, check_name="haiku_shape_check")

    def _pedagogy():
        raw = C.haiku_pedagogy_check(
            draft, locked_subsection=locked_subsection,
            locked_question=locked_question,
        )
        return C.to_universal_check_result(raw, check_name="haiku_pedagogy_check")

    runners = [_leak, _sycophancy, _shape, _pedagogy]
    if parallel:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(r) for r in runners]
            results = [f.result() for f in futures]
        # Re-order to canonical [leak, sycophancy, shape, pedagogy]
        order = ["haiku_leak_check", "haiku_sycophancy_check",
                 "haiku_shape_check", "haiku_pedagogy_check"]
        by_name = {r["_check_name"]: r for r in results}
        return [by_name[name] for name in order]
    else:
        return [r() for r in runners]


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator — run_turn
# ─────────────────────────────────────────────────────────────────────────────


def run_turn(
    *,
    teacher: TeacherV2,
    dean: DeanV2,
    turn_plan: TurnPlan,
    teacher_inputs: TeacherPromptInputs,
    dean_state: dict,
    dean_chunks: list[dict],
    locked_answer: str,
    locked_answer_aliases: Optional[list[str]] = None,
    prior_tutor_questions: Optional[list[str]] = None,
    parallel_quartet: bool = True,
    timeout_s: float = TURN_HARD_TIMEOUT_S,
) -> TurnRunResult:
    """Drive the L50 retry loop end-to-end.

    Args:
      teacher: TeacherV2 instance for draft rendering
      dean: DeanV2 instance for replan call
      turn_plan: the plan from Dean's primary planning call
      teacher_inputs: chunks/history/locked context for Teacher
      dean_state: TutorState dict (for Dean.replan path)
      dean_chunks: chunks for Dean.replan (usually same as teacher_inputs.chunks)
      locked_answer: the canonical locked answer (for leak_check)
      locked_answer_aliases: synonyms (for leak_check)
      prior_tutor_questions: last 1-2 tutor questions (for shape no-repetition)
      parallel_quartet: run the 4 Haiku checks concurrently (default True)
      timeout_s: hard wall-clock cap; on exceed → safe-generic-probe

    Returns TurnRunResult with the final text + full audit trail.
    """
    started = time.time()
    attempts: list[TurnAttempt] = []
    aliases = locked_answer_aliases or []
    prior_qs = prior_tutor_questions or []
    locked_subsection = teacher_inputs.locked_subsection
    locked_question = teacher_inputs.locked_question
    current_plan = turn_plan
    used_replan = False
    final_plan = current_plan

    def _within_timeout() -> bool:
        return (time.time() - started) < timeout_s

    # ── Attempts 1-3 with original turn_plan + retry feedback ──────────
    prior_drafts: list[str] = []
    prior_failures: list[dict] = []

    for n in range(1, MAX_TEACHER_ATTEMPTS + 1):
        if not _within_timeout():
            return _safe_probe_result(
                attempts, started, used_replan=used_replan,
                final_plan=final_plan, timed_out=True,
            )

        draft_result: TeacherDraftResult = teacher.draft(
            current_plan, teacher_inputs,
            prior_attempts=prior_drafts, prior_failures=prior_failures,
        )
        if not draft_result.text:
            # Teacher LLM error → record empty attempt + try again or fail through
            attempts.append(TurnAttempt(
                attempt_num=n, draft="",
                checks=[],
                elapsed_ms=draft_result.elapsed_ms,
                teacher_tokens_in=draft_result.input_tokens,
                teacher_tokens_out=draft_result.output_tokens,
            ))
            prior_drafts.append("")
            prior_failures.append({
                "_check_name": "teacher_draft_error",
                "reason": draft_result.error or "empty_draft",
                "evidence": "",
            })
            continue

        if not _within_timeout():
            return _safe_probe_result(
                attempts, started, used_replan=used_replan,
                final_plan=final_plan, timed_out=True,
            )

        check_results = _run_haiku_quartet(
            draft_result.text,
            locked_answer=locked_answer,
            locked_subsection=locked_subsection,
            locked_question=locked_question,
            forbidden_terms_aliases=aliases + current_plan.forbidden_terms,
            shape_spec=current_plan.shape_spec,
            hint_level=int(dean_state.get("hint_level") or 0),
            hint_text=current_plan.hint_text,
            prior_tutor_questions=prior_qs,
            parallel=parallel_quartet,
        )
        att = TurnAttempt(
            attempt_num=n, draft=draft_result.text, checks=check_results,
            elapsed_ms=draft_result.elapsed_ms,
            teacher_tokens_in=draft_result.input_tokens,
            teacher_tokens_out=draft_result.output_tokens,
        )
        attempts.append(att)

        if att.all_passed:
            return TurnRunResult(
                final_text=draft_result.text,
                final_attempt=n,
                used_safe_generic_probe=False,
                used_dean_replan=False,
                leak_cap_fallback_fired=False,
                timed_out=False,
                elapsed_ms=int((time.time() - started) * 1000),
                attempts=attempts,
                final_turn_plan=current_plan,
            )

        # Failed → feed forward
        prior_drafts.append(draft_result.text)
        failure = att.failure_summary()
        if failure:
            prior_failures.append(failure)

    # ── All 3 attempts failed → Dean re-plans ONCE ─────────────────────
    if not _within_timeout():
        return _safe_probe_result(
            attempts, started, used_replan=False,
            final_plan=final_plan, timed_out=True,
        )

    used_replan = True
    replan_result = dean.replan(
        dean_state, dean_chunks,
        prior_plan=current_plan,
        prior_attempts=prior_drafts,
        prior_failures=prior_failures,
    )
    final_plan = replan_result.turn_plan

    if not _within_timeout():
        return _safe_probe_result(
            attempts, started, used_replan=True,
            final_plan=final_plan, timed_out=True,
        )

    # ── Attempt 4 with the new TurnPlan ────────────────────────────────
    draft4 = teacher.draft(
        final_plan, teacher_inputs,
        prior_attempts=prior_drafts, prior_failures=prior_failures,
    )
    if not draft4.text:
        return _safe_probe_result(
            attempts, started, used_replan=True,
            final_plan=final_plan, timed_out=False,
        )

    check_results4 = _run_haiku_quartet(
        draft4.text,
        locked_answer=locked_answer,
        locked_subsection=locked_subsection,
        locked_question=locked_question,
        forbidden_terms_aliases=aliases + final_plan.forbidden_terms,
        shape_spec=final_plan.shape_spec,
        hint_level=int(dean_state.get("hint_level") or 0),
        hint_text=final_plan.hint_text,
        prior_tutor_questions=prior_qs,
        parallel=parallel_quartet,
    )
    att4 = TurnAttempt(
        attempt_num=4, draft=draft4.text, checks=check_results4,
        elapsed_ms=draft4.elapsed_ms,
        teacher_tokens_in=draft4.input_tokens,
        teacher_tokens_out=draft4.output_tokens,
    )
    attempts.append(att4)

    if att4.all_passed:
        return TurnRunResult(
            final_text=draft4.text,
            final_attempt=4,
            used_safe_generic_probe=False,
            used_dean_replan=True,
            leak_cap_fallback_fired=False,
            timed_out=False,
            elapsed_ms=int((time.time() - started) * 1000),
            attempts=attempts,
            final_turn_plan=final_plan,
        )

    # ── Critical safety rule (Codex round-1 fix #5) ────────────────────
    # If leak_check still fails → DO NOT SHIP draft4. Fall back to
    # safe-generic-probe. If only other checks fail (sycophancy / shape /
    # pedagogy) → ship draft4 anyway (less critical than leak).
    if not att4.leak_passed:
        return _safe_probe_result(
            attempts, started, used_replan=True,
            final_plan=final_plan, timed_out=False,
            leak_cap_fired=True,
        )

    # Non-leak failure on attempt 4 → ship anyway per L50
    return TurnRunResult(
        final_text=draft4.text,
        final_attempt=4,
        used_safe_generic_probe=False,
        used_dean_replan=True,
        leak_cap_fallback_fired=False,
        timed_out=False,
        elapsed_ms=int((time.time() - started) * 1000),
        attempts=attempts,
        final_turn_plan=final_plan,
    )


def _safe_probe_result(
    attempts: list[TurnAttempt],
    started: float,
    *,
    used_replan: bool,
    final_plan: Optional[TurnPlan],
    timed_out: bool,
    leak_cap_fired: bool = False,
) -> TurnRunResult:
    """Build the safe-generic-probe TurnRunResult per L50."""
    return TurnRunResult(
        final_text=SAFE_GENERIC_PROBE,
        final_attempt=5,
        used_safe_generic_probe=True,
        used_dean_replan=used_replan,
        leak_cap_fallback_fired=leak_cap_fired,
        timed_out=timed_out,
        elapsed_ms=int((time.time() - started) * 1000),
        attempts=attempts,
        final_turn_plan=final_plan,
    )
