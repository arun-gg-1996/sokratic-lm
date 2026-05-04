"""
conversation/turn_plan.py
─────────────────────────
TurnPlan — single-source contract from Dean to Teacher per L46.

Dean's per-turn LLM call EMITS a TurnPlan. Teacher's single entry point
(L49) CONSUMES a TurnPlan. Every field is locked in
docs/AUDIT_2026-05-02.md L46. The dataclass + Pydantic-style validation
in this module is the only place that touches the schema; both sides of
the Dean↔Teacher boundary stay in sync by construction.

Two important design rules:
  * `mode` and `tone` are ORTHOGONAL (Codex round-2 fix #3).
      mode = the SITUATION/PHASE the message addresses
             (socratic, clinical, rapport, opt_in, redirect, nudge,
              confirm_end, honest_close)
      tone = the EMOTIONAL REGISTER of the message
             (encouraging, firm, neutral, honest)
    Teacher's prompt picker uses `mode`; Teacher's phrasing instruction
    uses `tone`. They never get conflated.
  * `apply_redaction` is ALWAYS False in Option C (per L43). The field
    exists for forward-compat with Phase 6's optional Option B switch
    but is never True today. Validation enforces this.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Locked enum values per L46 (Codex round-2 fix #3)
# ─────────────────────────────────────────────────────────────────────────────

# `mode` selects which Teacher prompt path to render.
MODES = {
    "socratic",                # standard tutoring turn (Q + chunk-grounded scaffolding)
    "clinical",                # clinical-phase turn
    "rapport",                 # opening greeting (rapport_node only)
    "opt_in",                  # clinical opt-in prompt (yes/no after core answer reached)
    "redirect",                # pre-flight redirect (off-domain / help-abuse / deflection)
    "nudge",                   # ultra-light prompt — student needs minor poke
    "confirm_end",             # student requested end-of-session — confirm
    "honest_close",            # legacy — kept for back-compat; new code uses "close"
    "reach_close",             # legacy — kept for back-compat; new code uses "close"
    "clinical_natural_close",  # legacy — kept for back-compat; new code uses "close"
    "close",                   # M1 unified close mode — reason flag picks tone/text variant
}

# `tone` shapes phrasing — orthogonal to mode.
TONES = {
    "encouraging",    # supportive, validating effort
    "firm",           # direct, refocusing, no soft-pedal
    "neutral",        # plain, neither warm nor stern
    "honest",         # candid, used in honest-tone closes per L26
}

# Reach outcomes derivable from chunks/answer comparison
DEFAULT_SHAPE_SPEC = {
    "max_sentences": 4,
    "exactly_one_question": True,
}


# ─────────────────────────────────────────────────────────────────────────────
# TurnPlan dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TurnPlan:
    """Dean's per-turn instructions to Teacher. All fields per L46.

    Construction options:
      * `TurnPlan(...)` — direct, validated by `__post_init__`.
      * `TurnPlan.from_llm_json(text)` — parse Dean's JSON response,
         tolerant of markdown fences + missing optional fields.
      * `TurnPlan.minimal_fallback(scenario, hint_text)` — emergency
         fallback per L46 ("if 2nd parse fails, ship a minimal TurnPlan").
    """

    # Required
    scenario: str
    hint_text: str
    mode: str
    tone: str

    # Required-with-default (lists/dicts default to empty)
    permitted_terms: list[str] = field(default_factory=list)
    forbidden_terms: list[str] = field(default_factory=list)
    shape_spec: dict = field(default_factory=lambda: dict(DEFAULT_SHAPE_SPEC))
    carryover_notes: str = ""
    hint_suggestions: list[str] = field(default_factory=list)

    # Always False in Option C per L43 / L52
    apply_redaction: bool = False

    # Informational only — authority is the reach gate (L53)
    student_reached_answer: bool = False

    # VLM (per L77) — null for text-only sessions
    image_context: Optional[dict] = None

    # Clinical phase fields (per L74) — null in tutoring phase
    clinical_scenario: Optional[str] = None
    clinical_target: Optional[str] = None

    # M6 — exploration retrieval signal. Default False = reuse lock-time
    # chunks (preserves prompt-cache contract). Set True when student asked
    # about an OT-related sub-aspect not in current chunks. exploration_query
    # is the focused query string Dean wants to retrieve for.
    needs_exploration: bool = False
    exploration_query: str = ""

    # ── Validation ───────────────────────────────────────────────────────

    def __post_init__(self):
        if not isinstance(self.scenario, str) or not self.scenario.strip():
            raise ValueError("TurnPlan.scenario must be a non-empty string")
        if not isinstance(self.hint_text, str):
            raise ValueError("TurnPlan.hint_text must be a string (may be empty)")
        if self.mode not in MODES:
            raise ValueError(
                f"TurnPlan.mode={self.mode!r} not in {sorted(MODES)}"
            )
        if self.tone not in TONES:
            raise ValueError(
                f"TurnPlan.tone={self.tone!r} not in {sorted(TONES)}"
            )
        # Option C invariant per L43 / L52
        if self.apply_redaction:
            raise ValueError(
                "apply_redaction must be False under Option C "
                "(reserved for future Phase 6 Option B)"
            )
        # Forbidden / permitted terms must be lists of strings
        for fname in ("permitted_terms", "forbidden_terms", "hint_suggestions"):
            v = getattr(self, fname)
            if not isinstance(v, list):
                raise ValueError(f"TurnPlan.{fname} must be a list (got {type(v).__name__})")
            for item in v:
                if not isinstance(item, str):
                    raise ValueError(f"TurnPlan.{fname} items must be strings (got {type(item).__name__})")
        if not isinstance(self.shape_spec, dict):
            raise ValueError("TurnPlan.shape_spec must be a dict")
        if "max_sentences" in self.shape_spec and not isinstance(
            self.shape_spec["max_sentences"], int
        ):
            raise ValueError("shape_spec.max_sentences must be int")
        if "exactly_one_question" in self.shape_spec and not isinstance(
            self.shape_spec["exactly_one_question"], bool
        ):
            raise ValueError("shape_spec.exactly_one_question must be bool")

    # ── Construction helpers ─────────────────────────────────────────────

    @classmethod
    def from_llm_json(cls, text: str) -> "TurnPlan":
        """Parse Dean's JSON response into a validated TurnPlan.

        Strips markdown fences, then json.loads. Raises ValueError on
        anything that fails validation — caller (Dean's _setup_call site)
        catches and re-prompts with a stricter instruction per L46.
        """
        s = (text or "").strip()
        if s.startswith("```"):
            lines = s.split("\n")
            s = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError as e:
            # Last-ditch — find first JSON object
            import re as _re
            m = _re.search(r"\{.*\}", s, _re.DOTALL)
            if not m:
                raise ValueError(f"No JSON object in TurnPlan response: {text[:200]!r}") from e
            parsed = json.loads(m.group(0))
        if not isinstance(parsed, dict):
            raise ValueError(f"TurnPlan response is not a JSON object: {parsed!r}")

        # Coerce shape_spec from None / partial dict
        shape_spec = parsed.get("shape_spec") or dict(DEFAULT_SHAPE_SPEC)
        if not isinstance(shape_spec, dict):
            shape_spec = dict(DEFAULT_SHAPE_SPEC)

        # Build kwargs explicitly — we do NOT trust the LLM to know which
        # keys are required vs optional. Drop unknown keys silently
        # (Dean prompts may evolve; old keys shouldn't break parsing).
        kwargs = {
            "scenario": str(parsed.get("scenario") or "").strip(),
            "hint_text": str(parsed.get("hint_text") or ""),
            "mode": str(parsed.get("mode") or "socratic").strip().lower(),
            "tone": str(parsed.get("tone") or "neutral").strip().lower(),
            "permitted_terms": list(parsed.get("permitted_terms") or []),
            "forbidden_terms": list(parsed.get("forbidden_terms") or []),
            "shape_spec": shape_spec,
            "carryover_notes": str(parsed.get("carryover_notes") or ""),
            "hint_suggestions": list(parsed.get("hint_suggestions") or []),
            "apply_redaction": bool(parsed.get("apply_redaction") or False),
            "student_reached_answer": bool(parsed.get("student_reached_answer") or False),
            "image_context": parsed.get("image_context"),
            "clinical_scenario": parsed.get("clinical_scenario"),
            "clinical_target": parsed.get("clinical_target"),
            "needs_exploration": bool(parsed.get("needs_exploration") or False),
            "exploration_query": str(parsed.get("exploration_query") or "").strip(),
        }
        # Defensive: coerce list items to strings if LLM returned weird types
        kwargs["permitted_terms"] = [str(x) for x in kwargs["permitted_terms"]]
        kwargs["forbidden_terms"] = [str(x) for x in kwargs["forbidden_terms"]]
        kwargs["hint_suggestions"] = [str(x) for x in kwargs["hint_suggestions"]]
        return cls(**kwargs)

    @classmethod
    def minimal_fallback(
        cls,
        *,
        scenario: str = "fallback_minimal_after_parse_failures",
        hint_text: str = "",
        tone: str = "neutral",
    ) -> "TurnPlan":
        """Per L46: emergency fallback if Dean's TurnPlan parse fails twice.

        Ships with mode="socratic", tone="neutral", strict shape_spec
        (max 3 sentences, exactly one question). No permitted/forbidden
        terms (Teacher must rely on raw chunks + leak_check to avoid
        regression).
        """
        return cls(
            scenario=scenario,
            hint_text=hint_text,
            mode="socratic",
            tone=tone,
            permitted_terms=[],
            forbidden_terms=[],
            shape_spec={"max_sentences": 3, "exactly_one_question": True},
            carryover_notes="",
            hint_suggestions=[],
            apply_redaction=False,
            student_reached_answer=False,
            image_context=None,
            clinical_scenario=None,
            clinical_target=None,
            needs_exploration=False,
            exploration_query="",
        )

    # ── Serialization ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Round-trippable dict suitable for JSON serialization (e.g. trace)."""
        return asdict(self)

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)
