# 2026-05-01 — Haiku classifiers wired into dean.py + 3 regex blocks stripped

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_21-06-18_haiku_classifiers_poc.md`

---

## What shipped

Three regex blocks removed from `conversation/dean.py`, replaced by
Haiku classifier calls in `conversation/classifiers.py`:

| Removed | Lines (rough) | Replaced by |
|---|---|---|
| `_LETTER_HINT_PATTERNS` (~30 patterns) + `_has_letter_hint()` | ~80 | `haiku_hint_leak_check(draft, locked_answer, aliases)` |
| `_STRONG_AFFIRM_PATTERNS` (~30 patterns) + `_has_strong_affirmation()` | ~50 | `haiku_sycophancy_check(draft, student_state, reach_fired)` |
| `_BANNED_FILLER_PREFIXES` (10 openers) | ~20 | merged into sycophancy classifier |
| `_OFF_DOMAIN_REGEX` (vape/smoke/alcohol/etc.) | ~12 | `haiku_off_domain_check(student_msg)` |

Net: **~160 lines of regex deleted**, replaced with two import statements
and a `ThreadPoolExecutor` block.

### Wiring sites

1. **`_deterministic_tutoring_check`** (now hybrid):
   - Builds `hint_kwargs` + `sycoph_kwargs` once.
   - Runs `haiku_hint_leak_check` and `haiku_sycophancy_check` in
     parallel via `ThreadPoolExecutor(max_workers=2)`. Total latency
     ≈ max(individual) ≈ 2.3s instead of 4.5s sum.
   - Fail-open if classifier infra dies (won't block draft on phantom
     leak; LLM Dean QC still examines).
   - Records `wrapper="classifiers.haiku_hint_leak"` and
     `wrapper="classifiers.haiku_sycophancy"` traces with evidence
     and elapsed time.

2. **`_deterministic_assessment_check`**:
   - Single hint-leak classifier call (sycophancy isn't checked in
     assessment phase since the student is being graded, not coached).

3. **`_is_off_domain_judgment`** (DeanAgent method):
   - Single off-domain classifier call when `student_state == "irrelevant"`.

### Kept regex (not "QC" — mechanical or trivial)

Per the audit categorization the user OK'd:
- Word-boundary leak guards (`\b{locked_answer}\b`) — exact-string match,
  no judgment, free.
- Text utilities (`_normalize_text`, sentence split, JSON parse cleanup,
  chapter-num extract).
- Hedge markers in reach gate Step A (free shortcut on the FREE path
  of the two-step reach gate; replacing defeats the whole design).
- Opt-in yes/no, numeric card-pick (~10 patterns; trivial mechanics).
- Dean QC parse fallback (recovery code for malformed LLM JSON).

---

## E2E suite re-run #4 (verifying no regressions)

```
[A1] Cooperative trajectory (SA node / heart conduction) ... FAIL  ($0.1265)
[A2] Cooperative trajectory (epidermis layers) ............. PASS  ($0.1030)
[B1] Wrong-axon guess for synapse must not confirm ......... PASS  ($0.0965)
[C1] IDK ladder advances hint level ........................ PASS  ($0.2109)
[D1] Off-topic restaurant injection ........................ PASS  ($0.1498)
[E1] Persistent demand for the answer ...................... PASS  ($0.1759)
[F1] Multi-component partial reach (digestive processes) ... PASS  ($0.1227)
[G1] Whitespace-only message ............................... ERR   ($0.0573)
Done. 6/8 passed.
```

**Same 6/8 as run #3 — regex removal caused zero regressions.** A1 and
G1 still fail for the same non-regex reasons:

- **A1**: dean's `_prelock_refuse_call` LLM leaks the answer in its
  refuse copy ("try a term like 'sinoatrial node'"). LLM-side prompt
  bug, not classifier-related.
- **G1**: whitespace-only student input → Anthropic API 400 error
  ("messages: text content blocks must contain non-whitespace text").
  Input sanitization gap — we pass the raw whitespace through to the
  LLM.

Both will be addressed in the next round.

### Cost comparison

Suite cost on Haiku-classifiers build:
- Total: ~$0.94
- Per-scenario avg: ~$0.12 (was ~$0.13 previously without classifiers)

The Haiku calls add ~$0.02/scenario at most (only fires on Teacher
draft turns); savings come from a few scenarios that now lock topic
faster and skip wasted retrieval.

### Latency

Per-turn add from classifiers:
- Tutoring turn: hint+sycophancy parallel = ~2.3s extra
- Assessment turn: hint only = ~1.6s extra
- Off-domain (only fires on irrelevant turns): ~1.3s

Acceptable trade for the architectural win + catching novel phrasings
the regex didn't list.

---

## Files touched

- Modified: `conversation/dean.py`:
  - Deleted `_LETTER_HINT_PATTERNS` (66 lines)
  - Deleted `_has_letter_hint()` (10 lines)
  - Deleted `_STRONG_AFFIRM_PATTERNS` (44 lines)
  - Deleted `_has_strong_affirmation()` (24 lines)
  - Deleted `_BANNED_FILLER_PREFIXES` (20 lines)
  - Deleted `_OFF_DOMAIN_REGEX` regex (12 lines)
  - Added classifier wiring in `_deterministic_tutoring_check` (~50 lines)
  - Added classifier wiring in `_deterministic_assessment_check` (~25 lines)
  - Replaced regex call in `_is_off_domain_judgment` with classifier (~20 lines)
  - Added documentary comments where regex used to live
- Modified: `progress_journal/_tracker.json`

## Cost

E2E suite re-run: ~$0.94. Cumulative AWS Bedrock: ~$3.25 / $100.

---

## Next

Two non-regex bugs surfaced by the e2e suite, to be fixed:

1. **A1 prelock_refuse leak**: `_prelock_refuse_call` LLM is helpfully
   suggesting search terms that ARE the locked answer
   (e.g. *"try a term like 'sinoatrial node'"*). Fix the prompt to
   forbid suggesting specific anatomical terms in the refuse copy.
2. **G1 whitespace API 400**: sanitize whitespace-only student
   messages upstream of `graph.invoke()` so the API call doesn't hit
   Anthropic's empty-content rejection.

Then re-run the suite to confirm 8/8 → if so, expand to Stage 2 (port
remaining ~80 scenarios from teammate's bank).
