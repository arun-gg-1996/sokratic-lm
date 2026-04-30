"""
evaluation/quality/primary.py
-----------------------------
Assemble PRIMARY (headline) metrics: EULER + RAGAS.

Reads outputs from:
  - llm_judges.evaluate_per_turn (per-turn EULER + RAGAS faithfulness/answer_relevancy)
  - llm_judges.evaluate_retrieval (RAGAS context_precision, context_recall, context_relevancy)

Produces a structured dict per the schema in docs/EVALUATION_FRAMEWORK.md §6.
"""

from __future__ import annotations
from typing import Any, Optional

# Pass thresholds (per docs/EVALUATION_FRAMEWORK.md §3.1, §3.2).
EULER_THRESHOLDS = {
    "question_present": 0.95,
    "relevance": 0.70,
    "helpful": 0.70,
    "no_reveal": 0.95,
}

RAGAS_THRESHOLDS = {
    "context_precision": 0.80,
    "context_recall": 0.70,
    "faithfulness": 0.85,
    "answer_relevancy": 0.75,
}


def _safe_mean(values: list[float]) -> Optional[float]:
    vs = [v for v in values if v is not None]
    return (sum(vs) / len(vs)) if vs else None


def assemble_euler(per_turn_result: Optional[dict]) -> dict[str, Any]:
    """Mean each criterion across tutoring turns. Pass = all four ≥ threshold."""
    out = {
        "question_present": None,
        "relevance": None,
        "helpful": None,
        "no_reveal": None,
        "passes_all_thresholds": None,
        "n_turns_scored": 0,
    }
    if not per_turn_result or not per_turn_result.get("turns"):
        return out

    turns = per_turn_result["turns"]
    out["n_turns_scored"] = len(turns)
    for crit in ("question_present", "relevance", "helpful", "no_reveal"):
        out[crit] = _safe_mean([float(t.get(crit, 0.0) or 0.0) for t in turns])

    if all(out[c] is not None for c in EULER_THRESHOLDS):
        out["passes_all_thresholds"] = all(
            out[c] >= EULER_THRESHOLDS[c] for c in EULER_THRESHOLDS
        )
    return out


def assemble_ragas(
    per_turn_result: Optional[dict],
    retrieval_result: Optional[dict],
    final_reached: bool,
) -> dict[str, Any]:
    """RAGAS metrics:
      - context_precision: from retrieval call
      - context_recall: from retrieval call
      - context_relevancy: mean of per-chunk relevance scores from retrieval call
      - faithfulness: mean across tutoring turns from per_turn call
      - answer_relevancy: mean across tutoring turns from per_turn call
      - answer_correctness: 1.0 if final_reached AND gate said reached; else
        we leave None (we'd need a separate LLM check on the final student
        utterance to give a non-binary score; deferred).
    """
    out = {
        "context_precision": None,
        "context_recall": None,
        "context_relevancy": None,
        "faithfulness": None,
        "answer_relevancy": None,
        "answer_correctness": None,
        "passes_all_thresholds": None,
    }

    if retrieval_result:
        cp = retrieval_result.get("context_precision")
        cr = retrieval_result.get("context_recall")
        per_chunk = retrieval_result.get("per_chunk") or []
        out["context_precision"] = float(cp) if cp is not None else None
        out["context_recall"] = float(cr) if cr is not None else None
        if per_chunk:
            out["context_relevancy"] = _safe_mean(
                [float(c.get("relevance", 0.0) or 0.0) for c in per_chunk]
            )

    if per_turn_result and per_turn_result.get("turns"):
        turns = per_turn_result["turns"]
        out["faithfulness"] = _safe_mean(
            [float(t.get("faithfulness", 0.0) or 0.0) for t in turns]
        )
        out["answer_relevancy"] = _safe_mean(
            [float(t.get("answer_relevancy", 0.0) or 0.0) for t in turns]
        )

    # Threshold check (if all four are present)
    relevant_keys = ["context_precision", "context_recall", "faithfulness", "answer_relevancy"]
    if all(out[k] is not None for k in relevant_keys):
        out["passes_all_thresholds"] = all(
            out[k] >= RAGAS_THRESHOLDS[k] for k in relevant_keys
        )

    # Heuristic answer_correctness: 1.0 if reached, 0.0 if not.
    # A future Sonnet-based adjudication call could compute a graded score.
    out["answer_correctness"] = 1.0 if final_reached else 0.0

    return out
