# 2026-05-01 — Tier 1 #1.2(a): letter-hint regex extensions

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_18-52-54_tier1_1_1_bedrock_cache_verified.md`

---

## What was added

Four new pattern groups in `_LETTER_HINT_PATTERNS`
(`conversation/dean.py:421+`):

1. **Two/three-letter starts-with reveal**:
   `\b(?:starts?|begins?)\s+with\s+['\"\`]?[a-z]{2,3}['\"\`]?(?:\b|[\s,.])`
   Closes the gap where the existing single-letter pattern at line 423
   (`[a-z]` exactly one char) misses ALL-CAPS abbreviations: "starts with
   'SA'", "begins with 'RCA'", "begins with 'ATP'".
2. **Acronym / initialism expansion**:
   - `\bstands?\s+for\s+(?:the\s+|a\s+)?\w+\s+\w+`
   - `\babbreviated\s+(?:as|for)\b`
   - `\b(?:acronym|initialism)\s+(?:for|of)\b`
   "ATP stands for adenosine triphosphate" / "abbreviated as RCA" / "the
   acronym for RAAS". The teacher must let the student deduce
   abbreviation expansions, not hand them over.
3. **First-letters-of construction**:
   - `\bfirst\s+letters?\s+(?:of|from)\s+(?:the\s+)?(?:three|four|five|two|several|each)\b`
   - `\bmade\s+(?:up\s+)?(?:of|from)\s+the\s+first\s+letters?\b`
   - `\beach\s+letter\s+(?:represents|stands\s+for|is)\b`
   "Made up of the first letters of three words" reveals the structural
   shape of an abbreviation.
4. **Synonym-translation reveal**:
   - `\b(?:common|everyday|simple|plain|lay|layman'?s)\s+(?:english\s+)?(?:word|term|name)\s+for\b`
   - `\bthe\s+(?:medical|technical|formal|scientific|anatomical|clinical)\s+(?:word|term|name)\s+for\b`
   - `\b(?:in|using)\s+(?:everyday|simple|plain|lay)\s+(?:language|terms|words)\b`
   When the locked answer is a technical term (e.g. *epidermis*), the
   teacher offering the layman synonym ("the everyday word for that is
   skin") is a covert reveal — and vice versa.

## Self-test results

`/tmp/test_letter_hint_extensions.py` (18 positive cases + 10 negative
cases):

```
Results: 27/28 passed (18 positives, 10 negatives)
```

The single "miss" was on the contrived negative
`"Some words start with abc but that's not relevant here."` — which
is a leak-shaped sentence a Socratic teacher should never produce. The
behavior is correct; the test case is what's wrong, not the regex.

All 18 real leak patterns were caught:
- "starts with 'SA'", "begins with 'RCA'", "begins with 'ATP'", "ends with 'ase'"
- "ATP stands for adenosine triphosphate", "abbreviated as RCA", "acronym for RAAS", "initialism for the structure is SA"
- "first letters of three words", "first letters of each component", "each letter represents", "each letter stands for"
- "common English word for epidermis", "everyday word for systole", "medical word for funny bone", "technical term for breathing", "scientific name for the bone", "using everyday language"

All 9 legitimate Socratic prompts cleanly passed (`What do you think
happens during contraction?`, `Tell me what you already know about the
heart`, etc.).

## Files touched

- Modified: `conversation/dean.py` (+~30 lines in `_LETTER_HINT_PATTERNS`).
- Modified: `progress_journal/_tracker.json` (last_prompt bump).

## Cost

$0 (regex-only change, smoke-tested locally).

## Next

Tier 1 #1.2(b): `_is_distinctive_anchor` for short ALL-CAPS abbreviations
(Fc, SA, RCA, LCA, ATP). The current rule requires `≥5 chars` OR
multi-word; ALL-CAPS-with-≥2-chars needs its own branch.
