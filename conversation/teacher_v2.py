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
- End with exactly one question asking what {domain_name} topic the
  student wants to tackle today.
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
The student didn't fully engage with the locked topic ({locked_subsection}),
or hit a session boundary.

Strict rules:
- Be candid about what was/wasn't covered. No fake praise.
- Reference the chapter > section > subsection so the student knows
  what to revisit.
- Suggest they start fresh from My Mastery when ready.
- Maximum 3 sentences. No closing question.
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

_PROMPT_FOOTER = """\

Output ONLY the message you want to send to the student. No preamble,
no markdown, no JSON, no explanations.
"""


# Modes that use chunks (other modes don't need them — saves tokens).
_MODES_USING_CHUNKS = {"socratic", "clinical"}

# Modes that use history (rapport/opt_in/honest_close are short bursts
# that don't need prior turns).
_MODES_USING_HISTORY = {"socratic", "clinical", "redirect", "nudge", "confirm_end"}

# Modes that need locked-topic context fields
_MODES_USING_LOCKED = {"socratic", "clinical", "redirect", "opt_in",
                       "confirm_end", "honest_close"}


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
    domain_name: str = "human anatomy"
    domain_short: str = "anatomy"
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
        prompt = build_teacher_prompt(turn_plan, inputs)

        # Append retry-feedback addendum per L62 if present.
        if prior_attempts:
            prior_block = "\n\n--- PRIOR ATTEMPTS (do NOT repeat the same mistakes) ---\n"
            for i, attempt in enumerate(prior_attempts, start=1):
                prior_block += f"\nAttempt {i}: {attempt}\n"
                if prior_failures and len(prior_failures) >= i:
                    f = prior_failures[i - 1]
                    prior_block += (
                        f"  → failed {f.get('_check_name', '?')}: "
                        f"{f.get('reason', '?')}\n"
                    )
            prompt = prompt + prior_block

        t0 = time.time()
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[{"role": "user", "content": prompt}],
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
