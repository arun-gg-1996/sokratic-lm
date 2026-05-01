# 2026-05-01 — Tier 1 #1.3: multi-anchor reach (K of N partial credit)

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Continues: `2026-05-01_19-49-06_x1_x2_corpus_fixes.md`

---

## Problem

`reached_answer_gate()` Step A required ALL content tokens of
`locked_answer` (or any single alias) to appear in the student message.
For multi-component locked answers like
`"left and right coronary arteries"`, a student saying `"LCA"` matched
0 tokens of the umbrella → reach=False. They had to articulate BOTH
components (left AND right) in one message to count.

That's wrong pedagogically. Saying "LCA" is correct for half of the
target. The reach gate should award partial credit and let mastery
scoring weight by coverage.

This is also a measurement bug: in real sessions, multi-component
answers (the four digestive processes; the six synovial joint types;
the five layers of the epidermis; the two coronary arteries) showed
reach=False on partial student answers, then the LLM Step B fallback
either accepted them (wasting an LLM call) or rejected them
(under-counting reach). The simulation eval was reading these as failed
sessions.

---

## Fix

Three layers of changes:

### 1. New helper `_split_locked_answer(answer)` in `conversation/dean.py`

Splits a locked_answer on `" and "`, `" or "`, `,`, `;` to identify
top-level noun-phrase components. Whitespace-padded conjunctions only
(so `"ball-and-socket"` stays as one component, not split by the
inner "and"). Examples:

| input | components |
|---|---|
| `"skeletal muscle pump"` | `["skeletal muscle pump"]` (1) |
| `"left and right coronary arteries"` | `["left", "right coronary arteries"]` (2) |
| `"ingestion, propulsion, mechanical digestion, chemical digestion"` | 4 components |
| `"pivot, hinge, condyloid, saddle, plane, ball-and-socket"` | 6 components |
| `"Tc, Th1, Th2, Treg cells"` | 4 components |

### 2. K-of-N logic in `reached_answer_gate()`

Three-step strategy (was two-step):

- **Step A.1a** — locked_answer full match (always allowed, single or multi)
- **Step A.1b** — alias full match (single-component only — multi-component
  aliases are per-component identifiers, not full-answer paraphrases)
- **Step A.2** (NEW) — K-of-N partial reach for multi-component answers:
  - For each component AND each alias, check if its tokens are in the message
  - Dedup matches by sorted-token-tuple AND by subset/superset overlap so
    `"left coronary"` + `"left coronary artery"` count as 1
  - If matched ≥ ceil(N/2): full or partial reach
  - `coverage = K/N`
  - `path = "overlap"` if K=N else `"partial_overlap"`
- **Step B** — LLM paraphrase fallback (unchanged); coverage defaults
  to 1.0/0.0 binary on this path

### 3. State-schema additions

`conversation/state.py`:
- `student_reach_coverage: float` (0.0–1.0)
- `student_reach_path: str` (overlap, partial_overlap, paraphrase, etc.)

Plus `initial_state()` defaults.

### 4. Mastery scorer integration

`memory/mastery_store.py::score_session_llm`: read
`state["student_reach_coverage"]` and `state["student_reach_path"]`
and surface them in the dynamic prompt template. Backward-compat
default `1.0` when reached and `0.0` otherwise (covers state dicts
written before #1.3).

`config/base.yaml::mastery_scorer_dynamic`: new section explaining
coverage semantics and how to weight partial reach. The scorer is
instructed to weight mastery proportionally to coverage when
`reach_path = "partial_overlap"`, rather than treating partial reach
as a clean session.

---

## Self-test

`/tmp/test_kofn_reach.py` — 8 split tests + 9 gate scenarios:

```
=== _split_locked_answer ===
  [PASS] 'skeletal muscle pump'                          -> 1 component
  [PASS] 'left and right coronary arteries'              -> 2 components
  [PASS] 'ingestion, propulsion, mechanical digestion, chemical digestion' -> 4
  [PASS] 'pivot, hinge, condyloid, saddle, plane, ball-and-socket' -> 6
  [PASS] 'the heart or the lungs'                        -> 2 components
  [PASS] ''                                              -> 0 components
  [PASS] 'single'                                        -> 1 component
  [PASS] 'Tc, Th1, Th2, Treg cells'                      -> 4 components
  split: 8/8

=== K-of-N partial reach scenarios ===
  [PASS] single full match               (overlap, cov=1.00)
  [PASS] single no match                 (no_overlap, cov=0.00)
  [PASS] 2-component full                (overlap, cov=1.00)
  [PASS] 2-component partial via component (partial_overlap, cov=0.50)
  [PASS] 2-component partial via alias    (partial_overlap, cov=0.50)
  [PASS] 2-component full via aliases     (overlap, cov=1.00)
  [PASS] 4-component 2-of-4              (partial_overlap, cov=0.50)
  [PASS] 4-component 1-of-4 below thresh (no_overlap, cov=0.00)
  [PASS] 6-component 3-of-6 at threshold (partial_overlap, cov=0.50)

Results: split=8/8, gate=9/9
```

17/17 pass. Includes the regression-protection cases:
- Single-component still works exactly as before (no behavior change).
- Multi-component with all-components-in-msg still gives `coverage=1.0`.
- Below-threshold partial (1/4) correctly does NOT trigger reach.
- Single-alias match on multi-component answer correctly gives `0.50`,
  not `1.00` (the earlier failing case after the first patch).

---

## Behavioral changes

| Scenario | Before | After |
|---|---|---|
| Single-component locked, full match | `reached=True, cov=1.0` | `reached=True, cov=1.0` (no change) |
| Single-component locked, alias match | `reached=True, cov=1.0` | `reached=True, cov=1.0` (no change) |
| Single-component locked, no match | `reached=False` (Step B) | `reached=False` (Step B) (no change) |
| Multi-component, all components present | `reached=True` (only if ALL tokens present in one phrase) | `reached=True, cov=1.0` (more reliable; doesn't require single-phrase delivery) |
| Multi-component, K of N components present (K ≥ ceil(N/2)) | `reached=False` (regression bug) | `reached=True, cov=K/N`, `path=partial_overlap` |
| Multi-component, K < ceil(N/2) | `reached=False` (was correct) | `reached=False` (still correct; falls through to Step B LLM) |
| Multi-component, single alias match (e.g. "LCA") | `reached=True, cov=1.0` (false positive — student got half right but counted as full) | `reached=True, cov=0.5`, `path=partial_overlap` (correct partial credit) |

The biggest semantic change is **the multi-component single-alias case**.
Previously, saying just "LCA" on a "left and right coronary arteries"
lock counted as full reach. Now it correctly counts as half. Mastery
scoring will weight this proportionally rather than awarding full
credit for a partial answer.

---

## Files touched

- Modified: `conversation/dean.py`:
  - New helper `_split_locked_answer()` (~+30 lines)
  - `reached_answer_gate()` rewritten (~+80 lines net): 3-step strategy,
    coverage in return dict, n_matched/n_total fields
  - State propagation: `student_reach_coverage`, `student_reach_path`,
    new turn_trace fields
- Modified: `conversation/state.py`:
  - `student_reach_coverage: float`, `student_reach_path: str` schema
  - `initial_state()` defaults
- Modified: `memory/mastery_store.py`:
  - Read `student_reach_coverage` + `student_reach_path` from state
  - Pass to dynamic prompt format
  - Backward-compat default for sessions written before #1.3
- Modified: `config/base.yaml::mastery_scorer_dynamic`:
  - New "Reach-gate signal" section with coverage semantics
- Modified: `progress_journal/_tracker.json`

## Cost

$0 (all logic is deterministic regex/set ops, no LLM calls).

---

## Next

Tier 1 #1.4 — build the e2e driver adapting friend's 90-scenario
pattern. Now that K-of-N is in place, the driver can verify multi-
component scenarios at scale (e.g. a "the four digestive processes"
session with the student naming 2 of 4 should hit `partial_overlap` /
coverage=0.5).
