"""
scripts/score_conversation_quality.py
-------------------------------------
CLI entry point for the conversation quality scorer.

Reads a saved session JSON, computes the full evaluation (primary EULER +
RAGAS, 10 secondary dimensions, penalties), and writes a structured report
to disk.

Usage:
    .venv/bin/python scripts/score_conversation_quality.py SESSION_JSON [-o OUT_JSON]
                       [--no-llm]
                       [--skip-anchor]

Examples:
    # Score one test-harness session, write to data/artifacts/eval/
    .venv/bin/python scripts/score_conversation_quality.py \\
        data/artifacts/gate_e2e/T1_wrong_answer_*.json

    # Deterministic only (no API costs) — useful for fast iteration:
    .venv/bin/python scripts/score_conversation_quality.py SESSION.json --no-llm

    # Score every gate test JSON in one go:
    for f in data/artifacts/gate_e2e/T*.json; do
        .venv/bin/python scripts/score_conversation_quality.py "$f"
    done
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.quality import runner  # noqa: E402


DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent / "data" / "artifacts" / "eval"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_json", help="Path to a saved session JSON.")
    parser.add_argument("-o", "--output", help="Output JSON path. Defaults to data/artifacts/eval/{stem}_eval.json")
    parser.add_argument("--no-llm", action="store_true", help="Deterministic-only mode (skip all 4 LLM calls)")
    parser.add_argument("--skip-anchor", action="store_true", help="Skip the anchor-quality LLM call (cheapest)")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-section verbose printout; show only summary line")
    args = parser.parse_args()

    in_path = Path(args.session_json)
    if not in_path.exists():
        print(f"ERROR: not found: {in_path}", file=sys.stderr)
        return 2

    print(f"Scoring session: {in_path}")
    if args.no_llm:
        print("  Mode: DETERMINISTIC ONLY (no LLM calls)")

    report = runner.evaluate_session(
        in_path,
        run_llm_calls=not args.no_llm,
        skip_anchor_call=args.skip_anchor,
    )

    # Decide output path
    if args.output:
        out_path = Path(args.output)
    else:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_OUTPUT_DIR / f"{in_path.stem}_eval.json"

    runner.save_report(report, out_path)
    print(f"  → {out_path}")

    # ----- Console summary
    if not args.quiet:
        _print_full_summary(report)
    print(runner.short_summary(report))

    # Exit code: 0 = passed/warning, 1 = failed_critical_penalty, 2 = failed_threshold
    verdict = report.get("verdict")
    if verdict == "failed_critical_penalty":
        return 1
    if verdict == "failed_threshold":
        return 2
    return 0


def _print_full_summary(report: dict) -> None:
    print()
    print("=" * 72)
    print(f"  Session: {report.get('session_id')} | test={report.get('test_id') or '(prod)'}")
    print(f"  Verdict: {report.get('verdict')}")
    print("=" * 72)

    primary = report.get("primary") or {}
    print("\n  PRIMARY METRICS")
    eu = primary.get("EULER") or {}
    ra = primary.get("RAGAS") or {}
    print(f"    EULER (n_turns={eu.get('n_turns_scored', 0)})")
    for k in ("question_present", "relevance", "helpful", "no_reveal"):
        v = eu.get(k)
        print(f"      {k:18s}: {_fmt(v)}")
    print(f"      passes_all_thresholds: {eu.get('passes_all_thresholds')}")
    print(f"    RAGAS")
    for k in ("context_precision", "context_recall", "context_relevancy",
              "faithfulness", "answer_relevancy", "answer_correctness"):
        v = ra.get(k)
        print(f"      {k:18s}: {_fmt(v)}")
    print(f"      passes_all_thresholds: {ra.get('passes_all_thresholds')}")

    print("\n  SECONDARY DIMENSIONS")
    secondary = report.get("secondary") or {}
    for name in ("TLQ", "RRQ", "AQ", "TRQ", "RGC", "PP", "ARC", "CC", "CE", "MSC"):
        d = secondary.get(name) or {}
        score = d.get("score")
        thresh = d.get("threshold")
        passes = "✓" if d.get("passes") else ("✗" if d.get("passes") is False else "?")
        print(f"    {name:5s} {passes}  score={_fmt(score)}  threshold={_fmt(thresh)}")

    pens = report.get("penalties") or []
    if pens:
        print(f"\n  PENALTIES ({len(pens)})")
        for p in pens:
            print(f"    [{p.get('severity'):8s}] {p.get('code')}")
            print(f"             {p.get('evidence', '')[:240]}")
    else:
        print("\n  PENALTIES: none triggered ✓")

    raw = report.get("raw_signals") or {}
    print(f"\n  RAW SIGNALS")
    print(f"    turn_count: {raw.get('turn_count')}/{raw.get('max_turns')}")
    print(f"    final_phase: {raw.get('final_phase')}")
    print(f"    reached_answer: {raw.get('final_student_reached_answer')}")
    print(f"    cost_usd: ${_fmt(raw.get('cost_usd'))}  api_calls: {raw.get('api_calls')}")
    print(f"    intervention rate: {raw.get('interventions')}/{raw.get('n_tutoring_turns')}")
    print()


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:.3f}" if isinstance(v, float) else str(v)
    return str(v)


if __name__ == "__main__":
    sys.exit(main())
