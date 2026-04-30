"""
evaluation/quality/runner.py
----------------------------
Pipeline orchestrator for the conversation quality scorer.

Pipeline:
  1. load_session(path) → SessionView (handles both input formats)
  2. deterministic.compute_all(view) → flat dict of det_* sub-metrics
  3. llm_judges.evaluate_* (4 batched calls) → dicts of semantic judgments
  4. primary.assemble_euler / assemble_ragas → headline metrics
  5. dimensions.assemble_dimensions → 10 secondary dims
  6. penalties.compute_penalties → list of Critical/Major flags
  7. penalties.compute_verdict → final verdict string
  8. assemble report → return dict per docs/EVALUATION_FRAMEWORK.md §6

Usage:
    from evaluation.quality.runner import evaluate_session
    report = evaluate_session("path/to/session.json")
    # or with dict input:
    report = evaluate_session(session_view, run_llm_calls=False)
"""

from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from .schema import SessionView, load_session
from . import deterministic
from . import llm_judges
from . import primary
from . import dimensions
from . import penalties as penalties_mod


def evaluate_session(
    source: Union[str, Path, SessionView, dict],
    *,
    run_llm_calls: bool = True,
    skip_anchor_call: bool = False,
) -> dict[str, Any]:
    """Evaluate a single session.

    Args:
      source: file path (str/Path) OR a pre-loaded SessionView OR a raw dict.
      run_llm_calls: if False, skip all 4 LLM calls (deterministic-only mode,
        useful for quick smoke checks during development).
      skip_anchor_call: if True, skip the cheapest call (#4 anchor quality).

    Returns the structured report dict described in EVALUATION_FRAMEWORK.md §6.
    """
    # ---- Step 1: load
    if isinstance(source, SessionView):
        view = source
    elif isinstance(source, (str, Path)):
        view = load_session(source)
    elif isinstance(source, dict):
        # Treat as production state dump
        from .schema import _view_from_state
        view = _view_from_state(source)
    else:
        raise TypeError(f"Unsupported source type: {type(source)}")

    # ---- Step 2: deterministic
    det = deterministic.compute_all(view)

    # ---- Step 3: LLM batched calls
    llm_telemetry: list[dict] = []
    llm_per_turn = None
    llm_retrieval = None
    llm_synthesis = None
    llm_anchor = None

    if run_llm_calls:
        # Call 1
        llm_per_turn, t1 = llm_judges.evaluate_per_turn(view)
        llm_telemetry.append(t1)
        # Call 2
        llm_retrieval, t2 = llm_judges.evaluate_retrieval(view)
        llm_telemetry.append(t2)
        # Call 3
        llm_synthesis, t3 = llm_judges.evaluate_session_synthesis(view, det)
        llm_telemetry.append(t3)
        # Call 4 (optional)
        if not skip_anchor_call:
            llm_anchor, t4 = llm_judges.evaluate_anchor_quality(view)
            llm_telemetry.append(t4)

    # ---- Step 4: primary metrics (EULER + RAGAS)
    euler = primary.assemble_euler(llm_per_turn)
    ragas = primary.assemble_ragas(
        llm_per_turn, llm_retrieval, view.final_student_reached_answer
    )
    primary_block = {"EULER": euler, "RAGAS": ragas}

    # ---- Step 5: secondary dimensions
    dims = dimensions.assemble_dimensions(
        view=view,
        deterministic_results=det,
        llm_per_turn=llm_per_turn,
        llm_retrieval=llm_retrieval,
        llm_synthesis=llm_synthesis,
        llm_anchor=llm_anchor,
        primary=primary_block,
    )

    # ---- Step 6: penalties
    pens = penalties_mod.compute_penalties(view, det, llm_per_turn, llm_synthesis)

    # ---- Step 7: verdict
    verdict = penalties_mod.compute_verdict(primary_block, dims, pens)

    # ---- Step 8: assemble report
    report = {
        "session_id": view.session_id,
        "test_id": view.test_id,
        "description": view.description,
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),

        "primary": primary_block,
        "secondary": dims,
        "penalties": pens,
        "verdict": verdict,

        "raw_signals": {
            "turn_count": view.final_turn_count,
            "max_turns": view.max_turns,
            "final_phase": view.final_phase,
            "final_hint_level": view.final_hint_level,
            "max_hints": view.max_hints,
            "final_student_reached_answer": view.final_student_reached_answer,
            "interventions": view.interventions,
            "coverage_gap_events": view.coverage_gap_events,
            "retrieval_calls": view.retrieval_calls,
            "cost_usd": view.cost_usd,
            "api_calls": view.api_calls,
            "input_tokens": view.input_tokens,
            "output_tokens": view.output_tokens,
            "n_tutoring_turns": det.get("det_trq_n_tutoring_turns", 0),
            "n_intermediate_turns": det.get("det_arc_n_intermediate_turns", 0),
            "n_fabrication_turns": det.get("det_arc_n_fabrication_turns", 0),
        },

        "deterministic_signals": det,  # full flat dict, useful for debugging
        "llm_telemetry": llm_telemetry,

        "anchors_inspected": {
            "locked_question": view.locked_question,
            "locked_answer": view.locked_answer,
            "locked_answer_aliases": view.locked_answer_aliases,
            "locked_topic": view.locked_topic,
        },

        "expectations": (
            None if view.expected_reached is None
            else {
                "reached": view.expected_reached,
                "no_fabrication_keywords": view.expected_no_fabrication_keywords,
            }
        ),
    }

    return report


def save_report(report: dict, out_path: Union[str, Path]) -> Path:
    """Write a report dict to disk as JSON, returning the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    return out_path


def short_summary(report: dict) -> str:
    """Render a compact human-readable summary line for a report.
    Used by the dashboard aggregator and the CLI."""
    primary_block = report.get("primary") or {}
    eu = primary_block.get("EULER") or {}
    ra = primary_block.get("RAGAS") or {}
    secondary_block = report.get("secondary") or {}
    pens = report.get("penalties") or []
    crit = sum(1 for p in pens if p.get("severity") == "critical")
    major = sum(1 for p in pens if p.get("severity") == "major")

    def fmt(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "—"

    failed_dims = [n for n, d in secondary_block.items() if d.get("passes") is False]
    return (
        f"verdict={report.get('verdict'):20s} | "
        f"EULER={fmt(eu.get('relevance'))}/{fmt(eu.get('helpful'))}/{fmt(eu.get('no_reveal'))} | "
        f"RAGAS_faith={fmt(ra.get('faithfulness'))} ctx_prec={fmt(ra.get('context_precision'))} | "
        f"crit={crit} major={major} | "
        f"failed_dims={failed_dims}"
    )
