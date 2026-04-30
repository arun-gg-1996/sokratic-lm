"""
evaluation/quality/
-------------------
Sokratic conversation quality scorer (designed 2026-04-29).

Produces a structured per-session report with:
  - primary metrics (EULER, RAGAS) — headline / thesis-reported
  - secondary metrics (10 internal dimensions) — diagnostic / flow-specific
  - penalties (Critical / Major) — separate channel for hard fails

See docs/EVALUATION_FRAMEWORK.md for the full spec.

Usage:
    from evaluation.quality.runner import evaluate_session
    report = evaluate_session(session_data)

Or via CLI:
    python scripts/score_conversation_quality.py path/to/session.json

Module layout:
    deterministic.py — pure-Python computations from trace fields (free)
    llm_judges.py    — 3-4 batched LLM calls (Haiku); semantic judgments
    primary.py       — assemble EULER + RAGAS from llm + det results
    dimensions.py    — assemble 10 secondary dims from llm + det
    penalties.py     — Critical / Major penalty checks
    runner.py        — orchestrate the pipeline
    schema.py        — TypedDicts and dataclasses for inputs/outputs
"""
