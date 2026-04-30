"""
evaluation/quality/llm_judges.py
--------------------------------
Three-to-four batched LLM calls per session for the semantic judgments.

Calls:
  1. evaluate_per_turn(view) — EULER, RAGAS faithfulness/answer_relevancy,
     repetition, fabrication. Batches all tutor turns into one prompt.
  2. evaluate_retrieval(view) — RAGAS context_precision/recall/relevancy.
  3. evaluate_session_synthesis(view, det) — ARC.*, MSC.rationale_grounding,
     semantic-level penalty checks.
  4. evaluate_anchor_quality(view) — AQ.question_specificity, aliases_diversity.

All four return dicts with stable schemas. Failures (parse error, API error)
return None — the caller treats None as "skipped" and uses fallback values.

Cost discipline:
  - Haiku model (~$0.001/call) → ~$0.005 per session for all four.
  - System block per call is identical across sessions → prompt caching
    drops cost on subsequent sessions in the same eval run.
"""

from __future__ import annotations
import json
import os
import re
import time
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

from config import cfg
from .schema import SessionView, TutorTurn

load_dotenv()


# =============================================================================
# Client + model
# =============================================================================

def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _model() -> str:
    """Use cfg.models.evaluator. Override via SOKRATIC_EVAL_MODEL env var
    (e.g. for one-shot Sonnet adjudication passes)."""
    override = os.environ.get("SOKRATIC_EVAL_MODEL")
    if override:
        return override
    return getattr(cfg.models, "evaluator", None) or getattr(cfg.models, "dean", None) or "claude-haiku-4-5-20251001"


# =============================================================================
# JSON extraction (robust against markdown fences)
# =============================================================================

def _extract_json_object(text: str) -> Optional[dict]:
    """Find the first '{...}' block and parse. Tolerates ```json fences."""
    if not text:
        return None
    text = text.strip()
    # Drop ```json … ``` fences if present
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    # Find first '{' and last '}'
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        # Sometimes the model produces trailing commas or single quotes —
        # try a couple of cheap repairs.
        cleaned = re.sub(r",\s*([}\]])", r"\1", text[start:end + 1])
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


# =============================================================================
# Prompt loading
# =============================================================================

def _load_prompt(key: str) -> str:
    """Read a prompt template from config.cfg.eval_prompts (loaded from
    config/eval_prompts.yaml). Returns empty string if missing — callers
    should treat empty as 'skip this judge call'."""
    eval_prompts = getattr(cfg, "eval_prompts", None)
    if eval_prompts is None:
        return ""
    return getattr(eval_prompts, key, "") or ""


# =============================================================================
# Shared LLM call helper
# =============================================================================

def _call_llm(
    *, system_text: str, user_text: str, max_tokens: int = 2000,
    temperature: float = 0.0, label: str = "evaluator",
) -> tuple[Optional[dict], dict]:
    """Run one LLM call and parse the JSON response.

    Returns (parsed_json_or_None, telemetry_dict).
    Telemetry contains: elapsed_s, in_tok, out_tok, cost_estimate, error (if any).
    """
    client = _client()
    model = _model()

    # System block uses cache_control so subsequent sessions in the same
    # eval run hit the cache (~50% cost drop after first session).
    system_blocks = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    t0 = time.time()
    error: Optional[str] = None
    in_tok = out_tok = 0
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
        text = (resp.content[0].text if resp.content else "") or ""
        parsed = _extract_json_object(text)
        in_tok = getattr(resp.usage, "input_tokens", 0) or 0
        out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    except Exception as e:
        text = ""
        parsed = None
        error = f"{type(e).__name__}: {e}"

    elapsed = time.time() - t0

    telemetry = {
        "label": label,
        "model": model,
        "elapsed_s": round(elapsed, 3),
        "in_tok": in_tok,
        "out_tok": out_tok,
        "parse_ok": parsed is not None,
        "error": error,
        "raw_response_preview": (text[:200] + "...") if text and len(text) > 200 else text,
    }

    return parsed, telemetry


# =============================================================================
# CALL 1 — Per-turn quality
# =============================================================================

def evaluate_per_turn(view: SessionView) -> tuple[Optional[dict], dict]:
    """Score every tutor turn on EULER + RAGAS-per-turn + repetition + fabrication.
    Returns (result_or_None, telemetry).

    result schema:
      {"turns": [{turn_id, question_present, relevance, helpful, no_reveal,
                  faithfulness, answer_relevancy, repetition_to_prior,
                  fabrication_detected, fabrication_evidence, rationale}]}
    """
    static = _load_prompt("evaluator_per_turn_static")
    dynamic = _load_prompt("evaluator_per_turn_dynamic")
    if not static or not dynamic:
        return None, {"label": "per_turn", "error": "prompt_missing"}

    tutoring_turns = [t for t in view.turns if t.phase == "tutoring"]
    if not tutoring_turns:
        return {"turns": []}, {"label": "per_turn", "error": "no_tutoring_turns", "elapsed_s": 0}

    chunks_block = _format_chunks_block(view.retrieved_chunks, max_chunks=8)
    turns_block = _format_turns_block(tutoring_turns, max_prior=3)

    user_text = dynamic.format(
        locked_question=view.locked_question or "(none)",
        locked_answer=view.locked_answer or "(none)",
        locked_answer_aliases=", ".join(view.locked_answer_aliases) or "(none)",
        retrieved_chunks=chunks_block,
        turns_block=turns_block,
    )

    parsed, tele = _call_llm(
        system_text=static,
        user_text=user_text,
        max_tokens=2400,
        label="per_turn",
    )
    return parsed, tele


# =============================================================================
# CALL 2 — Retrieval quality
# =============================================================================

def evaluate_retrieval(view: SessionView) -> tuple[Optional[dict], dict]:
    """Score chunk-level relevance and session-level context_recall."""
    static = _load_prompt("evaluator_retrieval_static")
    dynamic = _load_prompt("evaluator_retrieval_dynamic")
    if not static or not dynamic:
        return None, {"label": "retrieval", "error": "prompt_missing"}

    if not view.retrieved_chunks:
        return None, {"label": "retrieval", "error": "no_chunks", "elapsed_s": 0}

    chunks_block = _format_chunks_block(view.retrieved_chunks, max_chunks=10, with_id=True)
    user_text = dynamic.format(
        locked_question=view.locked_question or "(none)",
        locked_answer=view.locked_answer or "(none)",
        chunks_block=chunks_block,
    )
    parsed, tele = _call_llm(
        system_text=static,
        user_text=user_text,
        max_tokens=1600,
        label="retrieval",
    )
    return parsed, tele


# =============================================================================
# CALL 3 — Session-level synthesis
# =============================================================================

def evaluate_session_synthesis(
    view: SessionView,
    deterministic_results: dict[str, Any],
) -> tuple[Optional[dict], dict]:
    """Session-level pedagogical integrity. Uses pre-computed intermediate_turns
    from the deterministic pass to focus the model on the at-risk turns."""
    static = _load_prompt("evaluator_session_synthesis_static")
    dynamic = _load_prompt("evaluator_session_synthesis_dynamic")
    if not static or not dynamic:
        return None, {"label": "synthesis", "error": "prompt_missing"}

    intermediate_turns = deterministic_results.get("intermediate_turns") or []
    help_abuse_triggered = view.help_abuse_count_max >= 2  # fixed threshold; see Change 3

    student_messages_block = "\n".join(
        f"[student msg {i}]: {(t.student_msg or '').strip()}"
        for i, t in enumerate(view.turns, 1)
        if t.student_msg
    )
    transcript_block = _format_full_transcript(view, max_turns=15)

    user_text = dynamic.format(
        locked_answer=view.locked_answer or "(none)",
        locked_answer_aliases=", ".join(view.locked_answer_aliases) or "(none)",
        student_reached_answer=str(view.final_student_reached_answer).lower(),
        help_abuse_triggered=str(help_abuse_triggered).lower(),
        session_mastery_score=("null" if view.mastery_score is None else f"{view.mastery_score:.3f}"),
        intermediate_turns=", ".join(str(x) for x in intermediate_turns) or "(none)",
        mastery_rationale=(view.mastery_rationale or "(not available)"),
        student_messages_block=student_messages_block or "(none)",
        transcript_block=transcript_block,
    )

    parsed, tele = _call_llm(
        system_text=static,
        user_text=user_text,
        max_tokens=1500,
        label="synthesis",
    )
    return parsed, tele


# =============================================================================
# CALL 4 — Anchor quality (cheap, optional)
# =============================================================================

def evaluate_anchor_quality(view: SessionView) -> tuple[Optional[dict], dict]:
    """Score locked_question specificity + aliases_diversity_semantic."""
    static = _load_prompt("evaluator_anchor_static")
    dynamic = _load_prompt("evaluator_anchor_dynamic")
    if not static or not dynamic:
        return None, {"label": "anchor", "error": "prompt_missing"}

    if not view.locked_question and not view.locked_answer:
        return None, {"label": "anchor", "error": "no_anchors"}

    user_text = dynamic.format(
        locked_question=view.locked_question or "(none)",
        locked_answer=view.locked_answer or "(none)",
        locked_answer_aliases=", ".join(view.locked_answer_aliases) or "(none)",
    )
    parsed, tele = _call_llm(
        system_text=static,
        user_text=user_text,
        max_tokens=600,
        label="anchor",
    )
    return parsed, tele


# =============================================================================
# Helpers — text block formatters
# =============================================================================

def _format_chunks_block(chunks: list[dict], *, max_chunks: int = 8, with_id: bool = False) -> str:
    if not chunks:
        return "(no chunks retrieved)"
    out = []
    for i, c in enumerate(chunks[:max_chunks]):
        text = str((c or {}).get("text") or "").strip()
        score = (c or {}).get("score")
        sub = (c or {}).get("subsection_title") or (c or {}).get("subsection") or ""
        prefix = f"[id={i}] " if with_id else f"[{i + 1}] "
        score_str = f" (score={score})" if score is not None else ""
        sub_str = f" [{sub}]" if sub else ""
        out.append(f"{prefix}{score_str}{sub_str}\n{text[:400]}\n")
    if len(chunks) > max_chunks:
        out.append(f"...({len(chunks) - max_chunks} more chunks omitted)")
    return "\n".join(out)


def _format_turns_block(tutoring_turns: list[TutorTurn], *, max_prior: int = 3) -> str:
    """Render every tutoring turn with its student msg, tutor msg, and the
    last `max_prior` tutor messages (so the evaluator can score repetition)."""
    out = []
    for i, t in enumerate(tutoring_turns):
        prior = tutoring_turns[max(0, i - max_prior):i]
        prior_block = "\n".join(
            f"  [prior tutor msg {j + 1}]: {p.tutor_msg[:200]}"
            for j, p in enumerate(prior)
        ) or "  (none)"
        out.append(
            f"=== turn_id={t.turn_id} (phase={t.phase}, hint_level={t.hint_level}, "
            f"student_state={t.student_state}, reached={t.student_reached_answer}) ===\n"
            f"student_msg: {t.student_msg}\n"
            f"tutor_msg: {t.tutor_msg}\n"
            f"prior tutor messages:\n{prior_block}"
        )
    return "\n\n".join(out)


def _format_full_transcript(view: SessionView, *, max_turns: int = 15) -> str:
    out = []
    if view.rapport_message:
        out.append(f"[rapport tutor]: {view.rapport_message[:300]}")
    for t in view.turns[:max_turns]:
        if t.student_msg:
            out.append(f"[student turn {t.turn_id}]: {t.student_msg}")
        if t.tutor_msg:
            out.append(
                f"[tutor turn {t.turn_id} | state={t.student_state}, reached={t.student_reached_answer}]: "
                f"{t.tutor_msg}"
            )
    return "\n".join(out)
