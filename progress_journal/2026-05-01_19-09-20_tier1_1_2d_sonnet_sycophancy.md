# 2026-05-01 — Tier 1 #1.2(d): Sonnet sycophancy regex + pre-existing affirmation bug fix

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_19-05-43_tier1_1_2c_sample_related_investigation.md`

This was a 2-bug fix: a planned addition (Sonnet sycophancy patterns) AND a
pre-existing latent bug surfaced while writing the smoke test.

---

## Bug 1 (pre-existing, surfaced today): _has_strong_affirmation never fired on contractions

`_has_strong_affirmation()` (`conversation/dean.py:575`) normalizes the
first sentence with `_normalize_text()` (replaces all non-alphanumerics
including apostrophes with spaces) and tests against
`_STRONG_AFFIRM_PATTERNS`. The patterns use the contraction form
`you'?re` / `that'?s` which means "apostrophe-or-nothing".

Problem: after normalization, `"you're"` becomes `"you re"` (with a
space). `you'?re` matches `you're` or `youre` — **NOT** `you re`. So the
regex silently failed on every real-world contraction-bearing
affirmation. Verified:

```
_has_strong_affirmation("You're right!")           -> False
_has_strong_affirmation("You're getting there.")   -> False
_has_strong_affirmation("You're on a good track.") -> False
_has_strong_affirmation("You've started ...")      -> False
```

How long this has been broken: at least since 2026-04-19 when
`_normalize_text` was introduced (per the conversation/dean.py git log).
The contraction patterns added in subsequent rounds (`r"\byou'?re right\b"`,
`r"\byou'?ve identified\b"`, etc.) all silently never matched. Cluster 4
"sycophancy guard" from the round-1 journal was therefore only
partially effective — patterns without contractions worked
(`\bexcellent\b`, `\bspot on\b`), patterns with contractions did not.

### Fix

Updated `_has_strong_affirmation` to test against BOTH the raw
lowercased first sentence AND the normalized form. Either match
fires:

```python
def _has_strong_affirmation(text: str) -> bool:
    sentence = _first_sentence(text)
    if not sentence:
        return False
    raw = (sentence or "").strip().lower()
    norm = _normalize_text(sentence)
    for pat in _STRONG_AFFIRM_PATTERNS:
        if raw and re.search(pat, raw):
            return True
        if norm and re.search(pat, norm):
            return True
    return False
```

This fixes every existing contraction-based pattern simultaneously
without requiring rewrite. Verified post-fix:
`_has_strong_affirmation("You're right!")` now returns True.

---

## Bug 2 (planned): Sonnet 4.6 sycophancy patterns

Sonnet 4.6 produces softer affirmation styles than Haiku 4.5 — empathic
"you're on track" / "partly right" / "both key concepts in hand"
phrasings that bypassed the existing strict-affirmation regex. Observed
in real 2026-05-01 antibody / CNS / cerebrum sessions.

### Patterns added to _STRONG_AFFIRM_PATTERNS

```python
r"\bon (an|a|the) interesting track\b",
r"\bon (an|a|the) (right|good) track\b",
r"\bheading in the right direction\b",
r"\b(?:you'?re|you are) on the right path\b",
r"\bpartly (right|correct)\b",
r"\b(both|all) (?:the )?key concepts? in hand\b",
r"\b(both|all) (?:the )?(?:key |important )?(?:components|pieces|parts) (?:are )?in hand\b",
r"\byou'?re getting there\b",
r"\byou'?re close\b",
r"\bgetting closer\b",
r"\bclose to (?:the |a |an )?(?:right|correct) (?:answer|idea|direction)\b",
r"\bin the right neighborhood\b",
r"\b(?:nice|good|great)\s+(?:thinking|intuition|reasoning|approach|instinct)\b",
r"\byou'?ve started (?:to )?(?:see|grasp|connect|identify)\b",
r"\byou'?ve (?:already )?(?:touched on|hinted at|gestured at|started toward|begun to)\b",
```

### Patterns added to _BANNED_FILLER_PREFIXES

(Triggers when the response starts with these — softer and more
specific than full-sentence regex.)

```python
"you're on the right",
"you're heading in the right",
"that's a thoughtful",
"great thinking",
"nice thinking",
"good intuition",
"you're thinking",
"good question",
"great question",
"interesting question",
```

---

## Self-test results

`/tmp/test_sycophancy.py` — 21 positive cases (real Sonnet sycophancy
phrases) + 7 negative cases (legitimate Socratic prompts):

```
Results: 28/28 passed (21 positives, 7 negatives)
```

Every Sonnet sycophancy phrase fires; every legitimate Socratic prompt
stays clean.

---

## Files touched

- Modified: `conversation/dean.py`:
  - `_BANNED_FILLER_PREFIXES`: +10 new openers
  - `_STRONG_AFFIRM_PATTERNS`: +15 new patterns (Sonnet sycophancy)
  - `_has_strong_affirmation`: dual-pass (raw + normalized) to fix
    pre-existing contraction-bypass bug
- Modified: `progress_journal/_tracker.json`

## Cost

$0 (regex-only change, smoke-tested locally with 28 cases).

---

## Next

Tier 1 #1.4: build the e2e driver adapting friend's pattern (90+
scenarios from `docs/external_reference/run_e2e_tests.py`, translated
to our state machine + WebSocket transport). This is the biggest
single Tier 1 item (~3 hr) and the leverage win — every fix shipped
today + multi-anchor reach (#1.3) needs the driver to verify at scale.
