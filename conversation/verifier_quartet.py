"""
conversation/verifier_quartet.py
─────────────────────────────────
4 post-draft Haiku safety checks (verifier quartet) + result adapter.

Lifecycle: AFTER Teacher draft, BEFORE delivering to student. If any
check flags the draft, the retry orchestrator either rewrites or falls
back to SAFE_GENERIC_PROBE.

Checks:
  haiku_hint_leak_check    — does draft leak locked_answer / aliases / chunk content?
  haiku_sycophancy_check   — does draft confirm a wrong claim or over-praise?
  haiku_shape_check        — does draft conform to plan.shape_spec?
  haiku_pedagogy_check     — does draft preserve Socratic stance?

Plus to_universal_check_result — adapter to the universal
{pass, reason, evidence} shape consumed by the retry orchestrator.

Shared infrastructure (_haiku_call, _extract_json, _validate_evidence,
_cached_system_block, model constants) lives in conversation/classifiers.py
and is imported below.

Ported during D3 (architectural split). Behavior unchanged — pure
namespace move from a colocated 1120-line classifiers.py.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from conversation.classifiers import (
    _haiku_call,
    _extract_json,
    _validate_evidence,
    _cached_system_block,
)


_HINT_LEAK_SYSTEM = """\
You are a teaching-quality reviewer for a Socratic anatomy tutor.
Your sole task: judge whether a draft tutor response REVEALS the
locked answer — either by naming it outright OR through indirect
hints (letter / morphology / etymology / blank / multiple-choice /
synonym / acronym).

A leak is any phrasing that hands the student the answer or its
SHAPE without making them recall it. The student should still need
to deduce the term. The locked_answer is the canonical textbook term;
aliases are paraphrases that count equally.

FLAG (verdict="leak") if the draft does ANY of these:

0. Names the locked answer (or any alias) outright. This is the
   single strongest leak — Teacher must NEVER state the answer.
   Match case-insensitively as a whole-word/phrase substring:
     locked_answer="pyruvate"; draft="The answer is pyruvate"
        → leak (verbatim mention)
     locked_answer="pyruvate"; draft="Yes, pyruvate is what glycolysis produces"
        → leak (verbatim mention even when phrased as confirmation)
     locked_answer="SA node"; draft="The SA node initiates the heartbeat"
        → leak (declaratively states the answer)
     locked_answer="axillary nerve"; alias="axillary"; draft="...the
        axillary nerve runs through the surgical neck..."
        → leak (alias + answer mentioned outright)
     locked_answer="autorhythmicity"; draft="That property is called
        autorhythmicity"
        → leak (names the answer in a definition)
   The cardinal rule: if the draft pronounces the answer at the
   student, it has handed it to them — leak regardless of phrasing.

1. Reveals letters or starts-with patterns:
     "starts with 'n'"        → leak (single letter)
     "begins with 'SA'"        → leak (two-letter abbreviation)
     "ends with 'ase'"         → leak (suffix letters)
     "begins with the letter f" → leak

2. Uses blank-completion format that gives the word shape:
     "comm-____?"
     "the suffix that completes 'compl-_____'"
     "fill in the blank: ____-tion"

3. Reveals etymology / Latin / Greek roots:
     "from a Latin root meaning together"
     "derived from Greek for blood"
     "the Greek prefix 'hepato-' means liver"

4. Lists multiple-choice options that contain the answer:
     "A) Glucose B) Oxygen C) ATP D) Lactic acid"
     "Is it (a) the SA node, (b) the AV node, or (c) Purkinje fibers?"

5. Hands over the layman / technical synonym:
     "the everyday word for that is the funny bone"
     "the medical word for funny bone is the elbow nerve"
     "the technical term for breathing is respiration"
     "the common English word for epidermis is the outer layer"

6. Reveals an acronym or initialism expansion:
     "ATP stands for adenosine triphosphate"
     "the abbreviation RAAS means renin-angiotensin-aldosterone system"
     "made up of the first letters of three words"
     "each letter represents a different cell type"

DO NOT flag (verdict="clean") for these legitimate Socratic moves:

A. Naming a broader anatomical region or system that contains the
   answer. Example: locked_answer="SA node", draft says
   "What part of the conduction system initiates the heartbeat?" —
   "conduction system" is broader context, not a letter/morphology hint.

B. Describing a property the student should derive the term FROM,
   without revealing the term:
     "What property lets these cells depolarize on their own?" — fine
     even if locked_answer is "autorhythmicity".

C. Mentioning unrelated structures from retrieved chunks for contrast:
     "...unlike the AV node which..." — fine even if AV node is in chunks.

D. Asking conceptual questions that require recall, not just letter
   recognition:
     "What kind of cells in this region would do this job?"
     "What term does the textbook use for this kind of cell?"

E. Standard Socratic openers:
     "Tell me what you already know about..."
     "What do you think happens when..."
     "Walk me through your reasoning..."

F. Mentioning the locked_topic's broad system / chapter context:
     "Within the cardiovascular system, what specifically..."

Output ONE JSON object EXACTLY in this shape, with no prose around it:

{
  "rationale": "<one sentence explaining what you saw>",
  "evidence": "<verbatim substring of the draft that constitutes the leak, or empty if clean>",
  "verdict": "leak" | "clean",
  "leak_type": "letter" | "blank" | "etymology" | "mcq" | "synonym" | "acronym" | ""
}

Asymmetric stakes:
  - False positive (clean turn flagged as leak) → one unnecessary
    Dean rewrite, mild cost.
  - False negative (real leak slips through) → silently hands the
    answer to the student, severe pedagogical cost.

When genuinely ambiguous (50/50), prefer "leak". When the only
"reveal" is a broad system/region name (rule A above), prefer "clean".

The "evidence" field MUST be a verbatim substring of the draft. If you
cannot quote a specific phrase, return verdict="clean" with empty
evidence.
"""


_HINT_LEAK_USER_TEMPLATE = """\
LOCKED ANSWER (the term the student should still deduce):
{locked_answer}

KNOWN ALIASES / paraphrases (also leaks if revealed by letter / morphology):
{aliases}

DRAFT TUTOR RESPONSE TO REVIEW:
{draft}

Return only the JSON object."""


def haiku_hint_leak_check(draft: str, locked_answer: str, aliases: list[str] | None = None) -> dict:
    """Detect verbatim mention / hint-3 / morphology / etymology / blank /
    MCQ / synonym / acronym leaks. Asymmetric retry-on-clean for safety.

    Wraps `_haiku_hint_leak_check_once` with a confirmation pass:
      - Call once. If verdict='leak' → trust it (fast path).
      - If verdict='clean' → call ONCE more to confirm (slow path).
      - If second call says 'leak' → return leak (per the prompt's stated
        asymmetric-stakes rule: "When genuinely ambiguous, prefer leak.").
      - If both 'clean' → confidently clean.

    Why: Bedrock Haiku at temp=0 is non-deterministic (~30% miss rate
    observed on the V1 baseline draft "starts with letter P" across 10
    serial calls). The double-check on `clean` drops miss rate to ~9%.
    Cost is asymmetric: leaks remain 1 call, cleans cost 2 calls. The
    L1 leak guard is the highest-stakes verifier — favour latency over
    silent false negatives.

    Returns same dict shape as `_haiku_hint_leak_check_once` plus a
    `_consensus` field: "first_leak" | "split_first_clean_second_leak"
    | "both_clean".
    """
    first = _haiku_hint_leak_check_once(draft, locked_answer, aliases)
    if first.get("verdict") == "leak":
        first["_consensus"] = "first_leak"
        return first
    # First said "clean" — confirm. If the prompt itself errored, don't
    # retry (would just error again); trust the safe default.
    if first.get("_error"):
        first["_consensus"] = "first_clean_errored"
        return first
    second = _haiku_hint_leak_check_once(draft, locked_answer, aliases)
    if second.get("verdict") == "leak":
        # Two-call disagreement → prefer leak (asymmetric stakes).
        second["_consensus"] = "split_first_clean_second_leak"
        return second
    first["_consensus"] = "both_clean"
    # Aggregate elapsed across both calls for telemetry honesty.
    first["_elapsed_s"] = round(
        float(first.get("_elapsed_s") or 0.0) + float(second.get("_elapsed_s") or 0.0),
        3,
    )
    return first


def _haiku_hint_leak_check_once(draft: str, locked_answer: str, aliases: list[str] | None = None) -> dict:
    """Single-shot leak check (no retry). The public wrapper above adds
    a confirmation pass on `clean` verdicts. Use this directly only when
    you specifically need the raw single-call behavior (e.g. from a
    panel test that's measuring single-call accuracy).

    Returns dict:
      verdict:    "leak" | "clean"
      leak_type:  "letter" | "blank" | "etymology" | "mcq" | "synonym" |
                  "acronym" | ""
      evidence:   verbatim substring of the draft, or "" if clean
      rationale:  1-sentence explanation
      _elapsed_s: wall time of the LLM call
      _raw:       raw response text (for debug)
      _error:     "parse_fail" | "evidence_invalid" | "" (empty on success)

    Safe defaults on error: verdict="clean" (don't false-fire on
    parser issues; Dean QC will re-examine).
    """
    t0 = time.time()
    if not draft or not (locked_answer or "").strip():
        return {
            "verdict": "clean", "leak_type": "", "evidence": "",
            "rationale": "no draft or no locked answer to check against",
            "_elapsed_s": 0.0, "_raw": "", "_error": "",
        }
    aliases_str = ", ".join(a for a in (aliases or []) if isinstance(a, str)) or "(none)"
    user_text = _HINT_LEAK_USER_TEMPLATE.format(
        locked_answer=locked_answer, aliases=aliases_str, draft=draft,
    )
    try:
        raw = _haiku_call(_cached_system_block(_HINT_LEAK_SYSTEM), user_text)
    except Exception as e:
        return {
            "verdict": "clean", "leak_type": "", "evidence": "",
            "rationale": f"haiku_call_error: {type(e).__name__}",
            "_elapsed_s": round(time.time() - t0, 3),
            "_raw": "", "_error": "haiku_error",
        }
    elapsed = round(time.time() - t0, 3)
    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "verdict": "clean", "leak_type": "", "evidence": "",
            "rationale": "json_parse_fail",
            "_elapsed_s": elapsed, "_raw": raw, "_error": "parse_fail",
        }
    verdict = str(parsed.get("verdict", "clean")).strip().lower()
    if verdict not in {"leak", "clean"}:
        verdict = "clean"
    evidence = str(parsed.get("evidence", "") or "")
    leak_type = str(parsed.get("leak_type", "") or "")
    rationale = str(parsed.get("rationale", "") or "")[:240]
    error = ""
    # Evidence-quote validation: if model claims a leak, the cited
    # substring MUST appear verbatim in the draft. If not, downgrade.
    if verdict == "leak" and evidence and not _validate_evidence(evidence, draft):
        verdict = "clean"
        leak_type = ""
        error = "evidence_invalid"
    return {
        "verdict": verdict,
        "leak_type": leak_type,
        "evidence": evidence,
        "rationale": rationale,
        "_elapsed_s": elapsed,
        "_raw": raw,
        "_error": error,
    }


# ─────────────────────────────────────────────────────────────────────
#                     CLASSIFIER 2 — SYCOPHANCY
# ─────────────────────────────────────────────────────────────────────

_SYCOPHANCY_SYSTEM = """\
You evaluate whether a tutor message contains SYCOPHANTIC affirmation
— telling the student they are correct, on the right track, or have
the right idea, BEFORE the student has earned that feedback.

Sycophancy undermines the Socratic method by teaching students that
gut-feel guesses are correct. The tutor must withhold strong
affirmation until the student demonstrates understanding through
reasoning, not just term-naming.

You will be given the student's classified state on this turn:
  correct          — student articulated the answer with reasoning
  partial_correct  — student got part of the answer or hedged
  incorrect        — student gave a wrong answer
  question         — student asked a question instead of answering
  irrelevant       — student went off-topic
  low_effort       — student gave "idk" / no genuine attempt

And whether the reach gate fired (i.e. system-confirmed the student
named the answer).

FLAG (verdict="sycophantic") if the draft shows ANY of these:

1. Strong affirmation while student state is NOT correct OR reach
   gate did NOT fire:
     "You're right" / "Exactly" / "Perfect" / "That's correct" /
     "You got it" / "Spot on" / "Excellent"

2. Soft-affirmation phrases that mistake confusion for progress
   (these are sycophantic regardless of student state, because they
   tell the student they're succeeding when they may not be):
     "You're on an interesting track"
     "You're on the right track"
     "You're heading in the right direction"
     "Partly right" / "You're getting there"
     "in the right neighborhood"
     "Good intuition" / "Nice thinking" / "Great approach"
     "You've already touched on the key idea"
     "You've hinted at the answer"

3. Premature attribution of reasoning the student didn't show:
     "You've identified the key concept" — when student only named
       one piece of a multi-component answer.
     "You've connected X to Y" — when student named X but not the
       connection.

DO NOT flag (verdict="clean") for these legitimate moves:

A. Affirmation when the student WAS objectively correct AND the
   reach gate fired. Example:
     student_state="correct", reach_fired=True, draft="Yes — that's
     the SA node. Now let's see how that connects to..."  → CLEAN.

B. Engagement openers that don't make truth claims:
     "Let's think about that..."
     "Take a moment to consider..."
     "Walk me through your reasoning..."

C. Polite redirect of off-topic, not affirmation:
     "Let's stay focused on the heart for now..."

D. Validation of effort/engagement, not correctness:
     "Good question — let's work through this together"
     (but careful: "good question" + claiming the question shows
     understanding crosses into sycophancy).

E. Neutral re-statement of what the student said without endorsing it:
     "So you're saying the chambers contract — let's test that..."

Output JSON exactly:

{
  "rationale": "<one sentence>",
  "evidence": "<verbatim substring or empty>",
  "verdict": "sycophantic" | "clean"
}

Asymmetric stakes:
  - False positive (one polite line gets rewritten more neutrally) → mild.
  - False negative (sycophancy slips through and student feels they're
    correct when they're not) → moderate harm.

When ambiguous AND student_state ∈ {incorrect, partial_correct,
question, low_effort}, prefer "sycophantic". When student_state="correct"
AND reach_fired=True, prefer "clean" — affirmation is earned.

The "evidence" field MUST be a verbatim substring of the draft. If you
cannot quote a specific phrase, return verdict="clean".
"""


_SYCOPHANCY_USER_TEMPLATE = """\
Student state on this turn: {student_state}
Reach gate fired this turn: {reach_fired}

DRAFT TUTOR RESPONSE TO REVIEW:
{draft}

Return only the JSON object."""


def haiku_sycophancy_check(draft: str, student_state: str, reach_fired: bool = False) -> dict:
    """Detect sycophantic affirmation. Asymmetric retry-on-clean wrapper.

    Bedrock Haiku at temp=0 has ~30% non-determinism on borderline cases.
    A single 'clean' verdict may be a false negative that ships sycophantic
    text to a student. This wrapper retries on `clean` to cut miss rate
    from ~30% to ~9% (9% = chance both calls miss). On `sycophantic`,
    trusts the first call (fast path).
    """
    first = _haiku_sycophancy_check_once(draft, student_state, reach_fired)
    if first.get("verdict") == "sycophantic":
        first["_consensus"] = "first_sycophantic"
        return first
    if first.get("_error"):
        first["_consensus"] = "first_clean_errored"
        return first
    second = _haiku_sycophancy_check_once(draft, student_state, reach_fired)
    if second.get("verdict") == "sycophantic":
        second["_consensus"] = "split_first_clean_second_sycophantic"
        return second
    first["_consensus"] = "both_clean"
    first["_elapsed_s"] = round(
        float(first.get("_elapsed_s") or 0.0) + float(second.get("_elapsed_s") or 0.0), 3,
    )
    return first


def _haiku_sycophancy_check_once(draft: str, student_state: str, reach_fired: bool = False) -> dict:
    """Single-shot sycophancy check (no retry). Public wrapper above adds
    a confirmation pass on `clean` verdicts.

    Returns dict:
      verdict:    "sycophantic" | "clean"
      evidence:   verbatim substring of the draft, or ""
      rationale:  1-sentence explanation
      _elapsed_s: wall time of the LLM call
      _raw:       raw response text
      _error:     "parse_fail" | "evidence_invalid" | "" on success

    Safe default on error: verdict="clean".
    """
    t0 = time.time()
    if not (draft or "").strip():
        return {
            "verdict": "clean", "evidence": "",
            "rationale": "empty draft", "_elapsed_s": 0.0,
            "_raw": "", "_error": "",
        }
    state_str = str(student_state or "unknown")
    reach_str = "yes" if reach_fired else "no"
    user_text = _SYCOPHANCY_USER_TEMPLATE.format(
        student_state=state_str, reach_fired=reach_str, draft=draft,
    )
    try:
        raw = _haiku_call(_cached_system_block(_SYCOPHANCY_SYSTEM), user_text)
    except Exception as e:
        return {
            "verdict": "clean", "evidence": "",
            "rationale": f"haiku_call_error: {type(e).__name__}",
            "_elapsed_s": round(time.time() - t0, 3),
            "_raw": "", "_error": "haiku_error",
        }
    elapsed = round(time.time() - t0, 3)
    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "verdict": "clean", "evidence": "",
            "rationale": "json_parse_fail",
            "_elapsed_s": elapsed, "_raw": raw, "_error": "parse_fail",
        }
    verdict = str(parsed.get("verdict", "clean")).strip().lower()
    if verdict not in {"sycophantic", "clean"}:
        verdict = "clean"
    evidence = str(parsed.get("evidence", "") or "")
    rationale = str(parsed.get("rationale", "") or "")[:240]
    error = ""
    if verdict == "sycophantic" and evidence and not _validate_evidence(evidence, draft):
        verdict = "clean"
        error = "evidence_invalid"
    return {
        "verdict": verdict,
        "evidence": evidence,
        "rationale": rationale,
        "_elapsed_s": elapsed,
        "_raw": raw,
        "_error": error,
    }


# ─────────────────────────────────────────────────────────────────────
#                     CLASSIFIER 3 — OFF-DOMAIN
# ─────────────────────────────────────────────────────────────────────

_SHAPE_CHECK_SYSTEM = """\
You are a teaching-quality reviewer for a Socratic tutor. Your sole task
is to verify that a draft tutor response respects FIVE shape constraints:

  1. LENGTH      — draft has at most {max_sentences} sentences (count
                   complete sentences; questions count as sentences).
  2. SINGLE_QUESTION — draft contains EXACTLY one question (when
                   exactly_one_question=true). Multiple questions = fail.
                   Zero questions = fail. Rhetorical questions count.
  3. BANNED_PREFIX — draft does NOT start with empty praise:
                   "Great!", "Excellent!", "Perfect!", "You got it!",
                   "Wonderful!", "Awesome!", "Nice!", "Right!",
                   "Exactly!", "Correct!", or close variants.
  4. HINT_LEVEL_ALIGNMENT — directness is consistent with the current
                   hint_level:
                     level 0 = no hint, pure question
                     level 1 = oblique hint (think category, not specifics)
                     level 2 = moderate (mention adjacent concept)
                     level 3 = direct (almost gives the structural answer)
                   The draft should match this level. When ambiguous,
                   bias toward PASS (level 4 swap is offline; live false
                   positives hurt more than rare misses).
  5. NO_REPETITION — draft is not substantively the same as the
                   previous tutor turns. Three failure modes (any one
                   triggers fail):
                     (a) same question stem reworded — paraphrase of the
                         same question fails; new angle on same topic
                         passes.
                     (b) same first 8 words verbatim as a prior tutor
                         turn — shows the LLM ignored history.
                     (c) same opening soft-cushion phrase TWICE in a
                         row ("That's okay", "No worries", "Let me
                         reframe", "It can feel tricky") — must vary.
                   Treat (b) and (c) as harder fails than (a).

Output STRICT JSON only — no markdown, no preamble:
{{
  "pass": true | false,
  "reason": "<short explanation if pass=false; empty if pass>",
  "evidence": "<verbatim substring of draft that triggered failure; empty if pass>",
  "checks": {{
    "length": true | false,
    "single_question": true | false,
    "banned_prefix": true | false,
    "hint_level_alignment": true | false,
    "no_repetition": true | false
  }}
}}

The draft passes overall iff ALL five sub-checks pass.
"""


_SHAPE_CHECK_USER_TEMPLATE = """\
SHAPE CONSTRAINTS:
  max_sentences: {max_sentences}
  exactly_one_question: {exactly_one_question}
  hint_level: {hint_level}
  intended hint_text from Dean: {hint_text}

PREVIOUS TUTOR TURNS (full text, most recent first — check the draft's
first 8 words AND opening phrase against these for NO_REPETITION):
{prior_questions}

DRAFT TO CHECK:
{draft}
"""


def haiku_shape_check(
    draft: str,
    *,
    shape_spec: dict | None = None,
    hint_level: int = 0,
    hint_text: str = "",
    prior_tutor_questions: list[str] | None = None,
) -> dict:
    """Asymmetric retry-on-pass wrapper around _haiku_shape_check_once.

    Bedrock Haiku flakes ~30% on borderline cases. A single `pass=True`
    verdict may miss a real shape violation (no question, wrong sentence
    count, etc.). This wrapper retries on pass=True; if the second call
    says pass=False, returns that (asymmetric stakes — prefer the
    fail-flag when in doubt).
    """
    first = _haiku_shape_check_once(
        draft, shape_spec=shape_spec, hint_level=hint_level,
        hint_text=hint_text, prior_tutor_questions=prior_tutor_questions,
    )
    if not first.get("pass", True):
        first["_consensus"] = "first_fail"
        return first
    if first.get("_error"):
        first["_consensus"] = "first_pass_errored"
        return first
    second = _haiku_shape_check_once(
        draft, shape_spec=shape_spec, hint_level=hint_level,
        hint_text=hint_text, prior_tutor_questions=prior_tutor_questions,
    )
    if not second.get("pass", True):
        second["_consensus"] = "split_first_pass_second_fail"
        return second
    first["_consensus"] = "both_pass"
    first["_elapsed_s"] = round(
        float(first.get("_elapsed_s") or 0.0) + float(second.get("_elapsed_s") or 0.0), 3,
    )
    return first


def _haiku_shape_check_once(
    draft: str,
    *,
    shape_spec: dict | None = None,
    hint_level: int = 0,
    hint_text: str = "",
    prior_tutor_questions: list[str] | None = None,
) -> dict:
    """L48 #3 + L59 — five sub-checks in ONE Haiku call, no regex.

    Returns dict with universal L61 schema (pass/reason/evidence) plus
    a `checks` sub-dict for fine-grained failure attribution.

    Safe defaults on error: pass=True (don't false-fire on parser
    issues; downstream Haiku checks will catch real problems).
    """
    t0 = time.time()
    if not draft:
        return {
            "pass": True, "reason": "empty draft", "evidence": "",
            "checks": {}, "_elapsed_s": 0.0, "_raw": "", "_error": "empty_draft",
        }

    spec = shape_spec or {"max_sentences": 4, "exactly_one_question": True}
    max_sentences = int(spec.get("max_sentences", 4))
    exactly_one_question = bool(spec.get("exactly_one_question", True))
    prior = "\n".join(f"- {q}" for q in (prior_tutor_questions or [])[:2]) or "(none)"

    system_text = _SHAPE_CHECK_SYSTEM.format(max_sentences=max_sentences)
    user_text = _SHAPE_CHECK_USER_TEMPLATE.format(
        max_sentences=max_sentences,
        exactly_one_question=str(exactly_one_question).lower(),
        hint_level=hint_level,
        hint_text=hint_text or "(none)",
        prior_questions=prior,
        draft=draft,
    )

    try:
        raw = _haiku_call(_cached_system_block(system_text), user_text)
    except Exception as e:
        return {
            "pass": True, "reason": "", "evidence": "",
            "checks": {}, "_elapsed_s": round(time.time() - t0, 3),
            "_raw": "", "_error": f"haiku_error: {type(e).__name__}",
        }
    elapsed = round(time.time() - t0, 3)
    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "pass": True, "reason": "json_parse_fail", "evidence": "",
            "checks": {}, "_elapsed_s": elapsed,
            "_raw": raw, "_error": "parse_fail",
        }
    passed = bool(parsed.get("pass"))
    reason = str(parsed.get("reason", "") or "")[:240]
    evidence = str(parsed.get("evidence", "") or "")[:240]
    checks = parsed.get("checks") if isinstance(parsed.get("checks"), dict) else {}
    error = ""
    if not passed and evidence and not _validate_evidence(evidence, draft):
        # Hallucinated evidence → downgrade to pass per the same rule
        # as haiku_hint_leak_check
        passed = True
        reason = ""
        evidence = ""
        error = "evidence_invalid"
    return {
        "pass": passed,
        "reason": reason,
        "evidence": evidence,
        "checks": checks,
        "_elapsed_s": elapsed,
        "_raw": raw,
        "_error": error,
    }


# ─────────────────────────────────────────────────────────────────────
#                  CLASSIFIER 5 — PEDAGOGY CHECK (L48 #4, L60)
# ─────────────────────────────────────────────────────────────────────

_PEDAGOGY_CHECK_SYSTEM = """\
You are a teaching-quality reviewer for a Socratic tutor. Your sole task
is to verify TWO pedagogy criteria from the EULER rubric:

  1. RELEVANCE — does the draft address content within the locked
                 subsection / locked anchor question scope? A draft that
                 wanders to a different topic, even an adjacent one,
                 fails this. Tangential tutoring on its own is fine
                 EXCEPT when the locked anchor is open and unanswered.

  2. HELPFUL   — does the draft advance reasoning toward the answer?
                 A draft that just restates the locked question without
                 introducing a new angle, hint, or scaffolding, fails.
                 A draft that introduces a leading sub-question or a
                 chunk-grounded fact-piece, passes.

Output STRICT JSON only — no markdown, no preamble:
{{
  "pass": true | false,
  "reason": "<short explanation if pass=false; empty if pass>",
  "evidence": "<verbatim substring of draft that triggered failure; empty if pass>",
  "checks": {{
    "relevance": true | false,
    "helpful": true | false
  }}
}}

The draft passes overall iff BOTH sub-checks pass.
"""


_PEDAGOGY_CHECK_USER_TEMPLATE = """\
LOCKED SUBSECTION: {locked_subsection}
LOCKED ANCHOR QUESTION: {locked_question}

DRAFT TO CHECK:
{draft}
"""


def haiku_pedagogy_check(
    draft: str,
    *,
    locked_subsection: str = "",
    locked_question: str = "",
) -> dict:
    """Asymmetric retry-on-pass wrapper around _haiku_pedagogy_check_once.

    Same pattern as shape/leak/sycophancy: retry on pass=True to catch
    Bedrock Haiku's ~30% false-negative rate on borderline cases.
    """
    first = _haiku_pedagogy_check_once(
        draft, locked_subsection=locked_subsection,
        locked_question=locked_question,
    )
    if not first.get("pass", True):
        first["_consensus"] = "first_fail"
        return first
    if first.get("_error"):
        first["_consensus"] = "first_pass_errored"
        return first
    second = _haiku_pedagogy_check_once(
        draft, locked_subsection=locked_subsection,
        locked_question=locked_question,
    )
    if not second.get("pass", True):
        second["_consensus"] = "split_first_pass_second_fail"
        return second
    first["_consensus"] = "both_pass"
    first["_elapsed_s"] = round(
        float(first.get("_elapsed_s") or 0.0) + float(second.get("_elapsed_s") or 0.0), 3,
    )
    return first


def _haiku_pedagogy_check_once(
    draft: str,
    *,
    locked_subsection: str = "",
    locked_question: str = "",
) -> dict:
    """L48 #4 + L60 — verifies EULER relevance + helpful in ONE Haiku call.

    Returns dict with universal L61 schema (pass/reason/evidence) plus
    a `checks` sub-dict.

    Safe defaults on error: pass=True (avoid false-firing).
    """
    t0 = time.time()
    if not draft:
        return {
            "pass": True, "reason": "empty draft", "evidence": "",
            "checks": {}, "_elapsed_s": 0.0, "_raw": "", "_error": "empty_draft",
        }

    user_text = _PEDAGOGY_CHECK_USER_TEMPLATE.format(
        locked_subsection=locked_subsection or "(unspecified)",
        locked_question=locked_question or "(unspecified)",
        draft=draft,
    )
    try:
        raw = _haiku_call(_cached_system_block(_PEDAGOGY_CHECK_SYSTEM), user_text)
    except Exception as e:
        return {
            "pass": True, "reason": "", "evidence": "",
            "checks": {}, "_elapsed_s": round(time.time() - t0, 3),
            "_raw": "", "_error": f"haiku_error: {type(e).__name__}",
        }
    elapsed = round(time.time() - t0, 3)
    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "pass": True, "reason": "json_parse_fail", "evidence": "",
            "checks": {}, "_elapsed_s": elapsed,
            "_raw": raw, "_error": "parse_fail",
        }
    passed = bool(parsed.get("pass"))
    reason = str(parsed.get("reason", "") or "")[:240]
    evidence = str(parsed.get("evidence", "") or "")[:240]
    checks = parsed.get("checks") if isinstance(parsed.get("checks"), dict) else {}
    error = ""
    if not passed and evidence and not _validate_evidence(evidence, draft):
        passed = True
        reason = ""
        evidence = ""
        error = "evidence_invalid"
    return {
        "pass": passed,
        "reason": reason,
        "evidence": evidence,
        "checks": checks,
        "_elapsed_s": elapsed,
        "_raw": raw,
        "_error": error,
    }


# ─────────────────────────────────────────────────────────────────────
#                  UNIVERSAL ADAPTER (L61)
# ─────────────────────────────────────────────────────────────────────


def to_universal_check_result(
    legacy_result: dict,
    *,
    check_name: str,
) -> dict:
    """Normalize any classifier output to the L61 universal schema.

    Per L61 every Haiku check (4 self-policing + 3 pre-flight) reports:
      {pass: bool, reason: str, evidence: str}
    The 3 pre-existing classifiers (haiku_hint_leak_check,
    haiku_sycophancy_check, haiku_off_domain_check) use a verdict-string
    schema instead. This adapter maps both shapes to the same canonical
    one so the L62 retry feedback loop can iterate uniformly.

    Adds `_check_name` so trace consumers know which check fired without
    having to inspect call shape.
    """
    # Already in the universal shape? (haiku_shape_check / haiku_pedagogy_check)
    if "pass" in legacy_result:
        return {
            "_check_name": check_name,
            "pass": bool(legacy_result.get("pass")),
            "reason": str(legacy_result.get("reason", "") or "")[:240],
            "evidence": str(legacy_result.get("evidence", "") or "")[:240],
            # Keep checks + diagnostics for downstream consumers
            "checks": legacy_result.get("checks") if isinstance(legacy_result.get("checks"), dict) else {},
            "_elapsed_s": legacy_result.get("_elapsed_s"),
            "_error": legacy_result.get("_error", ""),
        }

    # Verdict-string shape (haiku_hint_leak_check / sycophancy / off_domain)
    verdict = str(legacy_result.get("verdict", "") or "").lower()
    # Map per-check verdict → pass/fail
    fail_verdicts = {
        "haiku_leak_check":       {"leak"},
        "haiku_sycophancy_check": {"sycophantic"},
        # off_domain accepts the binary "off_domain" verdict (current impl)
        # AND the granular L56 set (future). Either signals fail.
        "haiku_off_domain_check": {"off_domain", "substance", "chitchat",
                                    "jailbreak", "answer_demand"},
    }
    fail_set = fail_verdicts.get(check_name, set())
    is_fail = verdict in fail_set
    rationale = str(legacy_result.get("rationale", "") or "")[:240]
    evidence = str(legacy_result.get("evidence", "") or "")[:240]
    return {
        "_check_name": check_name,
        "pass": not is_fail,
        "reason": rationale if is_fail else "",
        "evidence": evidence if is_fail else "",
        "_verdict": verdict,  # preserve original verdict string for trace
        "_elapsed_s": legacy_result.get("_elapsed_s"),
        "_error": legacy_result.get("_error", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# M7 — Unified intent classifier (replaces 3 separate Haiku calls)
# ─────────────────────────────────────────────────────────────────────────────

