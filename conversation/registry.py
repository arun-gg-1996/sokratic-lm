"""
conversation/registry.py
========================
Single source of truth for all vocabulary the conversation system uses.

Each vocabulary class is a registry of values + their definitions. The
SAME definitions are consumed by:

  1. Classifier system prompts (so LLM knows what verdicts to output)
  2. Teacher / Dean system prompts (so LLM knows what verdicts MEAN
     when seen in conversation history annotations)
  3. Runtime history annotations (so LLM sees actual values per turn)

Adding a new vocabulary item is a 1-line diff in the relevant dict.
The classifier prompt + Teacher/Dean prompts + history rendering all
update automatically.

Pattern: production agent frameworks (OpenAI function calling,
Anthropic tool_use, LangChain BaseTool) all use this same approach
of single-source-of-truth schemas.

SAFETY CONTRACT (Safeguard #2):
- Registry definitions must be GENERIC (no topic-specific data).
- No anatomy / medical terms in any value (no "thyroid",
  "glycolysis", "pyruvate", etc.).
- Enforced by tests/test_registry.py.
"""
from __future__ import annotations

from typing import Any


def _format_extras(extras: dict[str, Any]) -> str:
    """Render extras dict as compact ', k=v' string. Stable iteration order."""
    if not extras:
        return ""
    parts = []
    for k in sorted(extras.keys()):
        v = extras[k]
        parts.append(f"{k}={v}")
    return ", " + ", ".join(parts)


class IntentVocabulary:
    """Intent verdicts emitted by the unified preflight classifier.

    Every student message gets EXACTLY ONE verdict. on_topic_engaged is the
    catchall for messages that don't trigger a specific intervention but
    still represent genuine engagement.
    """

    INTENTS: dict[str, str] = {
        "on_topic_engaged": (
            "Student is genuinely engaging with the locked topic in good "
            "faith — partial answer, clarifying question, hedge, guess, "
            "follow-up question. The default when nothing else fits."
        ),
        "low_effort": (
            "Minimal-engagement response: 'idk', 'i don't know', 'no idea', "
            "'not sure', '?', single-word non-engagement. Passive "
            "disengagement — distinct from help_abuse which is an active "
            "demand. Triggers escalation when consecutive."
        ),
        "help_abuse": (
            "Active attempt to short-circuit the Socratic process: 'just "
            "tell me', 'what's the answer', 'skip', 'make it easier', "
            "demands for direct answer."
        ),
        "off_domain": (
            "Off-topic chatter, jailbreak attempt, or unrelated subject. "
            "NOT off_domain if the question relates to the locked "
            "subsection."
        ),
        "deflection": (
            "Wants to end or decline the session: 'let's stop', 'I'm done', "
            "'I have to go', 'no thanks not today', 'wrap up'."
        ),
        "opt_in_yes": (
            "Affirmative reply ONLY when phase=assessment and prior tutor "
            "turn offered a clinical bonus. Examples: 'yes', 'yeah', "
            "'let's do it', 'sure'."
        ),
        "opt_in_no": (
            "Negative reply in opt-in context: 'no', 'skip', 'wrap up here'. "
            "Distinct from deflection — student is cleanly declining the "
            "bonus, not exiting frustrated."
        ),
        "opt_in_ambiguous": (
            "In opt-in context but reply unclear: 'ok', 'maybe', or a "
            "substantive answer that doesn't address the offer."
        ),
    }

    @classmethod
    def system_prompt_block(cls) -> str:
        """Render for LLM system prompt — explains vocabulary to the LLM.

        Used by classifier prompt (so LLM knows verdicts to emit) AND
        by Teacher/Dean prompts (so LLM knows what verdicts MEAN when
        seen in history annotations).
        """
        lines = [
            "INTENT VERDICTS (emitted by preflight classifier; visible "
            "annotated on STUDENT turns in CONVERSATION HISTORY):"
        ]
        for k, v in cls.INTENTS.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @classmethod
    def annotate(cls, verdict: str, **extras: Any) -> str:
        """Render inline annotation for a turn in history.

        Example: annotate("low_effort", consecutive_low_effort=3)
                 → "[intent=low_effort, consecutive_low_effort=3]"
        """
        if verdict not in cls.INTENTS:
            verdict = "on_topic_engaged"
        return f"[intent={verdict}{_format_extras(extras)}]"


class TeacherModeVocabulary:
    """Teacher-mode taxonomy — what KIND of message Teacher renders this turn.

    Mode is selected by Dean (or by preflight bypass for redirect/nudge/
    confirm_end) and passed to Teacher in TurnPlan.
    """

    MODES: dict[str, str] = {
        "socratic": (
            "Standard Socratic tutoring scaffold — ask one question to "
            "nudge student toward locked answer using Dean's hint."
        ),
        "clinical": (
            "Clinical-application scenario — student demonstrated core "
            "concept, now testing application reasoning."
        ),
        "rapport": (
            "Opening greeting at session start. Brief, natural, "
            "register-matching."
        ),
        "opt_in": (
            "Offer optional clinical bonus question. One sentence, yes/no."
        ),
        "redirect": (
            "Student signaled help-abuse — gentle reframe, invite even a "
            "partial guess. No hint advance."
        ),
        "nudge": (
            "Student went off-domain — briefly refocus to locked topic."
        ),
        "confirm_end": (
            "Student signaled session-end — confirm intent. Frontend "
            "renders YES/NO buttons after this."
        ),
        "honest_close": (
            "Graceful end — student didn't reach answer (gave up, ran "
            "out of turns, multiple strikes)."
        ),
        "reach_close": (
            "Graceful end — student reached answer, declined clinical "
            "bonus. Warm but not over-praising."
        ),
        "clinical_natural_close": (
            "Clinical phase hit its turn cap before resolving target. "
            "Acknowledge work without revealing target answer."
        ),
        "close": (
            "Unified close mode (M1 redesign) — driven by close_reason "
            "in CARRYOVER NOTES. Emits structured JSON with message + "
            "demonstrated + needs_work."
        ),
        "soft_reset": (
            "Student just canceled the exit modal — they want to keep "
            "going. Provide a completely fresh angle on locked_question, "
            "explicitly suppress the prior analogy. Acknowledge the "
            "choice to continue without dwelling on it."
        ),
        "multichoice_rescue": (
            "Student gave low_effort responses 2+ times in a row. "
            "Pivot to a CLOSED CHOICE — present 2-3 specific candidates "
            "and ask 'is it A, B, or C?'. The hint_text contains "
            "slash-separated candidates."
        ),
    }

    @classmethod
    def system_prompt_block(cls) -> str:
        lines = [
            "TEACHER MODES (Dean picks one per turn; visible annotated on "
            "TUTOR turns in CONVERSATION HISTORY):"
        ]
        for k, v in cls.MODES.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @classmethod
    def annotate(cls, mode: str, **extras: Any) -> str:
        """Example: annotate("redirect", tone="firm", attempts=2)
                    → "[mode=redirect, attempts=2, tone=firm]"
        """
        if mode not in cls.MODES:
            mode = "socratic"
        return f"[mode={mode}{_format_extras(extras)}]"


class ToneTierVocabulary:
    """Tone tiers — selected by Dean per turn, escalates with off-topic strikes."""

    TONES: dict[str, str] = {
        "encouraging": (
            "Supportive, validating effort. Used when student engages "
            "even if wrong."
        ),
        "neutral": (
            "Plain, unembellished. Default for redirects and opt-ins."
        ),
        "firm": (
            "Direct, refocusing. Used after repeated off-domain strikes."
        ),
        "honest": (
            "Candid, no fake hype. Used for honest_close."
        ),
    }

    @classmethod
    def system_prompt_block(cls) -> str:
        lines = ["TONE TIERS (Dean picks one per turn):"]
        for k, v in cls.TONES.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @classmethod
    def annotate(cls, tone: str) -> str:
        if tone not in cls.TONES:
            tone = "neutral"
        return f"tone={tone}"


class PhaseVocabulary:
    """Conversation lifecycle phases."""

    PHASES: dict[str, str] = {
        "rapport": (
            "Opening greeting + topic-card or anchor-pick selection. "
            "First phase of every session."
        ),
        "tutoring": (
            "Main Socratic loop — Dean plans, Teacher drafts, Verifier "
            "checks. Hints escalate 0→1→2→3 as student misses."
        ),
        "assessment": (
            "Optional clinical bonus phase — opt-in then clinical loop. "
            "Entered after student reaches locked answer."
        ),
        "memory_update": (
            "Session close — mastery saved, key takeaways recorded. "
            "Final phase. After this, session_ended=true."
        ),
    }

    @classmethod
    def system_prompt_block(cls) -> str:
        lines = ["CONVERSATION PHASES:"]
        for k, v in cls.PHASES.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @classmethod
    def annotate(cls, phase: str) -> str:
        if phase not in cls.PHASES:
            phase = "tutoring"
        return f"phase={phase}"


class ModalEventVocabulary:
    """System events that may appear inline in conversation history.

    These give the LLM visibility into UI/state events the student took
    or saw, beyond just text exchanges.
    """

    EVENTS: dict[str, str] = {
        "anchor_pick_shown": (
            "3 question variations were shown to the student as cards. "
            "Their next message either picks one or types a new question."
        ),
        "topic_cards_shown": (
            "List of suggested topics shown for selection. Student's next "
            "message picks one or types a custom topic."
        ),
        "exit_modal_shown": (
            "End-session confirmation modal popped (triggered by "
            "deflection intent). Student is asked to confirm or cancel."
        ),
        "exit_modal_confirmed": (
            "Student clicked End on the modal — session is closing."
        ),
        "exit_modal_canceled": (
            "Student clicked Cancel on the modal — they're continuing. "
            "Next tutor turn should acknowledge this and offer a fresh "
            "angle (soft_reset mode)."
        ),
        "phase_change": (
            "Conversation phase transitioned (e.g. rapport → tutoring)."
        ),
        "hint_advance": (
            "Hint level incremented by Dean (student attempted answer, missed)."
        ),
        "topic_locked": (
            "Topic + anchor question + answer locked for tutoring. Tutoring "
            "loop begins with the next turn."
        ),
        "preflight_intervened": (
            "Preflight classifier fired a non-engaged verdict (help_abuse "
            "/ off_domain / deflection). Normal Dean planning was bypassed "
            "for the next tutor turn."
        ),
    }

    @classmethod
    def system_prompt_block(cls) -> str:
        lines = [
            "SYSTEM EVENTS (may appear inline in CONVERSATION HISTORY, "
            "marked with SYSTEM_EVENT prefix):"
        ]
        for k, v in cls.EVENTS.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @classmethod
    def annotate(cls, event: str, **payload: Any) -> str:
        """Render a system event line for history.

        Example: annotate("phase_change", from_phase="rapport", to_phase="tutoring")
                 → "SYSTEM_EVENT: phase_change, from_phase=rapport, to_phase=tutoring"
        """
        if event not in cls.EVENTS:
            event = "phase_change"
        return f"SYSTEM_EVENT: {event}{_format_extras(payload)}"


class HintTransitionVocabulary:
    """Hint-level transitions — what happened to hint_level on a turn."""

    TRANSITIONS: dict[str, str] = {
        "advance": (
            "Hint level incremented by 1 (student attempted answer, missed)."
        ),
        "freeze": (
            "Hint level held (student gave low-effort, no advance per Dean rules)."
        ),
        "cap": (
            "Hint level reached max+1 (exhausted) — routes to memory_update."
        ),
    }

    @classmethod
    def system_prompt_block(cls) -> str:
        lines = ["HINT TRANSITIONS:"]
        for k, v in cls.TRANSITIONS.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


def all_vocabulary_blocks() -> str:
    """Combined system-prompt block of ALL vocabularies.

    Use this in master prompt assembly to give the LLM a complete
    ontology of the system in one place.
    """
    blocks = [
        IntentVocabulary.system_prompt_block(),
        TeacherModeVocabulary.system_prompt_block(),
        ToneTierVocabulary.system_prompt_block(),
        PhaseVocabulary.system_prompt_block(),
        ModalEventVocabulary.system_prompt_block(),
        HintTransitionVocabulary.system_prompt_block(),
    ]
    return "\n\n".join(blocks)
