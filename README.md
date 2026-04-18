# Sokratic-OT

Sokratic-OT is an OT anatomy tutoring system with a Dean-Teacher architecture.
The tutor uses Socratic questioning, confidence-aware gating, and an optional clinical reasoning stage.

## Current Status (Milestone 2)
- RAG pipeline integrated (dense+sparse+RRF+cross-encoder)
- Dean quality gate + leak guard in loop
- Topic scoping gate before tutoring starts
- Confidence-aware correctness gating
- Clinical application stage with multi-turn support (max 3)
- Export pipeline includes evaluator-ready metadata for EULER/RAGAS-style judging

See full architecture and roadmap in:
- [ARCHITECTURE.md](/Users/arun-ghontale/UB/NLP/sokratic/ARCHITECTURE.md)

## Quick Start
```bash
pip install -r requirements.txt
streamlit run ui/app.py
```

## Core Flow
`rapport -> topic scoping -> tutoring -> assessment (clinical opt-in) -> memory_update`

## Retrieval Stack
1. Qdrant dense search
2. BM25 sparse search (preprocessed query)
3. RRF merge
4. Parent chunk expansion
5. Cross-encoder rerank
6. Top-7 contexts to Dean/Teacher

## Key Results Snapshot
From `data/eval/eval_results_2026_04_17.json`:
- Hit@1: 0.57
- Hit@3: 0.64
- Hit@7: 0.71
- MRR: 0.616
- no_result: 22/100

Baseline (same file):
- Hit@5: 0.52
- MRR: 0.493
- no_result: 42/100

## Data and Artifacts
- RAG eval data: `data/eval/`
- Conversation and quality artifacts: `data/artifacts/`
- Handoff package (snapshot + processed data): `sokratic_handoff_nidhi/sokratic_handoff/`

## Important Notes
- Persistent memory quality depends on Qdrant memory backend availability.
- UI polish and clinical-stage conversational continuity are still active work items.

## Submission Draft Support
Use:
- [MILESTONE2_ACL_REPORT_DRAFT.md](/Users/arun-ghontale/UB/NLP/sokratic/MILESTONE2_ACL_REPORT_DRAFT.md)

This file is designed to be converted into the final ACL-format milestone report.
