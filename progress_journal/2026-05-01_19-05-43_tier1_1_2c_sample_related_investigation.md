# 2026-05-01 — Tier 1 #1.2(c): sample_related "coronary circulation" investigation

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_18-59-18_tier1_1_2b_short_caps_anchors.md`

---

## What was investigated

Prior round flagged: "`sample_related` returned random cards for
'coronary circulation'". Investigation: build a tracer that mirrors
`TopicMatcher.sample_related()` step-by-step and inspect every vote
key, every match, every reject.

`/tmp/trace_sample_related.py` ran on `'coronary circulation'` with
`n=3, min_chunk_count=3`.

## What was found

**The function works correctly on this query.**

Trace summary:

```
Step 1: retriever.retrieve('coronary circulation', top_k=12)
  -> got 26 chunks  (Qdrant + BM25 + RRF + window-expansion)

Step 3: vote tallies (descending):
  2.5588  ch19 | sec='Heart Anatomy' | sub='Coronary Circulation'
  0.7456  ch28 | sec='Fetal Development' | sub='The Fetal Circulatory System'
  0.4226  ch20 | sec='Circulatory Pathways' | sub='The Aorta'
  0.0889  ch19 | sec='Heart Anatomy' | sub='Diseases of the…'
  0.0385  ch19 | sec='Heart Anatomy' | sub='Internal Structure of the Heart'

Step 4: matched 3/3 vote keys to teachable index entries:
  MATCH: 'Chapter 19: ... > Heart Anatomy > Coronary Circulation'
  MATCH: 'Chapter 28: ... > Fetal Development > The Fetal Circulatory System'
  MATCH: 'Chapter 19: ... > Heart Anatomy > Internal Structure of the Heart'
```

All 3 returned cards are genuinely related to the query. The "random
results" report from the prior round is no longer reproducible —
likely already fixed by the `chapter_num` backfill on `topic_index.json`
that landed in the round-1 commit (`346b66b`).

## What the trace DID surface (not the original bug, but real)

Two corpus-level issues, both downstream of `sample_related` (not in it):

### Bug X1 — Chapter 20 entirely missing from `data/topic_index.json`

Per-chapter entry count audit:

```
ch 1: 10  ch 8:  8  ch15: 10  ch22: 14
ch 2: 11  ch 9: 17  ch16: 13  ch23: 19
ch 3: 13  ch10: 14  ch17: 17  ch24: 10
ch 4: 10  ch11: 15  ch18: 19  ch25: 17
ch 5:  3  ch12:  6  ch19: 16  ch26: 14
ch 6: 15  ch13: 11  ch20:  0  <-- MISSING
ch 7: 16  ch14:  9  ch21: 20  ch27: 12
                              ch28: 24
```

`Chapter 20: The Cardiovascular System: Blood Vessels and Circulation`
**exists in `data/textbook_structure.json`** at line 1192 with all
subsections present (The Aorta, Pulmonary Circulation, Capillary
Exchange, Homeostatic Regulation, Circulatory Pathways, …). But
`scripts/build_topic_index.py` produced zero matches for it. Likely
cause: chunk-side metadata uses a different chapter-title format than
the structure JSON, so the build's `chapter_title + section_title` join
fails and `chunk_count = 0` → all entries dropped by the `≥1 chunk`
filter.

This means any query about blood vessels / circulation / the aorta /
pulmonary circulation will **never surface a ch20 card** even though
the chunks are indexed in Qdrant.

### Bug X2 — Truncated subsection title in 6 chunks

`subsection_title = 'Diseases of the…'` (literal ellipsis char `…`)
in 6 chunks. Source: `data/textbook_structure.json` line 1201:
`"Disorders of the...": {"difficulty": "moderate"}` — the structure
file itself has the truncation, propagated through the chunker. The
"…" is a placeholder for what was probably "Disorders of the
Cardiovascular System" or similar — the OpenStax PDF parsing dropped
the suffix.

Affects `sample_related` only as occasional unmatched vote keys (see
trace step 4); it does NOT cause random results because the function
correctly skips unmatched keys and uses lower-ranked vote keys
instead.

## Decision: NOT fixing X1 + X2 now

Both are real and worth fixing, but:
- X1 (ch20 missing) requires re-running `scripts/build_topic_index.py`
  which itself depends on the chunks file. If the chunks have the
  wrong chapter-title format, the build will still fail even after a
  rebuild — root cause is upstream in chunk metadata. Could ripple
  into hours of follow-up work.
- X2 (Disorders truncation) is in the source `textbook_structure.json`
  — fixing requires either re-extracting from PDF (expensive) or
  hand-editing the JSON (small but breaks reproducibility from PDF).
- Neither blocks the paper deadline. They're coverage gaps in the
  card-suggestion path; tutoring quality on locked sessions is
  unaffected.

Filed as known-issues followups for post-paper cleanup. Tier 1 #1.2(c)
itself is **closed**: the function works as designed; the original
report was about a state that no longer exists.

## Files touched

- New: `/tmp/trace_sample_related.py` (diagnostic, not committed —
  artifact-only, recoverable from this journal entry).
- Modified: `progress_journal/_tracker.json`.

## Cost

$0 (one retrieval call, no LLM).

## Next

Tier 1 #1.2(d): Sonnet-specific sycophancy patterns
("on an interesting track", "partly right", "both key concepts in
hand"). Extend the dean QC's banned-prefix regex.
