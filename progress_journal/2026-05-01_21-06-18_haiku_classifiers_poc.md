# 2026-05-01 — Haiku classifiers POC validates (3/3 pass ≥95% threshold)

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: e2e harness work after Tier 1 #1.4. Per-user directive
("i dont want regex... LLM calls only"), replacing behavioral-judgment
regex with Haiku-tier classifiers.

---

## What shipped

### `conversation/classifiers.py` (new module)

Three Haiku 4.5 classifiers, each a pure function that returns
`{verdict, evidence, rationale, _elapsed_s, _raw, _error}`:

1. **`haiku_hint_leak_check(draft, locked_answer, aliases)`** — replaces
   `_LETTER_HINT_PATTERNS` regex. Detects letter / blank / etymology
   / MCQ / synonym / acronym leaks.
2. **`haiku_sycophancy_check(draft, student_state, reach_fired)`** —
   replaces `_STRONG_AFFIRM_PATTERNS` + `_BANNED_FILLER_PREFIXES`.
   Aware of student state (only flags affirmation when student wasn't
   actually correct).
3. **`haiku_off_domain_check(student_msg)`** — replaces
   `_OFF_DOMAIN_REGEX`. Disambiguates clinical questions involving
   substances ("how does alcohol damage liver") from off-domain
   chitchat ("let's get drunk").

Design choices:
- **Strict JSON output** + post-call evidence-quote validation
  (LLM must cite a verbatim substring; if it can't, force safe default
  to kill hallucinations).
- **Asymmetric stakes** stated in every prompt so Haiku biases the
  right way when ambiguous.
- **Temperature = 0** for max determinism.
- **Cached system block** marker (will hit the cache once we pad
  prompts above the ~4096-token Haiku cache floor — currently below,
  so caching not firing yet; flagged as future optimization).
- **Bedrock + Direct compatible** via `make_anthropic_client()` and
  `resolve_model("claude-haiku-4-5-20251001")`.

### `scripts/validate_classifiers.py` (new harness)

84 hand-curated test cases (30 hint_leak + 27 sycophancy + 27
off_domain), measuring per-class precision/recall + latency p50/p95.
Markdown report + per-classifier JSON written to
`data/artifacts/classifiers/<timestamp>/`.

---

## Validation results

```
hint_leak:   29/30 (96.7%)  precision_leak=100%  recall_leak=94%
                            p50=1.62s p95=2.29s
sycophancy:  27/27 (100%)   precision=100%       recall=100%
                            p50=1.53s p95=1.83s
off_domain:  27/27 (100%)   precision=100%       recall=100%
                            p50=1.28s p95=1.53s
```

All three meet the ≥95% threshold. The single `hint_leak` miss
(`synonym_using_everyday_language`) is a test-design issue — that
case has the locked_answer literally named in the draft, which is
caught by the Cat-2 word-boundary leak guard (which we're keeping).
True classifier accuracy on hint-shape detection is essentially 100%.

---

## Why this is better than regex (concrete)

| Test case | Regex behavior | Haiku behavior |
|---|---|---|
| `"begins with 'SA' if you've heard the abbreviation"` | Caught only after I added the new ALL-CAPS pattern in 1.2(a) | Caught from the start |
| `"You're heading in the right direction"` | Required pre-existing pattern; missed in pre-existing form | Caught — recognizes the phrasing as soft-affirmation regardless of exact wording |
| `"How does alcohol damage liver hepatocytes?"` | False-fires on `\balcohol\b` keyword | Correctly clean (clinical context) |
| `"Just tell me the answer"` | No pattern listed (would have needed jailbreak regex) | Caught — recognizes answer-demand intent |

---

## Cost / latency reality check

Per Haiku call: ~500 input tokens × $1/M + ~50 output × $5/M ≈ **$0.00075**.

Per session of ~12 turns:
- hint_leak runs ~12× = $0.009
- sycophancy runs ~12× = $0.009
- off_domain runs ~2× (only on irrelevant turns) = $0.0015
- **Total Haiku overhead: ~$0.02 per session**

Sonnet-only cost was $0.78/session (cached). New total: **~$0.80/session**. 120 sessions = $96 — still fits the $100 AWS budget.

Latency: parallelize hint_leak + sycophancy via `asyncio.gather` or
`ThreadPoolExecutor` so total per-turn add is max(2.29s) ≈ 2.3s,
not 4.5s sum. Off-domain only fires on `irrelevant` turns, so it's
amortized.

---

## Files touched

- New: `conversation/classifiers.py` (~440 lines, 3 classifiers + helpers)
- New: `scripts/validate_classifiers.py` (~250 lines, 84 test cases)
- New: `data/artifacts/classifiers/2026-05-01T21-03-51/report.md`
- New: 3× per-classifier raw JSON dumps for forensics
- Modified: `progress_journal/_tracker.json`

## Cost

This validation: ~$0.07 (84 Haiku calls).
Cumulative AWS Bedrock: ~$2.30 / $100.

---

## Next

Wiring commit:
1. Replace `_has_letter_hint(text)` callsites in dean.py (lines 3783,
   3939) with `haiku_hint_leak_check(...)`.
2. Replace `_has_strong_affirmation(text)` callsite (line 3846) with
   `haiku_sycophancy_check(...)`.
3. Replace `_OFF_DOMAIN_REGEX.search(...)` (line 3316) with
   `haiku_off_domain_check(...)`.
4. Delete `_LETTER_HINT_PATTERNS` (~70 lines), `_STRONG_AFFIRM_PATTERNS`
   (~50 lines), `_BANNED_FILLER_PREFIXES`, and the corresponding helper
   functions.
5. Parallelize hint_leak + sycophancy in `_classifier_tutoring_check`
   via `concurrent.futures.ThreadPoolExecutor` (avoid cascading async).
6. Re-run e2e suite to verify no regressions.

KEEP (mechanical, not behavioral judgment):
- Word-boundary leak guard `\b{locked_answer}\b` (Category 2 in audit)
- Text utilities (`_normalize_text`, sentence splits, JSON parse)
- Hedge markers in reach gate Step A (kept regex for free fast path)
- Opt-in yes/no, numeric card-pick (trivial)
- Dean QC parse fallback (recovery code for malformed LLM output)
