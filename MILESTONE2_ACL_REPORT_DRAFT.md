# Milestone 2 Report Draft (ACL-Style Content)

Use this as the source content for the final 5-page ACL report (references excluded).

## Title
Sokratic-OT: A Dean-Teacher Socratic Tutoring System for Occupational Therapy Anatomy with Hybrid Retrieval and Confidence-Aware Gating

## Abstract (Draft)
We present Sokratic-OT, a Socratic tutoring system for Occupational Therapy (OT) anatomy education. The system combines a dual-agent pedagogical architecture (Dean supervisor + Teacher tutor), hybrid retrieval over textbook-derived knowledge, and confidence-aware progression logic. The Dean controls answer locking, leak prevention, and quality gating, while the Teacher focuses on question-centric Socratic scaffolding. Our Milestone 2 baseline demonstrates an operational end-to-end prototype with a functioning RAG backbone, interactive tutoring loop, optional clinical reasoning stage, and evaluator-ready exports for post-hoc scoring. On a 100-query RAG benchmark, the system improves from Hit@5 0.52 / MRR 0.493 to Hit@7 0.71 / MRR 0.616 with no-result count reduced from 42 to 22. We report current strengths, failure modes, and concrete next steps for clinical-stage continuity and edge-case retrieval robustness.

## 1. Problem Statement
OT students need more than factual recall; they need anatomy-to-function reasoning for clinical interpretation. A direct-answer chatbot can reduce learning durability and promote shallow pattern matching. We target a tutoring system that:
- avoids direct answer reveal during learning turns,
- adapts hints based on student progress and confidence,
- supports topic-focused tutoring with optional clinical application,
- preserves evidence for rigorous evaluation (EULER/RAGAS-style scoring).

## 2. Data
### 2.1 Knowledge Source
Primary source: OpenStax Anatomy & Physiology textbook and structured derivatives.

| Asset | Count / Size | Path |
|---|---:|---|
| Raw elements | 5,231 | `data/processed/raw_elements_ot.jsonl` |
| Sections (L1/L2) | 574 | `data/processed/raw_sections_ot.jsonl` |
| Chunks | 7,801 | `data/processed/chunks_ot.jsonl` |
| Propositions | 40,614 | `data/processed/propositions_ot.jsonl` |
| Qdrant vectors | 40,614 | Qdrant snapshot in handoff |
| BM25 index | 1 file | `data/indexes/bm25_ot.pkl` |

### 2.2 Evaluation Sets
| Dataset | Size | Path |
|---|---:|---|
| RAG QA benchmark | 100 | `data/eval/rag_qa.jsonl` |
| Edge-case QA benchmark | 50 | `data/eval/rag_qa_edge_cases.jsonl` |

### 2.3 Sample Transcript Excerpt (system-generated)
Example from `data/artifacts/conversations/S1_04e45789_turn_2.json`:
- Student topic: “brachial plexus”
- Tutor Socratic turn: asks for deltoid-control branch hypothesis
- Student answer: “axillary nerve”
- Clinical stage: asks to distinguish suprascapular vs axillary deficits in MMT

This demonstrates core -> clinical transition with functional reasoning prompts.

## 3. Solution Architecture
### 3.1 Dean-Teacher Design
- **Teacher**: Generates Socratic prompts and clinical questions.
- **Dean**: Runs retrieval, locks the correct answer, classifies student state, performs quality checks, and controls progression.

### 3.2 Session Flow
`rapport -> topic scoping -> tutoring -> assessment (clinical opt-in) -> memory_update`

### 3.3 Retrieval Pipeline
1. Qdrant dense retrieval
2. BM25 sparse retrieval (query preprocessing)
3. Reciprocal Rank Fusion (`rrf_k=60`)
4. Parent chunk expansion
5. Cross-encoder reranking
6. Final top-7 contexts

### 3.4 Example Query Path (for architecture figure)
Input: “Which nerve innervates the deltoid?”
1. Dean retrieves candidate chunks.
2. Dean locks answer internally.
3. Teacher produces non-revealing Socratic question.
4. Dean quality gate checks leak risk + question compliance.
5. Approved response sent to student.
6. If confidence-thresholded correctness is met, system enters clinical application.

### 3.5 Prompt Caching and Observability
- Static prompt + retrieved chunks are packed into cached blocks.
- Dynamic state/history remain non-cached.
- Export includes per-turn traces, contexts, and scoring-ready aliases.

## 4. Experiments and Baseline Results
### 4.1 Retrieval Results
Source: `data/eval/eval_results_2026_04_17.json`

| Metric | Baseline | Current |
|---|---:|---:|
| Hit@5 | 0.52 | — |
| Hit@1 | — | 0.57 |
| Hit@3 | — | 0.64 |
| Hit@7 | — | 0.71 |
| MRR | 0.493 | 0.616 |
| No-result count | 42/100 | 22/100 |

Gate-2 status: **Passed**.

### 4.2 Edge-Case Stress Test
Source: `data/eval/edge_case_eval_report.md`

| Split | N | Hit@5 | MRR |
|---|---:|---:|---:|
| Original | 100 | 0.50 | 0.473 |
| Edge cases | 50 | 0.08 | 0.060 |
| Combined | 150 | 0.36 | 0.336 |

Category weakness: cross-chapter and clinical edge queries.

### 4.3 Proposition Quality
Source: `data/eval/proposition_quality_report.md`

| Check | Result |
|---|---:|
| Propositions audited | 474 |
| Possible hallucination flags | 3 (0.63%) |
| Non-atomic propositions | 16 (3.38%) |

### 4.4 Conversation Baseline (Simulation Slice)
Source: `data/artifacts/simulation_analysis_latest2.json`

| Metric | Value |
|---|---:|
| Conversations | 12 |
| Reach rate | 0.667 |
| Avg turns | 13.0 |
| Avg hints | 1.83 |
| Avg dean interventions | 7.17 |

### 4.5 Cost and Performance
From artifacts (`data/artifacts/conversations/*` aggregated):
- Conversations analyzed: 33
- Avg API calls / conversation: 49.364
- Avg input tokens / conversation: 33,565.333
- Avg output tokens / conversation: 6,148.970
- Avg cost / conversation: $0.238739
- Total cost across analyzed artifacts: $7.878396

## 5. Error Analysis (Who Owns What)
We separate issues by component responsibility.

### 5.1 Teacher-dominant issues
- Over-affirmation in partial/incorrect contexts.
- Some clinical prompts become disconnected from preceding turn context.

### 5.2 Dean-dominant issues
- QC/parser brittleness in prior runs (improved but still monitored).
- Gating decisions can close clinical stage quickly when confidence is high.

### 5.3 Retrieval-dominant issues
- Cross-chapter queries underperform.
- Informal vocabulary and malformed inputs yield no-result cases.

### 5.4 Scoring/diagnosis framework in export
Export now includes a conversation-scoring prompt requiring:
1. **Score** (main + subscores),
2. **Diagnosis** (what failed, evidence turns, impact, owner attribution),
3. **Fixes** (concrete implementation steps + expected gain),
4. **Operations** (speed/cost/performance analysis).

This supports consistent offline auditing and report generation.

## 6. What Is Left (Milestone 2 -> Final)
1. Tighten clinical continuity: enforce richer “what right vs wrong” feedback before closure.
2. Improve retrieval on edge categories: cross-chapter reasoning + query rewriting.
3. Stabilize prompt-cache observability and cache-hit verification.
4. Complete UI hardening (clean/debug mode consistency, clinical card cohesion).
5. Enable and validate persistent memory once Qdrant memory backend is attached.

## 7. Future Direction
- Multi-step clinical coaching with adaptive remediation loops.
- Better mastery-tier calibration using both core and clinical evidence trajectories.
- Structured tutor turn planning for improved pedagogical consistency.
- Robust evaluator pipeline that combines EULER-style pedagogy checks with RAGAS retrieval-grounded metrics.

## 8. Figures and Tables to Include in Final ACL PDF
### Required visual assets
1. **Architecture diagram**: user query -> retrieval -> dean gating -> teacher output -> assessment -> memory.
2. **RAG results table**: baseline vs current metrics.
3. **Edge-case category chart**: category-wise Hit@5 / MRR.
4. **Conversation flow screenshot**: one complete core + clinical session.
5. **Error ownership chart**: Teacher vs Dean vs Retrieval issue distribution.

### Suggested screenshot sources
- UI flow screenshots from your live run folder
- Conversation JSON examples from `data/artifacts/conversations/`

## 9. ACL Formatting Checklist (for final document assembly)
- Keep within 5 pages excluding references.
- Use ACL style file and bibliography format.
- Use concise prose; prioritize diagrams/tables.
- Present baseline first, then improvements and remaining gaps.
- Keep sectioning research-paper style (not phase-wise logs).

## 10. One-Paragraph Conclusion (Draft)
Sokratic-OT now has a functional baseline that couples pedagogical control (Dean-Teacher gating) with retrieval-grounded tutoring for OT anatomy. The system demonstrates strong gains on core retrieval metrics and a usable end-to-end tutoring loop with confidence-aware progression and clinical application support. Remaining weaknesses are concentrated in edge-case retrieval robustness, clinical-stage continuity depth, and production UI/persistence polish. These are tractable and already instrumented through export metadata and conversation-level diagnostics.
