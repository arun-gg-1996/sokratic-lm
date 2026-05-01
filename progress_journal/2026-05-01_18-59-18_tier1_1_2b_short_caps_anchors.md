# 2026-05-01 — Tier 1 #1.2(b): short ALL-CAPS abbreviations as distinctive anchors

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_18-56-38_tier1_1_2a_letter_hint_extensions.md`

---

## The bug

`_is_distinctive_anchor(token)` (`conversation/dean.py:387`) had three
ways to return True before this fix:
- multi-word phrase
- ≥5 chars and not in the common-anatomy stopword set

Both branches **rejected short ALL-CAPS abbreviations**. So when the
`locked_answer` was `"SA"` (sinoatrial node), `"RCA"` (right coronary
artery), `"ATP"`, `"Fc"`, `"LDL"`, etc., the leak-detection paths at
`dean.py:3457`, `:3467`, `:3531`, `:3618`, and `:3625` skipped the
word-boundary reveal check entirely. The Teacher could literally say
"the answer is SA" mid-tutoring and the deterministic guard would
ignore it.

## The fix

Two new branches added to `_is_distinctive_anchor`:

1. **ALL-CAPS short token (case preserved)**:
   ```python
   if 2 <= len(word_raw) <= 4 and word_raw.isupper():
       return True
   ```
   Catches `"SA"`, `"AV"`, `"RCA"`, `"LCA"`, `"ATP"`, `"DNA"`, `"EKG"`,
   `"MRI"`, `"CT"`, `"ICU"` when callers pass the original casing.
2. **Curated short-abbreviation set (case-insensitive)**:
   New `_DISTINCTIVE_SHORT_ABBREVIATIONS` frozenset with ~40 common
   clinical / anatomical / biochemistry abbreviations. Closes the gap
   when the caller has already lowercased the token (e.g. `"sa"`,
   `"rca"`, `"atp"`).

Set covers cardiac (sa, av, rca, lca, lad, ivc, svc), neuro (cn, cns,
pns, rem, csf, bbb), biochem (atp, adp, gtp, dna, rna, mrna, fc, fab,
ig, iga–igd), lipids/blood (ldl, hdl, vldl, rbc, wbc, cbc, hb, abg),
endocrine (tsh, fsh, lh, acth, adh, gh, prl), imaging (ekg, mri, ct,
pet), and other clinical (icu, er, or, ot, pt).

## Self-test results

`/tmp/test_distinctive_anchor.py`:

```
Results: 70/70 passed (37 positives, 33 negatives)
```

37 positive cases caught:
- Existing ≥5-char path still works (nucleus, ganglion, pepsin, …).
- Multi-word still works ("skeletal muscle pump", "left coronary artery",
  "sa node").
- New: ALL-CAPS short — SA, AV, RCA, LCA, ATP, ADP, Fc, Ig, LDL, HDL,
  DNA, RNA, EKG, MRI, CT, ICU.
- New: lowercased short — sa, rca, atp, ldl, ekg, ct, icu.
- New: capitalized short — Fc, Ig.

33 negative cases clean (no false positives):
- Common anatomy stopwords (muscle, nerve, vein, bone, artery, tissue,
  left, right, anterior, posterior).
- Short generic English (is, a, an, the, of, in, on, ma, if, as, no, up,
  go, see, and, for, but).
- Common 4-char anatomy (left, node, vein).
- Empty / whitespace.

## Files touched

- Modified: `conversation/dean.py` (~+50 lines: new
  `_DISTINCTIVE_SHORT_ABBREVIATIONS` frozenset + 2 new branches +
  reworked docstring).
- Modified: `progress_journal/_tracker.json`.

## Cost

$0 (pure code change, smoke-tested locally with 70 cases).

## Next

Tier 1 #1.2(c): investigate why `sample_related` cold-starts on queries
like "coronary circulation" — the function returned random cards when
this query was used. Likely cause: vote-key mismatch between retriever
output and topic-index entries, or `min_chunk_count` floor too high.
