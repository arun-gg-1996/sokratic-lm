"""
conversation/dean_v2.py
───────────────────────
Dean TurnPlan emitter per L46 + L47 + L51 + L53 (Track 4.5).

Replaces today's 9-method Dean planner stack with a SINGLE Sonnet call
that emits a validated TurnPlan. Today's _setup_call + _quality_check_call
+ _format_dean_critique etc. all collapse into ONE planning step:

  dean = DeanV2(client, model="claude-sonnet-4-6")
  plan = dean.plan(state, retrieved_chunks, mem0_carryover) → TurnPlan

Why one call?
  - Today's Sonnet QC (_quality_check_call ~$0.01-0.02/turn) is REMOVED
    per L51. The 4 Haiku self-policing checks (Track 4.2) replace it.
  - The TurnPlan IS the contract — Dean's job is to plan ONE message
    well, not to plan + critique a draft.
  - Cost saving: ~$0.04 today → ~$0.02/turn (single Sonnet planning call)
    + ~$0.0008 (4 Haiku checks) = ~$0.021/turn for the planner+checker
    layer. ~50% cheaper.

Per L46:
  Output is a TurnPlan with all required fields filled.
  If parse fails → re-prompt with stricter instruction.
  If 2nd parse fails → minimal_fallback().

Per L47:
  Hint suggestions are pre-baked at lock time as state.hint_suggestions
  (~5 angles, $0.005). Dean's per-turn _setup_call decides whether to
  reuse a bank suggestion or write fresh; bank entries are SOFT.
  This module provides plan_hint_bank() for the lock-time call.

Per L51:
  Today's Sonnet quality check is GONE. dean_v2 has no _quality_check
  function. Quality verification is the Track 4.2 quartet of Haiku checks.

Per L53:
  Reach checking lives separately (Step A + Step B per existing
  reached_answer_gate). Dean's TurnPlan.student_reached_answer is
  INFORMATIONAL only — authority is the dedicated reach gate.

Per L9 + Track 2:
  Topic-mapper-LLM is a SEPARATE module (retrieval/topic_mapper_llm.py).
  Dean does NOT do topic mapping; it operates on an already-locked topic.
  The L9 mapper fires once at topic-lock time (session.py / rapport_node).

Architecture
------------
- DeanV2.plan(state, chunks, carryover) → TurnPlan
    Single Sonnet call. Builds a structured prompt with:
      * locked_topic + locked_question + locked_answer + aliases
      * retrieved chunks (anchor + tangent)
      * conversation history (last N turns)
      * previous turn's failures (if any, for re-plan path per L50)
      * carryover_notes from mem0
      * shape_spec target
    Asks for strict JSON matching the TurnPlan schema.
    Parses via TurnPlan.from_llm_json — re-prompts once on parse fail
    per L46, then minimal_fallback if still failing.

- DeanV2.plan_hint_bank(state, chunks) → list[str]
    Lock-time call. Generates ~5 hint angles for the locked subsection
    as a SOFT bank Dean can reuse per-turn or ignore. Stored as
    state.hint_suggestions.

- DeanV2.replan(state, chunks, prior_plan, prior_attempts, prior_failures)
    Per L50: after 3 Teacher attempts fail Haiku checks, Dean re-plans
    ONCE with the failure detail before falling back to safe-generic-probe.
    Same Sonnet call as plan(), with the failure feedback prepended.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from conversation.turn_plan import TurnPlan, MODES, TONES


# ─────────────────────────────────────────────────────────────────────────────
# Single planning prompt — Dean's only job is producing a TurnPlan
# ─────────────────────────────────────────────────────────────────────────────

_DEAN_SYSTEM = """\
You are the Dean of a Socratic {domain_name} tutor. Your job is to plan
ONE turn of teaching by emitting a TurnPlan JSON object that the
Teacher will render into a message.

Decision flow:
  1. Read the conversation context, locked topic, retrieved chunks, and
     any prior-session carryover notes.
  2. Decide what KIND of message Teacher should send (mode):
       socratic      — standard tutoring scaffold
       clinical      — clinical-application turn
       opt_in        — student reached answer; offer clinical bonus
       confirm_end   — student signaled session-end (rare; pre-flight handles
                       most of these — only emit if pre-flight skipped)
       honest_close  — graceful end (pre-lock cap, turn cap, etc.)
  3. Decide the TONE:
       encouraging   — supportive, validating effort
       firm          — direct, refocusing
       neutral       — plain
       honest        — candid (used for honest_close)
  4. Write the HINT TEXT — the actual hint Teacher should scaffold around.
     For socratic mode this is the conceptual angle for THIS turn.
     For clinical mode this can be empty (clinical_scenario does the work).
  5. List FORBIDDEN TERMS — the locked answer + its aliases + any close
     synonyms that would constitute a leak. Teacher reads chunks but
     must not use these terms.
  6. List PERMITTED TERMS — vocab anchors Teacher can lean on (e.g.
     "pacemaker", "atrium" while protecting "SA node").
  7. Set carryover_notes from the provided mem0 context if it's relevant
     to this turn; empty string otherwise.

Output STRICT JSON only — no markdown fences, no preamble:

{{
  "scenario": "<1-line summary of student state>",
  "hint_text": "<conceptual scaffold for this turn>",
  "mode": "socratic" | "clinical" | "rapport" | "opt_in" |
          "redirect" | "nudge" | "confirm_end" | "honest_close",
  "tone": "encouraging" | "firm" | "neutral" | "honest",
  "permitted_terms": ["..."],
  "forbidden_terms": ["..."],
  "shape_spec": {{"max_sentences": 4, "exactly_one_question": true}},
  "carryover_notes": "<from mem0 if relevant; empty otherwise>",
  "hint_suggestions": [],
  "apply_redaction": false,
  "student_reached_answer": false,
  "image_context": null,
  "clinical_scenario": null,
  "clinical_target": null
}}

apply_redaction is ALWAYS false. clinical_scenario / clinical_target are
null UNLESS mode="clinical".

EXPLORATION RETRIEVAL (M6):
Set needs_exploration=true ONLY when the student's question is tangential
to the locked subsection AND the answer requires content not present in the
chunks shown above. When true, also set exploration_query to a short
focused search string (3-8 words) for the tangential concept.

Default: needs_exploration=false, exploration_query="" — reuse the
chunks already provided (they cover the locked subsection).

You will see exploration_count and turns_remaining in the user prompt.
If needs_exploration=true:
  - Always answer helpfully with the chunks (existing + exploration).
  - If turns_remaining < 4 OR exploration_count >= 2, briefly remind the
    student we have N turns left for the original question.
  - Never refuse exploration. Genuine curiosity is welcome.
"""


_DEAN_USER_TEMPLATE = """\
LOCKED TOPIC
  Subsection: {locked_subsection}
  Question:   {locked_question}
  Answer:     {locked_answer}
  Aliases:    {aliases}

CURRENT TURN CONTEXT
  Hint level: {hint_level}
  Turn number: {turn_count}
  Turns remaining: {turns_remaining}
  Phase: {phase}
  Exploration count: {exploration_count}
{clinical_style_block}
CARRYOVER NOTES (mem0 — empty if cold-start):
{carryover_notes}

RETRIEVED CHUNKS:
{chunks}

CONVERSATION HISTORY (most recent last):
{history}
{prior_failures_block}
Output the TurnPlan JSON object only.
"""


_CLINICAL_STYLE_BLOCK = """\

CLINICAL SCENARIO STYLE (when emitting mode="clinical"):
  {clinical_scenario_style}
"""


_PRIOR_FAILURES_TEMPLATE = """\

PRIOR ATTEMPTS THIS TURN (Teacher drafts that failed Haiku checks):
{attempts}

You are now RE-PLANNING. Adjust hint_text or forbidden_terms to avoid
the same failure mode. The 4 self-policing Haiku checks (leak,
sycophancy, shape, pedagogy) will run again on Teacher's next draft.
"""


def _format_chunks(chunks: list[dict], max_chunks: int = 7) -> str:
    out = []
    for i, c in enumerate(chunks[:max_chunks], start=1):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        sub = c.get("subsection_title") or c.get("subsection") or ""
        prefix = f"[{i}] ({sub}) " if sub else f"[{i}] "
        out.append(prefix + text[:1200])
    return "\n\n".join(out) or "(no chunks)"


def _format_history(history: list[dict], max_turns: int = 6) -> str:
    tail = history[-(max_turns * 2):]
    out = []
    for m in tail:
        role = m.get("role") or "?"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        prefix = {"student": "STUDENT", "tutor": "TUTOR"}.get(role, role.upper())
        out.append(f"{prefix}: {content}")
    return "\n\n".join(out) or "(no history)"


def _format_prior_failures(attempts: list[str], failures: list[dict]) -> str:
    if not attempts:
        return ""
    lines = []
    for i, attempt in enumerate(attempts, start=1):
        lines.append(f"\nAttempt {i}: {attempt}")
        if failures and len(failures) >= i:
            f = failures[i - 1]
            lines.append(
                f"  → failed {f.get('_check_name', '?')}: {f.get('reason', '?')}"
            )
    return _PRIOR_FAILURES_TEMPLATE.format(attempts="\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Hint-bank prompt — fires once per session at lock time per L47
# ─────────────────────────────────────────────────────────────────────────────


_HINT_BANK_PROMPT = """\
You are pre-generating a SOFT bank of hint angles for a Socratic tutor
to draw from. The student has just locked onto this topic; you are
producing 5 hint angles that scaffold reasoning toward the answer
WITHOUT revealing it.

LOCKED SUBSECTION: {locked_subsection}
LOCKED QUESTION: {locked_question}
LOCKED ANSWER: {locked_answer}

Each angle should:
  - Be 1-2 sentences max
  - Reference the locked subsection's content (use the chunks below)
  - Vary in directness (some oblique, some closer to the answer)
  - NOT contain the locked answer or close synonyms

CHUNKS:
{chunks}

Output STRICT JSON:
{{"hint_angles": ["...", "...", "...", "...", "..."]}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# DeanV2 — single-entry planner
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DeanPlanResult:
    """Wrap the planning call's TurnPlan + diagnostics."""
    turn_plan: TurnPlan
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    raw_response: str = ""
    parse_attempts: int = 1   # 1 = parsed first try, 2 = needed re-prompt, 3 = used minimal_fallback
    used_fallback: bool = False
    error: Optional[str] = None


class DeanV2:
    """Single-entry Dean planner per L46.

    Construction:
      dean = DeanV2(client, model="claude-sonnet-4-6")
      plan_result = dean.plan(state, chunks, carryover_notes)
      print(plan_result.turn_plan.mode)
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def plan(
        self,
        state: dict,
        chunks: list[dict],
        *,
        carryover_notes: str = "",
        domain_name: str = "this subject",
        domain_short: str = "subject",
        clinical_scenario_style: str = "",
        prior_attempts: Optional[list[str]] = None,
        prior_failures: Optional[list[dict]] = None,
    ) -> DeanPlanResult:
        """Single Sonnet call → DeanPlanResult containing a validated TurnPlan.

        On parse failure: re-prompt once with a stricter instruction.
        On second parse failure: emit TurnPlan.minimal_fallback() per L46.

        Per L78 the `domain_name` / `domain_short` defaults are
        intentionally generic ("this subject" / "subject") so a missing
        cfg.domain.* slot still yields a parseable prompt — but a real
        production caller passes from cfg. `clinical_scenario_style`
        comes from cfg.domain.clinical_scenario_style and is injected
        into the user prompt only when non-empty so Dean's clinical-mode
        scenarios stay domain-appropriate (patient case for medical /
        anatomy, engineering problem for physics, etc.) per L74.
        """
        system_prompt = _DEAN_SYSTEM.format(domain_name=domain_name)
        user_prompt = self._build_user_prompt(
            state, chunks, carryover_notes,
            clinical_scenario_style=clinical_scenario_style,
            prior_attempts=prior_attempts, prior_failures=prior_failures,
        )

        # Attempt 1 — primary planning call
        result = self._call_and_parse(
            system_prompt, user_prompt, attempt=1,
        )
        if result.turn_plan is not None and not result.used_fallback:
            return result

        # Attempt 2 — re-prompt with stricter instruction per L46
        stricter_user = (
            user_prompt
            + "\n\n--- RE-PROMPT ---\n"
            "Your previous response could not be parsed as valid JSON. Try again.\n"
            "Output STRICT JSON ONLY — start with { end with } no other text.\n"
        )
        result2 = self._call_and_parse(
            system_prompt, stricter_user, attempt=2,
        )
        if result2.turn_plan is not None and not result2.used_fallback:
            return result2

        # Both attempts failed — emit minimal fallback per L46
        scenario = "dean_parse_failed_twice"
        if state.get("hint_level"):
            scenario += f"_hint_level_{state['hint_level']}"
        fallback_plan = TurnPlan.minimal_fallback(
            scenario=scenario,
            hint_text="",
            tone="neutral",
        )
        return DeanPlanResult(
            turn_plan=fallback_plan,
            elapsed_ms=result.elapsed_ms + result2.elapsed_ms,
            input_tokens=result.input_tokens + result2.input_tokens,
            output_tokens=result.output_tokens + result2.output_tokens,
            cache_read_tokens=result.cache_read_tokens + result2.cache_read_tokens,
            raw_response=result2.raw_response,
            parse_attempts=3,
            used_fallback=True,
            error=result2.error or result.error or "parse_failed_twice",
        )

    def replan(
        self,
        state: dict,
        chunks: list[dict],
        *,
        prior_plan: TurnPlan,
        prior_attempts: list[str],
        prior_failures: list[dict],
        carryover_notes: str = "",
    ) -> DeanPlanResult:
        """Per L50: after 3 Teacher attempts fail Haiku checks, re-plan
        ONCE with the failure detail. Just calls plan() with the failure
        history prepended — same prompt path, different inputs."""
        return self.plan(
            state, chunks,
            carryover_notes=carryover_notes,
            prior_attempts=prior_attempts,
            prior_failures=prior_failures,
        )

    def plan_hint_bank(
        self,
        state: dict,
        chunks: list[dict],
        *,
        n_angles: int = 5,
    ) -> list[str]:
        """Per L47 — pre-generate ~5 hint angles at lock time. Returns
        list[str] (caller stores in state.hint_suggestions). Bank entries
        are SOFT — Dean's per-turn plan() may reuse or ignore them.

        Returns [] on any LLM failure; caller falls back to fully
        per-turn hint generation in plan().
        """
        locked = state.get("locked_topic") or {}
        prompt = _HINT_BANK_PROMPT.format(
            locked_subsection=locked.get("subsection") or "(unspecified)",
            locked_question=state.get("locked_question") or "(unspecified)",
            locked_answer=state.get("locked_answer") or "(unspecified)",
            chunks=_format_chunks(chunks),
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=600,
                temperature=0.4,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return []
        raw = resp.content[0].text.strip() if resp.content else ""
        try:
            parsed = self._parse_json(raw)
        except (ValueError, json.JSONDecodeError):
            return []
        angles = parsed.get("hint_angles") or []
        if not isinstance(angles, list):
            return []
        return [str(a).strip() for a in angles if str(a).strip()][:n_angles]

    # ── Internals ────────────────────────────────────────────────────────

    def _build_user_prompt(
        self,
        state: dict,
        chunks: list[dict],
        carryover_notes: str,
        *,
        clinical_scenario_style: str = "",
        prior_attempts: Optional[list[str]] = None,
        prior_failures: Optional[list[dict]] = None,
    ) -> str:
        locked = state.get("locked_topic") or {}
        if not locked:
            locked = (state.get("debug") or {}).get("locked_topic_snapshot") or {}
        aliases = state.get("locked_answer_aliases") or []
        # L78 — surface the per-domain clinical scenario style only when
        # the cfg slot is populated. Empty → block is empty (no wasted
        # prompt tokens for domains where clinical isn't applicable).
        clinical_block = ""
        if (clinical_scenario_style or "").strip():
            clinical_block = _CLINICAL_STYLE_BLOCK.format(
                clinical_scenario_style=clinical_scenario_style.strip(),
            )
        turn_count_val = sum(
            1 for m in (state.get("messages") or []) if m.get("role") == "student"
        )
        max_turns_val = int(state.get("max_turns", 0) or 0)
        turns_remaining = max(0, max_turns_val - turn_count_val) if max_turns_val else "n/a"
        return _DEAN_USER_TEMPLATE.format(
            locked_subsection=locked.get("subsection") or "(unspecified)",
            locked_question=state.get("locked_question") or "(unspecified)",
            locked_answer=state.get("locked_answer") or "(unspecified)",
            aliases=", ".join(aliases) if aliases else "(none)",
            hint_level=state.get("hint_level") or 0,
            turn_count=turn_count_val,
            turns_remaining=turns_remaining,
            exploration_count=int(state.get("exploration_count", 0) or 0),
            phase=state.get("phase") or "tutoring",
            clinical_style_block=clinical_block,
            carryover_notes=carryover_notes or "(none)",
            chunks=_format_chunks(chunks),
            history=_format_history(state.get("messages") or []),
            prior_failures_block=_format_prior_failures(
                prior_attempts or [], prior_failures or []
            ),
        )

    def _call_and_parse(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        attempt: int,
    ) -> DeanPlanResult:
        t0 = time.time()
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e:
            return DeanPlanResult(
                turn_plan=None,  # type: ignore[arg-type]
                elapsed_ms=int((time.time() - t0) * 1000),
                parse_attempts=attempt,
                used_fallback=True,
                error=f"{type(e).__name__}: {str(e)[:160]}",
            )

        elapsed_ms = int((time.time() - t0) * 1000)
        raw = resp.content[0].text if resp.content else ""
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        try:
            plan = TurnPlan.from_llm_json(raw)
        except (ValueError, json.JSONDecodeError) as e:
            return DeanPlanResult(
                turn_plan=None,  # type: ignore[arg-type]
                elapsed_ms=elapsed_ms,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cache_read_tokens=cache_read,
                raw_response=raw,
                parse_attempts=attempt,
                used_fallback=True,
                error=f"parse_fail: {e}",
            )

        return DeanPlanResult(
            turn_plan=plan,
            elapsed_ms=elapsed_ms,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_tokens=cache_read,
            raw_response=raw,
            parse_attempts=attempt,
            used_fallback=False,
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Parse JSON tolerantly (markdown fences, preamble noise)."""
        s = (text or "").strip()
        if s.startswith("```"):
            lines = s.split("\n")
            s = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            import re as _re
            m = _re.search(r"\{.*\}", s, _re.DOTALL)
            if not m:
                raise
            return json.loads(m.group(0))
