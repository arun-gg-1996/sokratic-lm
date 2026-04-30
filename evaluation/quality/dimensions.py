"""
evaluation/quality/dimensions.py
--------------------------------
Assemble the 10 SECONDARY (diagnostic) dimensions from the deterministic
results + LLM judge outputs.

Each dimension produces:
  {"score": 0.0-1.0, "sub": {key: value, ...}}

A dimension's `score` is the mean of its non-null sub-metrics. Missing
sub-metrics are skipped (never zeroed) — so partial signals don't drag the
score down inappropriately.

See docs/EVALUATION_FRAMEWORK.md §2 for the definitions.
"""

from __future__ import annotations
from typing import Any, Optional

from .schema import SessionView


# =============================================================================
# Per-dimension thresholds (docs/EVALUATION_FRAMEWORK.md §8).
# A dimension "passes" if score >= threshold.
# =============================================================================

DIMENSION_THRESHOLDS = {
    "TLQ": 0.80,
    "RRQ": 0.75,
    "AQ": 0.85,
    "TRQ": 0.75,
    "RGC": 0.95,
    "PP": 0.60,
    "ARC": 0.70,
    "CC": 1.00,
    "CE": 0.50,
    "MSC": 0.70,
}


def _mean_present(values: list) -> Optional[float]:
    """Mean of non-None values, or None if all are missing."""
    vs = [float(v) for v in values if v is not None]
    return sum(vs) / len(vs) if vs else None


def _round_or_none(x, digits: int = 3):
    return round(x, digits) if x is not None else None


def assemble_dimensions(
    view: SessionView,
    deterministic_results: dict[str, Any],
    llm_per_turn: Optional[dict],
    llm_retrieval: Optional[dict],
    llm_synthesis: Optional[dict],
    llm_anchor: Optional[dict],
    primary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return {dimension_name: {score, sub, threshold, passes}}."""
    det = deterministic_results

    out: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------- TLQ
    tlq_sub = {
        "match_top_score": det.get("det_tlq_match_top_score"),
        "vote_consensus": det.get("det_tlq_vote_consensus"),
        "coverage_gate_pass": det.get("det_tlq_coverage_gate_pass"),
        "anchor_extraction_success": det.get("det_tlq_anchor_extraction_success"),
        "repair_clean": det.get("det_tlq_repair_clean"),
    }
    out["TLQ"] = _wrap("TLQ", tlq_sub)

    # ---------------------------------------------------------- RRQ
    rrq_sub = {
        "chunk_count_adequacy": det.get("det_rrq_chunk_count_adequacy"),
        "mean_top3_score": det.get("det_rrq_mean_top3_score"),
        "section_match_rate": det.get("det_rrq_section_match_rate"),
        "retrieval_calls_efficiency": det.get("det_rrq_retrieval_calls_efficiency"),
        "coverage_gap_frequency": det.get("det_rrq_coverage_gap_frequency"),
    }
    # Bring in RAGAS context_precision + context_relevancy as additional signals
    if primary.get("RAGAS"):
        rrq_sub["ragas_context_precision"] = primary["RAGAS"].get("context_precision")
        rrq_sub["ragas_context_relevancy"] = primary["RAGAS"].get("context_relevancy")
    out["RRQ"] = _wrap("RRQ", rrq_sub)

    # ---------------------------------------------------------- AQ
    aq_sub = {
        "groundedness": det.get("det_aq_groundedness"),
        "answer_brevity": det.get("det_aq_answer_brevity"),
        "answer_is_noun_phrase": det.get("det_aq_answer_is_noun_phrase"),
        "aliases_count_adequacy": det.get("det_aq_aliases_count_adequacy"),
    }
    if llm_anchor:
        aq_sub["question_specificity"] = llm_anchor.get("question_specificity")
        aq_sub["aliases_diversity_semantic"] = llm_anchor.get("aliases_diversity_semantic")
    out["AQ"] = _wrap("AQ", aq_sub)

    # ---------------------------------------------------------- TRQ
    trq_sub: dict[str, Any] = {
        "qc_pass_rate": det.get("det_trq_qc_pass_rate"),
        "revised_draft_rate": det.get("det_trq_revised_draft_rate"),
        "intervention_rate": det.get("det_trq_intervention_rate"),
        "reason_codes_clean_rate": det.get("det_trq_reason_codes_clean_rate"),
    }
    # Pull EULER mean scores so TRQ reflects them too (cross-link to primary)
    if primary.get("EULER"):
        e = primary["EULER"]
        trq_sub["euler_question_present"] = e.get("question_present")
        trq_sub["euler_relevance"] = e.get("relevance")
        trq_sub["euler_helpful"] = e.get("helpful")
        trq_sub["euler_no_reveal"] = e.get("no_reveal")
    # Repetition score: mean across turns from llm_per_turn
    if llm_per_turn and llm_per_turn.get("turns"):
        rep = _mean_present(
            [t.get("repetition_to_prior") for t in llm_per_turn["turns"]]
        )
        trq_sub["repetition_score"] = rep
    out["TRQ"] = _wrap("TRQ", trq_sub)

    # ---------------------------------------------------------- RGC
    rgc_sub = {
        # invert false_positive_rate so higher score = better
        "no_false_positives": (
            None if det.get("det_rgc_n_decisions", 0) == 0
            else 1.0 - float(det.get("det_rgc_false_positive_rate") or 0.0)
        ),
        "no_false_negatives": (
            None if det.get("det_rgc_n_decisions", 0) == 0
            else 1.0 - float(det.get("det_rgc_false_negative_rate") or 0.0)
        ),
        "evidence_quote_validity": det.get("det_rgc_evidence_quote_validity"),
    }
    out["RGC"] = _wrap("RGC", rgc_sub)
    # Attach diagnostic histogram (not in score)
    out["RGC"]["sub"]["path_distribution"] = det.get("det_rgc_path_distribution") or {}
    out["RGC"]["sub"]["confusion_matrix"] = {
        "tp": det.get("det_rgc_tp", 0),
        "fp": det.get("det_rgc_fp", 0),
        "tn": det.get("det_rgc_tn", 0),
        "fn": det.get("det_rgc_fn", 0),
    }

    # ---------------------------------------------------------- PP
    pp_sub = {
        "hint_progression_monotonic": det.get("det_pp_hint_progression_monotonic"),
        "hint_utilization": det.get("det_pp_hint_utilization"),
        "student_engagement_rate": det.get("det_pp_student_engagement_rate"),
        "state_trajectory_score": det.get("det_pp_state_trajectory_score"),
    }
    out["PP"] = _wrap("PP", pp_sub)

    # ---------------------------------------------------------- ARC
    arc_sub: dict[str, Any] = {
        "step_vs_reach_disambiguation_det": det.get("det_arc_step_vs_reach_disambiguation"),
        "intermediate_credit_avoidance_det": det.get("det_arc_intermediate_credit_avoidance"),
    }
    if llm_synthesis:
        arc_sub["mastery_attribution_grounding"] = llm_synthesis.get("mastery_attribution_grounding")
        arc_sub["step_vs_reach_disambiguation_llm"] = llm_synthesis.get("step_vs_reach_disambiguation")
        arc_sub["intermediate_credit_avoidance_llm"] = llm_synthesis.get("intermediate_credit_avoidance")
        arc_sub["off_topic_drift_score"] = llm_synthesis.get("off_topic_drift_score")
    out["ARC"] = _wrap("ARC", arc_sub)
    # Attach diagnostic info
    if llm_synthesis:
        out["ARC"]["sub"]["mastery_attribution_evidence"] = llm_synthesis.get("mastery_attribution_evidence") or []
    out["ARC"]["sub"]["n_intermediate_turns"] = det.get("det_arc_n_intermediate_turns", 0)
    out["ARC"]["sub"]["n_fabrication_turns"] = det.get("det_arc_n_fabrication_turns", 0)

    # ---------------------------------------------------------- CC
    cc_sub = {
        "phase_transitions_legal": det.get("det_cc_phase_transitions_legal"),
        "topic_stability": det.get("det_cc_topic_stability"),
        "anchor_stability": det.get("det_cc_anchor_stability"),
        "invariant_score": det.get("det_cc_invariant_score"),
    }
    out["CC"] = _wrap("CC", cc_sub)
    out["CC"]["sub"]["invariant_violations_count"] = det.get("det_cc_invariant_violations_count", 0)

    # ---------------------------------------------------------- CE
    ce_sub: dict[str, Any] = {
        "cost_score": det.get("det_ce_cost_score"),
        "cache_hit_rate": det.get("det_ce_cache_hit_rate"),
    }
    if det.get("det_ce_latency_score") is not None:
        ce_sub["latency_score"] = det.get("det_ce_latency_score")
    out["CE"] = _wrap("CE", ce_sub)
    out["CE"]["sub"]["cost_per_useful_turn"] = det.get("det_ce_cost_per_useful_turn")

    # ---------------------------------------------------------- MSC
    msc_sub: dict[str, Any] = {
        "confidence_appropriateness": det.get("det_msc_confidence_appropriateness"),
        "ewma_movement_score": det.get("det_msc_ewma_movement_score"),
    }
    if llm_synthesis is not None:
        msc_sub["rationale_grounding_llm"] = llm_synthesis.get("mastery_attribution_grounding")
    out["MSC"] = _wrap("MSC", msc_sub)

    return out


def _wrap(dim_name: str, sub: dict) -> dict:
    """Compute mean of present sub-metrics. Round score to 3 places."""
    score_inputs = [v for v in sub.values() if isinstance(v, (int, float))]
    score = sum(score_inputs) / len(score_inputs) if score_inputs else None
    threshold = DIMENSION_THRESHOLDS.get(dim_name, 0.50)
    passes = (score is not None and score >= threshold)

    return {
        "score": _round_or_none(score, 3),
        "threshold": threshold,
        "passes": passes,
        "sub": {k: _round_or_none(v, 3) if isinstance(v, float) else v for k, v in sub.items()},
    }
