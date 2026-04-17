# Progress Journal — 2026-04-17_04-06-37

**Author:** Nidhi Rajani  
**Session:** Threshold diagnosis, out-of-scope calibration, original-100 reevaluation, Gate 2 rerun

---

## 1) Threshold diagnostics (pre-threshold CE scores)

Ran for 5 in-scope queries:
- What are the fascicles?
- What is the rotator cuff?
- What are the rhomboids?
- What is the patella?
- What is the orthostatic reflex?

For each query, printed:
- RRF top 5 (pre CE)
- CE score for each
- max CE score
- pass/fail against threshold 0.3

Key findings:
- `fascicles` max CE score was negative (~ -2.77), so it fails threshold and returns []
- The other 4 in-scope queries had max CE > 0.3 and passed threshold

Conclusion: threshold contributes to some false negatives, but retrieval candidate quality is also a major issue (especially index/noise chunks surfacing in RRF stage).

---

## 2) Applied threshold fix

Updated `config.yaml`:
- `retrieval.out_of_scope_threshold: 0.3 -> 0.1`

---

## 3) Out-of-scope verification after lowering threshold

Queries tested:
- what is the capital of France
- how do I write Python code
- who won the FIFA World Cup

All still returned `[]` at threshold 0.1.
No need to raise to 0.15.

---

## 4) source_chunk_id integrity check

Checked original 100 Q&A records against base chunk IDs.

Result:
- `Q&A pairs with source_chunk_id not in base chunks: 0`

So evaluation ID matching is structurally correct.

---

## 5) Re-ran original-100 evaluation (cold cache)

Results:
- Hit@1: 0.470
- Hit@3: 0.520
- Hit@5: 0.520
- MRR: 0.493
- no_result_count: 42

Top failures still dominated by no-result queries; `What are the fascicles?` still returned [] even at 0.1.

---

## 6) Gate 2 rerun

Command:
- `python -m pytest tests/test_rag.py -v`

Result:
- 2 failed, 3 passed

Failures:
1. `test_rag_qa_hit_and_mrr`
   - Hit@5 = 0.520 < 0.700 (FAIL)
   - MRR = 0.493 >= 0.400 (PASS on MRR, but test fails due Hit@5)
2. `test_result_count_in_range`
   - `What are the fascicles?` returned 0 chunks (expected 1-5)

Passes:
- retrieval latency
- out-of-scope empty responses
- no duplicate chunks

---

## Next

Primary blocker is still retrieval recall (not only threshold):
- high no-result rate on in-scope anatomy queries
- index/noise chunk competition in RRF stage

Suggested next fixes:
1. Exclude Chapter 28 INDEX chunks at retrieval time (or re-index without them)
2. Add lexical normalization/query rewrite for short factual queries
3. Consider lowering threshold further into CE logit domain OR calibrating CE scores
4. Regenerate low-quality Q&A prompts that produce vague/unanswerable question wording
