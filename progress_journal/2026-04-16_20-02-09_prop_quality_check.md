# Progress Journal — 2026-04-16_20-02-09 (proposition quality check)

**Author:** Nidhi Rajani  
**Session:** Phase 1 Step 4 — Hallucination + atomicity audit on first 50 base-chunk propositions

---

## Scope
- Input propositions: `data/processed/propositions_ot.jsonl`
- Scope filter: propositions whose `parent_chunk_id` belongs to first 50 base chunks (`is_overlap=false`, excluding `paragraph_overlap`)
- Total propositions evaluated: **474**

## Hallucination grounding check
Method: extract proper nouns (with sentence-start capitalization filtering) and numbers from each proposition, and verify each extracted term appears in `parent_chunk_text`.

- Fully grounded: **471**
- Flagged possible hallucination: **3**
- Hallucination rate: **0.63%**

Flagged items:
1. Missing term `Earth`
   - Proposition: *The surface of Earth and its atmosphere provide a range of temperature and pressure necessary for human survival.*
   - Parent chunk: `7a6bde4b-ee46-4a0f-97d8-e86091736bc1`
2. Missing term `Fahrenheit`
   - Proposition: *Physicians lower a cardiac arrest patient's body temperature to approximately 91 degrees Fahrenheit as part of controlled hypothermia treatment.*
   - Parent chunk: `d0e40ea9-cdf7-4c40-8a3d-3d17faee5579`
3. Missing term `Fahrenheit`
   - Proposition: *Lowering the body temperature to approximately 91 degrees Fahrenheit slows the patient's metabolic rate.*
   - Parent chunk: `d0e40ea9-cdf7-4c40-8a3d-3d17faee5579`

## Atomicity check
- Propositions with 3+ commas: **5**
- Propositions with `" and "` >= 2: **12**
- Non-atomic union count: **16**
- Non-atomic rate: **3.38%**

## Target comparison
- Hallucination target: 0%  
  Observed: **0.63%** (near target, not zero)
- Non-atomic target: < 2%  
  Observed: **3.38%** (above target)

## Artifact
- Report file saved: `data/eval/proposition_quality_report.md`

## Next
- Tighten proposition prompt to reduce short/paraphrased outputs and split compound facts more aggressively before approving full run.
