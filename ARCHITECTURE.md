# Sokratic-OT Architecture (Current State)

Last updated: 2026-04-18

## 1) System Goal
Sokratic-OT is a Socratic tutor for Occupational Therapy (OT) anatomy learning. The system is designed to avoid direct answer reveal during tutoring, guide the learner with hint progression, and then evaluate application through a short clinical reasoning stage.

## 2) Current End-to-End Flow
1. `rapport_node`
   - Teacher opens session with OT-student-focused greeting.
2. Topic scoping gate (pre-tutoring)
   - Dean + Teacher require explicit topic narrowing via 3-4 scoped options (card-based UI path).
   - Tutoring does not start until a topic is selected.
3. `dean_node` tutoring loop
   - Dean setup: retrieval, answer lock, student state classification.
   - Teacher draft: Socratic question generation.
   - Dean quality gate: EULER-like constraints + leak guard.
   - If QC fails: revised draft applied; fallback only when needed.
   - Hint logic + low-effort gating + confidence-aware correctness gating.
4. `assessment_node`
   - If core answer reached: clinical opt-in -> clinical question -> up to `clinical_max_turns` (currently 3).
   - If answer not reached: reveal path + weak-topic update.
5. `memory_update_node`
   - Session summary and weak-topic updates are flushed (persistence active once Qdrant memory backend is available).

## 3) Agent Roles
### Teacher
- Generates Socratic responses and clinical prompts.
- Does not own final correctness decisions.
- Avoids direct answer reveal behavior by prompt contract.

### Dean
- Owns retrieval setup, locked answer, student-state classification, confidence scoring, and quality gating.
- Runs deterministic checks and LLM QC.
- Applies revised drafts and fallback only when required.

## 4) Retrieval and Knowledge Stack (RAG)
### Data assets (handoff package integrated)
- `data/processed/chunks_ot.jsonl`: 7,801 chunks
- `data/processed/propositions_ot.jsonl`: 40,614 propositions
- `data/processed/raw_elements_ot.jsonl`: 5,231 elements
- `data/processed/raw_sections_ot.jsonl`: 574 sections
- `data/indexes/bm25_ot.pkl`: BM25 index
- Qdrant snapshot for `sokratic_kb` (40,614 vectors)

### Retrieval pipeline
1. Dense search (Qdrant)
2. Sparse search (BM25 with preprocessing)
3. Reciprocal Rank Fusion (`rrf_k=60`)
4. Parent-chunk expansion
5. Cross-encoder rerank (`cross-encoder/ms-marco-MiniLM-L-6-v2`)
6. Top-k final chunk return (`top_chunks_final=7`)

## 5) Prompt Caching Strategy (Current)
- Caching is explicit via Anthropic `cache_control` blocks.
- Static instruction + retrieved chunks are merged into one cached block.
- Dynamic state/history are separated into non-cached blocks.
- Structure is model-agnostic at architecture level (same prompt-block design path).

## 6) Confidence and Mastery Logic
### Core tutoring
- Dean classifies student state: `correct|partial_correct|incorrect|question|irrelevant|low_effort`.
- Confidence score is tracked per turn and as running session mastery confidence.
- `student_reached_answer` requires correct classification + confidence threshold.

### Clinical stage
- Clinical reasoning is evaluated turn-by-turn (max 3 turns currently).
- Clinical confidence threshold determines pass.

### Mastery tiers
- `core_mastery_tier`, `clinical_mastery_tier`, and `mastery_tier` are tracked.
- Final mastery is based on both core and clinical evidence.

## 7) Evaluation and Exportability
The export pipeline supports downstream LLM judging and report generation:
- Turn-level records with:
  - student input
  - tutor output
  - locked answer
  - state labels
  - hint level
  - retrieved contexts
  - dean-teacher internal trace
  - per-message metrics (tokens/cost/cache where logged)
- RAGAS aliases: `user_input`, `response`, `contexts`, `reference`
- EULER aliases: `student_message`, `tutor_response`, `locked_answer`, `phase`
- Coverage flags for evaluator-readiness
- Built-in conversation-scoring prompt schema requiring:
  - score
  - diagnosis (with owner attribution: Teacher/Dean/Retrieval/UI)
  - fixes
  - operations (speed/cost/performance)

## 8) Current Metrics Snapshot
### RAG baseline vs improved (from `data/eval/eval_results_2026_04_17.json`)
- Baseline: `Hit@5=0.52`, `MRR=0.493`, `no_result=42/100`
- Improved: `Hit@1=0.57`, `Hit@3=0.64`, `Hit@7=0.71`, `MRR=0.616`, `no_result=22/100`
- Gate-2 status: **Passed**

### Edge-case stress (`data/eval/edge_case_eval_report.md`)
- Original 100: `Hit@5=0.50`, `MRR=0.473`
- Edge 50: `Hit@5=0.08`, `MRR=0.060`
- Combined 150: `Hit@5=0.36`, `MRR=0.336`
- Weakest category: Cross-chapter queries

### Proposition quality (`data/eval/proposition_quality_report.md`)
- Propositions checked: 474
- Possible hallucination flags: 3 (`0.63%`)
- Non-atomic propositions: 16 (`3.38%`)

### Conversation quality (`data/artifacts`)
- Sample simulation set: 12 conversations
- Reach rate: `0.667`
- Avg turns: `13.0`
- Avg hints: `1.83`
- Avg dean interventions: `7.17`
- Avg cost/conversation: `~$0.2387` over 33 stored conversation artifacts

## 9) Known Gaps (Important)
1. Clinical continuity quality
   - Multi-turn exists, but conversational cohesion/feedback depth still needs tightening in some runs.
2. UI refinement
   - Layout/composer/clinical card coherence and pinned input behavior still need hardening.
3. Cache utilization observability
   - Some traces still show zero cache reads/writes; validation and instrumentation checks are ongoing.
4. Memory backend dependency
   - Persistent memory quality depends on Qdrant memory availability.

## 10) Near-Term Roadmap
1. Clinical stage calibration
   - Enforce 2-3 turn coaching pattern with explicit “what was right vs wrong” feedback before closure.
2. Quality gate hardening
   - Reduce reveal-risk affirmations and reduce rubric mismatch between tutoring vs assessment scoring.
3. Edge-case retrieval improvements
   - Query rewriting, typo robustness, cross-chapter retrieval support.
4. UI production pass
   - Stabilize layout and debug/clean mode separation for demo reliability.

## 11) Key Files
- Core orchestration: `conversation/dean.py`, `conversation/teacher.py`, `conversation/nodes.py`, `conversation/edges.py`, `conversation/graph.py`, `conversation/state.py`
- Retrieval: `retrieval/retriever.py`
- Evaluation: `evaluation/euler.py`, `data/eval/*`
- Simulation/performance artifacts: `data/artifacts/*`
- UI/export logic: `ui/app.py`
