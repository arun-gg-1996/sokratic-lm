"""
conversation/teacher_v2.py
──────────────────────────
Single-entry-point Teacher per L49 (Track 4.4). Replaces today's 4
methods (draft_rapport / draft_socratic / draft_clinical /
draft_clinical_opt_in) with ONE function:

  teacher.draft(turn_plan, chunks, history) -> str

The TurnPlan's `mode` field selects which prompt path is used. The
TurnPlan's `tone` field is orthogonal — it shapes phrasing within the
chosen mode (encouraging / firm / neutral / honest).

Per L49 + L52 + L54:
  - One prompt template per mode, modulated by tone variable
  - Consistent voice across phases — reduces drift
  - Forbidden_terms (Option C) baked into every mode that operates on
    chunks (socratic, clinical) — Teacher is instructed not to use
    them; haiku_leak_check verifies post-draft (Track 4.6)
  - Carryover_notes from mem0 injected for socratic/clinical modes

Why a v2 module instead of editing teacher.py?
  Additive-rebuild pattern: teacher.py keeps working with today's flow;
  teacher_v2.py is consumed by the new graph (Track 4.7) behind a
  feature flag. Once the new graph is verified end-to-end, teacher.py
  can be deleted (Track 4.8). Mirrors what we did with mastery API v2,
  topic_mapper_llm, etc.

Test approach: 100% mocked Sonnet client. The prompt builders are
pure-Python — easy to unit-test by inspecting the rendered prompt.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from conversation.turn_plan import TurnPlan


# ─────────────────────────────────────────────────────────────────────────────
# Mode-specific instruction templates
# ─────────────────────────────────────────────────────────────────────────────

# Each mode has a single instruction block telling Teacher what to write.
# Tone is a separate string injected into a final "TONE:" line that all
# modes share, so phrasing variation is controlled by ONE variable.

_MODE_INSTRUCTIONS: dict[str, str] = {

    "socratic": """\
You are a Socratic {domain_short} tutor. The student has locked onto a
topic and you are mid-tutoring. Your goal: ask ONE focused question that
moves the student a small step closer to the locked answer, using the
hint Dean has provided as your scaffolding.

Strict rules:
- Do NOT reveal the locked answer or any of the FORBIDDEN TERMS.
- Use the HINT TEXT as the conceptual scaffold for your question.
- Ground every claim in the RETRIEVED CHUNKS — never invent details.
- Maximum {max_sentences} sentences. Exactly one question.
- Do NOT start with empty praise ("Great!", "Excellent!", "Perfect!", etc.).
""",

    "clinical": """\
You are a Socratic {domain_short} tutor in the clinical-application
phase. The student has demonstrated the core concept; now you are
exploring real-world reasoning via a clinical scenario.

Strict rules:
- Frame the question around the CLINICAL SCENARIO if provided.
- Connect the scenario back to the core concept the student already
  demonstrated.
- Do NOT reveal the CLINICAL TARGET or any FORBIDDEN TERMS.
- Maximum {max_sentences} sentences. Exactly one question.
- Avoid empty praise.
""",

    "rapport": """\
You are a Socratic {domain_name} tutor opening a new session with a
{student_descriptor}. Write a brief, natural opener (2-3 sentences max).

Strict rules:
- First words must be exactly: "Good {time_of_day}".
- Do NOT use generic clichés ("Hello", "Welcome", "I'm here to help").
- If LOCKED SUBSECTION is set (the student arrived with a prelocked
  topic from My Mastery), do NOT ask them to pick a topic. Instead,
  acknowledge by NAME — e.g. "Picking up on <subsection> today" — and
  end with a brief invitation to dive in (no question; cards follow).
- If LOCKED SUBSECTION is "(unspecified)" or empty, end with exactly
  one question asking what {domain_name} topic the student wants to
  tackle today.
- If CARRYOVER NOTES reference a prior topic, mention it briefly as a
  resume option — but do NOT lecture.
""",

    "opt_in": """\
You are a Socratic {domain_short} tutor. The student has just reached
the core answer for the locked topic. Offer a clinical-application
question as a *bonus*, asking ONLY whether they'd like to try it.

Strict rules:
- 1-2 sentences total. One question.
- Make clear it's optional — student can decline and end the session.
- Do NOT start the clinical content yet — just the opt-in.
""",

    "redirect": """\
You are a Socratic {domain_short} tutor. The student just signaled
HELP-ABUSE (asked for the answer directly, said "idk" with no
reasoning attempt, or demanded simplification).

Strict rules:
- Acknowledge their state briefly without judgment.
- Reframe: "let's think together" — invite even a partial guess.
- Do NOT advance the hint level.
- Do NOT reveal the locked answer or FORBIDDEN TERMS.
- Maximum 3 sentences. End with one question that invites engagement.
""",

    "nudge": """\
You are a Socratic {domain_short} tutor. The student just went
OFF-DOMAIN (asked something unrelated to {domain_name}).

Strict rules:
- Briefly redirect them back to the locked topic.
- Match the TONE precisely — gentle nudges feel different from firm
  ones; do not over-soften when tone is "firm".
- Do NOT engage with the off-topic content.
- Maximum 2 sentences. End with one focused question on the locked
  topic.
""",

    "confirm_end": """\
You are a Socratic {domain_short} tutor. The student just signaled they
want to end the session.

Strict rules:
- Confirm their intent in 1 sentence: "It sounds like you'd like to
  wrap up — is that right?"
- Mention briefly what they accomplished (locked subsection, did/didn't
  reach the answer) so the choice feels informed.
- Maximum 2 sentences. End with one yes/no question.
- The frontend renders YES/NO buttons after this message.
""",

    "honest_close": """\
You are a Socratic {domain_short} tutor closing the session honestly.
The student did NOT reach the locked answer for the subsection
({locked_subsection}) — they either gave up, ran out of turns, or
disengaged across multiple strikes.

Strict rules:
- Be candid that the locked question wasn't fully resolved today.
- Reference the chapter > section > subsection so the student knows
  what to revisit.
- Suggest they start fresh from My Mastery when ready.
- Maximum 3 sentences. No closing question.
- Do NOT congratulate them on reaching anything they didn't reach.
""",

    "reach_close": """\
You are a Socratic {domain_short} tutor closing the session warmly.
The student SUCCESSFULLY reached the locked answer for the subsection
({locked_subsection}) and has chosen not to continue with the optional
clinical bonus.

Strict rules:
- Acknowledge what they got right — name the subsection and the core
  concept they demonstrated.
- The textbook answer they reached is provided in HINT TEXT — confirm
  it briefly so they leave with closure.
- Warm and brief. No fake hype, no unnecessary praise spirals.
- Maximum 3 sentences. No closing question.
- Do NOT say "we didn't get to" or "you didn't cover" — they reached it.
""",

    "clinical_natural_close": """\
You are a Socratic {domain_short} tutor closing the clinical phase. The
student already reached the core answer for ({locked_subsection}) and
engaged with the clinical-application question, but the clinical phase
hit its natural turn limit before fully resolving the clinical target.

Strict rules:
- Acknowledge the clinical reasoning work they did — without revealing
  the clinical target answer.
- Note that mastery + any open threads will appear in My Mastery.
- Warm and brief. No fake praise.
- Maximum 2 sentences. No closing question.
- Do NOT say they "didn't engage" — they did engage; they just ran out
  of turns on the bonus.
""",

    # M1 — unified close mode. ONE Sonnet call replaces 3 legacy close
    # prompts. The CLOSE_REASON in the user prompt picks the framing.
    # Output is STRICT JSON {message, demonstrated, needs_work} so:
    #   message → tutor chat bubble (streamed)
    #   demonstrated + needs_work → sessions.key_takeaways (M5 reads this)
    "close": """\
You are a Socratic {domain_short} tutor closing this session. Produce
a thoughtful, history-aware goodbye message that ALSO emits the
post-session takeaways used by the My Mastery analysis view.

The CLOSE_REASON in the user prompt tells you WHY the session is
ending. Tailor tone and content accordingly:

  reach_full          — student got the clinical answer correctly. Warm
                        celebratory close. Confirm they nailed it.
  reach_skipped       — student reached the core answer but declined the
                        clinical bonus. Warm. No reproach for skipping.
  clinical_cap        — clinical phase hit turn cap. Acknowledge the
                        reasoning work. No congrats they didn't earn.
  hints_exhausted     — student didn't reach the answer; hints used up.
                        Honest, encouraging. Name the gap explicitly.
                        Suggest a fresh start from My Mastery.
  tutoring_cap        — turn budget hit, no reach. Same as above.
  off_domain_strike   — student kept going off-domain. Firm but kind.
                        Suggest the right time to come back.
  exit_intent         — student clicked End session. Brief, neutral.
                        No save-or-don't-save framing — frontend banner
                        handles that.

Universal rules:
- Read the CONVERSATION HISTORY. Name something SPECIFIC the student
  did or said — never generic "great work today".
- Write 2-4 sentences total. No closing question.
- Do NOT congratulate them on reaching anything they didn't reach.
- Do NOT say "we didn't get to..." if reason indicates they did reach.

Output STRICT JSON only — no markdown, no preamble:
{{
  "message":      "<tutor goodbye message — 2-4 sentences>",
  "demonstrated": "<one short line: what the student showed they understood>",
  "needs_work":   "<one short line: what they should revisit; empty if none>"
}}
""",
}


# Universal preamble + footer attached to EVERY mode prompt. Carries the
# tone, shape_spec, forbidden_terms, and carryover_notes (per L52 + L54).
_PROMPT_PREAMBLE = """\
{instructions}
TONE: {tone}
SHAPE: max {max_sentences} sentences, exactly one question = {exactly_one_question}.
"""

_PROMPT_FORBIDDEN_BLOCK = """\

FORBIDDEN TERMS (you must NOT use these or any close variant):
{forbidden_terms}
"""

_PROMPT_PERMITTED_BLOCK = """\

PERMITTED TERMS (you may lean on these vocabulary anchors):
{permitted_terms}
"""

_PROMPT_CARRYOVER_BLOCK = """\

CARRYOVER NOTES (relevant prior-session context — use sparingly):
{carryover_notes}
"""

_PROMPT_HINT_BLOCK = """\

HINT TEXT (Dean's intended scaffolding for this turn):
{hint_text}
"""

_PROMPT_CLINICAL_BLOCK = """\

CLINICAL SCENARIO:
{clinical_scenario}

CLINICAL TARGET (do not reveal — this is what student must reach):
{clinical_target}
"""

_PROMPT_LOCKED_BLOCK = """\

LOCKED SUBSECTION: {locked_subsection}
LOCKED QUESTION: {locked_question}
"""

_PROMPT_CHUNKS_BLOCK = """\

RETRIEVED CHUNKS (ground every claim in these — never invent):
{chunks}
"""

_PROMPT_HISTORY_BLOCK = """\

CONVERSATION HISTORY (most recent last):
{history}
"""

# L77 — image-driven session. Surfaced into socratic/clinical mode
# prompts when TurnPlan.image_context is populated. Lets Teacher
# scaffold around what's actually visible in the student's image
# instead of generic textbook-recall questions.
_PROMPT_IMAGE_BLOCK = """\

IMAGE CONTEXT (student uploaded an image — ground identification questions in what's visible):
  Description: {description}
  Identified structures: {structures}
  Image type: {image_type}
"""

_PROMPT_FOOTER = """\

Output ONLY the message you want to send to the student. No preamble,
no markdown, no JSON, no explanations.
"""


# Modes that use chunks (other modes don't need them — saves tokens).
_MODES_USING_CHUNKS = {"socratic", "clinical"}

# Modes that use history. M1: close modes added so the LLM goodbye sees
# what actually happened (was generic before — produced near-identical
# closes for very different conversations).
_MODES_USING_HISTORY = {
    "socratic", "clinical", "redirect", "nudge", "confirm_end",
    "honest_close", "reach_close", "clinical_natural_close", "close",
}

# Modes that need locked-topic context fields. M4: rapport added so
# prelock from My Mastery → Start surfaces the subsection name in the
# greeting instead of asking "what topic do you want to study?"
_MODES_USING_LOCKED = {"socratic", "clinical", "redirect", "opt_in",
                       "confirm_end", "honest_close", "reach_close",
                       "clinical_natural_close", "close", "rapport"}


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TeacherPromptInputs:
    """Everything the prompt needs that isn't on the TurnPlan itself.

    Kept as a dataclass (not buried as kwargs) so callers + tests can
    construct it explicitly — and so missing fields surface at boundary
    rather than mid-prompt.
    """
    chunks: list[dict]                # retrieved chunks (subsection-anchor)
    history: list[dict]               # conversation messages (newest last)
    locked_subsection: str = ""
    locked_question: str = ""
    domain_name: str = "this subject"
    domain_short: str = "subject"
    student_descriptor: str = "student"
    time_of_day: str = "afternoon"    # for rapport mode greeting


def build_teacher_prompt(turn_plan: TurnPlan, inputs: TeacherPromptInputs) -> str:
    """Assemble the full Teacher prompt from TurnPlan + inputs.

    Pure function — no LLM call. Easy to unit-test by inspecting the
    returned string.
    """
    if turn_plan.mode not in _MODE_INSTRUCTIONS:
        raise ValueError(f"Unknown TurnPlan mode: {turn_plan.mode!r}")

    instructions = _MODE_INSTRUCTIONS[turn_plan.mode].format(
        domain_name=inputs.domain_name,
        domain_short=inputs.domain_short,
        student_descriptor=inputs.student_descriptor,
        time_of_day=inputs.time_of_day,
        max_sentences=turn_plan.shape_spec.get("max_sentences", 4),
        locked_subsection=inputs.locked_subsection or "(unspecified)",
    )

    parts = [_PROMPT_PREAMBLE.format(
        instructions=instructions,
        tone=turn_plan.tone,
        max_sentences=turn_plan.shape_spec.get("max_sentences", 4),
        exactly_one_question=str(
            turn_plan.shape_spec.get("exactly_one_question", True)
        ).lower(),
    )]

    if turn_plan.permitted_terms:
        parts.append(_PROMPT_PERMITTED_BLOCK.format(
            permitted_terms=", ".join(turn_plan.permitted_terms),
        ))
    if turn_plan.forbidden_terms:
        parts.append(_PROMPT_FORBIDDEN_BLOCK.format(
            forbidden_terms=", ".join(turn_plan.forbidden_terms),
        ))
    if turn_plan.carryover_notes:
        parts.append(_PROMPT_CARRYOVER_BLOCK.format(
            carryover_notes=turn_plan.carryover_notes,
        ))
    if turn_plan.mode in _MODES_USING_LOCKED:
        parts.append(_PROMPT_LOCKED_BLOCK.format(
            locked_subsection=inputs.locked_subsection or "(unspecified)",
            locked_question=inputs.locked_question or "(unspecified)",
        ))
    if turn_plan.hint_text and turn_plan.mode in {"socratic", "redirect"}:
        parts.append(_PROMPT_HINT_BLOCK.format(hint_text=turn_plan.hint_text))
    if turn_plan.mode == "clinical" and turn_plan.clinical_scenario:
        parts.append(_PROMPT_CLINICAL_BLOCK.format(
            clinical_scenario=turn_plan.clinical_scenario,
            clinical_target=turn_plan.clinical_target or "(unspecified)",
        ))
    if turn_plan.mode in _MODES_USING_CHUNKS and inputs.chunks:
        parts.append(_PROMPT_CHUNKS_BLOCK.format(
            chunks=_format_chunks(inputs.chunks),
        ))
    if turn_plan.mode in _MODES_USING_HISTORY and inputs.history:
        parts.append(_PROMPT_HISTORY_BLOCK.format(
            history=_format_history(inputs.history),
        ))
    # L77 — surface image context when present and the mode is one that
    # benefits from grounding in visual structures (socratic + clinical).
    if (turn_plan.image_context
            and turn_plan.mode in {"socratic", "clinical"}):
        ic = turn_plan.image_context
        structs = ic.get("identified_structures") or []
        struct_names = ", ".join(
            str((s or {}).get("name", "")) for s in structs[:8]
            if isinstance(s, dict) and s.get("name")
        ) or "(none identified)"
        parts.append(_PROMPT_IMAGE_BLOCK.format(
            description=str(ic.get("description") or "(no description)"),
            structures=struct_names,
            image_type=str(ic.get("image_type") or "other"),
        ))

    parts.append(_PROMPT_FOOTER)
    return "".join(parts)


def _format_chunks(chunks: list[dict], max_chunks: int = 7) -> str:
    """Render up to N retrieved chunks as a numbered list."""
    out = []
    for i, c in enumerate(chunks[:max_chunks], start=1):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        # Tag with subsection so Teacher can self-attribute
        sub = c.get("subsection_title") or c.get("subsection") or ""
        prefix = f"[{i}] ({sub}) " if sub else f"[{i}] "
        out.append(prefix + text[:1200])
    return "\n\n".join(out) or "(no chunks)"


def _format_history(history: list[dict], max_turns: int = 8) -> str:
    """Render the last N student/tutor exchanges as plain text."""
    tail = history[-(max_turns * 2):]  # max_turns × 2 (student + tutor)
    out = []
    for m in tail:
        role = m.get("role") or "?"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        prefix = {"student": "STUDENT", "tutor": "TUTOR"}.get(role, role.upper())
        out.append(f"{prefix}: {content}")
    return "\n\n".join(out) or "(no history)"


# ─────────────────────────────────────────────────────────────────────────────
# Single entry point
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TeacherDraftResult:
    """Result of one teacher.draft() call.

    Carries the rendered text plus diagnostics for trace.
    """
    text: str
    mode: str
    tone: str
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    error: Optional[str] = None


class TeacherV2:
    """Single-entry-point Teacher per L49.

    Construction:
      teacher = TeacherV2(client, model="claude-sonnet-4-6")
      result = teacher.draft(turn_plan, inputs)
      print(result.text)
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 800,
        temperature: float = 0.4,
    ):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def draft(
        self,
        turn_plan: TurnPlan,
        inputs: TeacherPromptInputs,
        *,
        prior_attempts: Optional[list[str]] = None,
        prior_failures: Optional[list[dict]] = None,
    ) -> TeacherDraftResult:
        """Render Teacher's message via mode-dispatched prompt.

        `prior_attempts` and `prior_failures` are used by the L62 retry
        feedback loop (Track 4.6) — empty on first attempt; populated
        when a check failed and we're retrying with the failure detail.
        """
        # PERF — split the prompt into (stable, variable) so Anthropic's
        # prompt cache can hit on the heavy chunks/history/locked block
        # across retries within the same turn AND across turns within
        # the 5-min TTL. Stable part = the rendered Teacher prompt (chunks,
        # locked context, history). Variable part = retry-feedback addendum
        # which changes per attempt. ~10× cheaper input + faster TTFT.
        stable_prompt = build_teacher_prompt(turn_plan, inputs)

        variable_tail = ""
        if prior_attempts:
            variable_tail = "\n\n--- PRIOR ATTEMPTS (do NOT repeat the same mistakes) ---\n"
            for i, attempt in enumerate(prior_attempts, start=1):
                variable_tail += f"\nAttempt {i}: {attempt}\n"
                if prior_failures and len(prior_failures) >= i:
                    f = prior_failures[i - 1]
                    variable_tail += (
                        f"  → failed {f.get('_check_name', '?')}: "
                        f"{f.get('reason', '?')}\n"
                    )

        content_blocks: list[dict] = [
            {
                "type": "text",
                "text": stable_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        if variable_tail:
            content_blocks.append({"type": "text", "text": variable_tail})

        t0 = time.time()
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[{"role": "user", "content": content_blocks}],
            )
        except Exception as e:
            return TeacherDraftResult(
                text="",
                mode=turn_plan.mode,
                tone=turn_plan.tone,
                elapsed_ms=int((time.time() - t0) * 1000),
                error=f"{type(e).__name__}: {str(e)[:160]}",
            )

        elapsed_ms = int((time.time() - t0) * 1000)
        text = resp.content[0].text.strip() if resp.content else ""
        usage = getattr(resp, "usage", None)
        return TeacherDraftResult(
            text=text,
            mode=turn_plan.mode,
            tone=turn_plan.tone,
            elapsed_ms=elapsed_ms,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
