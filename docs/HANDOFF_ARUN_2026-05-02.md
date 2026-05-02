# Handoff for Arun — 2026-05-02

This is the second handoff doc on the takeover branch
`nidhi/reach-gate-and-override-analysis`. Pairs with the original
`docs/HANDOFF_NIDHI.md` and `docs/SETUP_NOTES_NIDHI_2026-04-30.md`.

If you're picking this up to continue, read this top-to-bottom — it
covers what changed structurally, what's running, what's broken, and
what's queued.

---

## TL;DR — what's different from when you last touched it

1. **Bedrock + Sonnet 4.6 across all roles.** The Anthropic-Direct
   key was burned out; we're on AWS Bedrock with a $100 cap. ~$70 of
   that has been spent through the eval round.
2. **Behavioral regex is gone.** Per Nidhi's "LLM-only QC" directive,
   `_LETTER_HINT_PATTERNS` (~30 patterns), `_STRONG_AFFIRM_PATTERNS`
   (~30), `_BANNED_FILLER_PREFIXES` (10), `_OFF_DOMAIN_REGEX` are all
   deleted from `conversation/dean.py`. They're replaced by three
   Haiku 4.5 classifiers in `conversation/classifiers.py`:
   `haiku_hint_leak_check`, `haiku_sycophancy_check`,
   `haiku_off_domain_check`. Validated 96.7%, 100%, 100% on a 84-case
   hand-curated test set.
3. **K-of-N partial reach.** `reached_answer_gate` now treats
   multi-component locked_answers (e.g. `"left and right coronary
   arteries"`, `"the four digestive processes"`) as K-of-N matches.
   `student_reach_coverage: float` propagates into mastery scoring.
4. **E2E harness (`scripts/run_e2e_tests.py`).** New 8-scenario
   regression suite adapted from a teammate's 90-scenario testing
   bank (saved at `docs/external_reference/`). 8/8 passing as of
   commit `18c07c9`.
5. **Corpus fixes.** Ch 20 was completely missing from
   `topic_index.json` (chunker truncated chapter_title to "Circulation"
   only); now restored. 40 callout-box junk chunks dropped (Aging /
   Disorders / Homeostatic Imbalances pedagogical sidebars). Topic
   index went from 363 → 382 entries. **All chunks are gitignored** —
   if you re-clone, run `scripts/fix_corpus_metadata_2026_05_01.py`
   followed by `scripts/drop_disorders_callout_chunks_2026_05_01.py`
   then `scripts/build_topic_index.py` then `scripts/validate_topic_index.py`
   to reproduce the corpus state.

---

## Run this first if you're catching up

```bash
cd /Users/nidhirajani/Desktop/sokratic-lm
git pull origin nidhi/reach-gate-and-override-analysis

# Verify Bedrock setup
cat .env | grep SOKRATIC_USE_BEDROCK   # should be 1
grep "claude-sonnet-4-6" config/base.yaml | head -3

# Verify e2e harness still passes
SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_e2e_tests.py
# Expected: 8/8 PASS, ~$1 cost, ~10 min

# Verify Haiku classifiers
SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/validate_classifiers.py
# Expected: hint_leak ~96%, sycophancy 100%, off_domain 100%
```

---

## Architecture summary (post-takeover)

### LangGraph nodes — 4

```
START → rapport_node → dean_node ⇄ (loops on student input)
                          ↓ (when reach=True OR hints exhausted OR turns hit cap)
                  assessment_node → memory_update_node → END
```

### Sonnet 4.6 LLM-call wrappers (high-stakes)

- `dean._prelock_intent_call` — classify student intent (greeting/topic/off-topic)
- `dean._prelock_refuse_call` — generate refuse copy when topic doesn't lock
- `dean._lock_anchors_call` — extract locked_question + locked_answer + aliases + full_answer
- `dean._hint_plan_call` — pre-generate 3-step hint progression
- `dean.reached_answer_gate` — Step A token overlap + Step B paraphrase
- `dean._setup_call` — per-turn student-state classifier
- `dean._quality_check_call` — Dean QC on Teacher draft + revision pass
- `dean._clinical_turn_call` — assessment-phase coaching feedback
- `dean._close_session_call` — end-of-session mastery + summary
- `teacher.draft_rapport / draft_socratic / draft_clinical_opt_in / draft_clinical`
- `summarizer.maybe_summarize` — old-turn compression
- `mastery_store.score_session_llm` — final mastery + grading

### Haiku 4.5 LLM-call wrappers (cheap classifiers)

- `classifiers.haiku_hint_leak_check` — letter / blank / etymology / MCQ / synonym / acronym leaks
- `classifiers.haiku_sycophancy_check` — state-aware affirmation detection
- `classifiers.haiku_off_domain_check` — substance / chitchat / jailbreak / answer_demand
- `retriever._hyde_rewrite` — HyDE rescue on weak retrieval

### Anthropic `tool_use` schemas — 4

`search_textbook`, `get_student_memory`, `update_student_memory`,
`submit_turn_evaluation` (defined in `tools/mcp_tools.py`).

### Per-turn LLM call counts

- Topic-lock turn: 7-10 calls
- Mid-tutoring turn: 5-8 calls
- Assessment turn: 2-3 calls
- Memory update: 1-2 calls

---

## What the eval (Tier 1 #1.5) showed

Full 59-conversation simulation eval across S1–S6 profiles on Bedrock+
Sonnet 4.6+Haiku classifiers stack. See
`progress_journal/2026-05-02_03-52-44_tier1_1_5_50conv_eval_results.md`
for the full writeup. Headline:

```
prof  n   topic✓   sec_hit  ch_hit   reach    cost
─────────────────────────────────────────────────────
S1    9  100.0%   66.7%    77.8%   100.0%   $0.65
S2    9  100.0%   77.8%   100.0%    77.8%   $0.80
S3   11  100.0%   45.5%    72.7%    36.4%   $1.89
S4   10  100.0%   60.0%    80.0%    90.0%   $0.90
S5   10  100.0%   40.0%    60.0%    10.0%   $1.71
S6   10  100.0%   90.0%   100.0%    90.0%   $0.62
─────────────────────────────────────────────────────
ALL  59  100.0%   62.7%    81.4%    66.1%   $1.12
```

Reach rate: 38.9% (Apr 29 baseline) → 66.1% (now). +27 pp jump.
Driven by S2 +44pp, S3 +36pp, S6 +90pp.

---

## Issues that NEED fixing (post-paper or before, your call)

### 1. Wrong-topic locks (~12% of S4/S5 convos)

The system locked the wrong CHAPTER on certain abstract phrasings.
Examples from the eval:
- S5 expected "Skeletal System" → locked "ventral horn" (spinal cord)
- S5 expected "Hip Bone" → locked "hip bone components" (close)
- S4 expected "Types of Synovial Joints" → locked "midsagittal plane"

Root cause: dean's vote-based topic resolution at `dean.py:~1340`
struggles on student-driven paraphrases that don't match TOC titles.

**Fix path**: build an LLM topic-classifier (haiku) that takes (query,
all 382 TOC entries) and returns the best match with confidence. Same
architectural pattern as the existing Haiku classifiers. ~3 hr work.
See `progress_journal/2026-05-02_03-52-44_tier1_1_5_50conv_eval_results.md`
section "Next" for details.

### 2. Empty-subsection chunks silently filtered from semantic_top

1,012 of 7,516 chunks (13.5%) have `subsection_title=""` because
OpenStax has 9 sections that don't subdivide further (overview /
development / "Nervous Tissue Mediates Perception" where synapse
content lives). Dean's vote algorithm at `dean.py:~1340` does
`if not key[2]: continue` — silently skips them.

**Fix path**: drop the skip; build path_key with empty subsection as
a valid lock target. The 9 section-level entries are already in
`topic_index.json` marked teachable. ~30 min change. **Do this before
the LLM topic-classifier above.**

Surfaced live during a manual UI test where Nidhi typed "what's the
gap between the neurons" — the synapse-defining chunk was retrieved
at score 1.0 but not voted into semantic_top because of empty
subsection_title.

### 3. Long-tail retry loops (~10% of convos)

Some convos (mostly S2, some S6) take 28-50 minutes wall and 80-300
API calls (vs ~10 calls/turn normal). They eventually complete with
reached=True, so it's "slow not broken" — but expensive and slow on
the eval.

**Fix path**: investigate one of the long-tail convo JSONs (e.g.
`data/artifacts/scaled_convo/2026-05-01T18-18-04_tier1_1_5_eval_50conv/convo_S2_seed5_*.json`)
to see what's looping. Likely Dean QC reject-and-retry on certain
prompt/anchor combos. Cap Dean retries at 3 per turn to bound the
worst case.

### 4. mem0 zombie entries (lower priority)

Earlier sessions had mem0 fragmentation issues (anchor split into
"left", "anterior", "descending", "artery"). Was wiped manually
mid-session in earlier rounds. May need a one-time cleanup script.

---

## What's been left clean

- E2E harness 8/8 green, runs in ~10 min for ~$1
- Haiku classifiers validated on 84 hand-curated cases
- 11 commits on `nidhi/reach-gate-and-override-analysis` since
  takeover, all journaled
- Corpus state: 7,516 chunks, 382 topic_index entries (374 teachable),
  Qdrant collection in sync
- 59 eval convos saved on disk for analysis / paper figures

---

## Cost ledger

```
Anthropic Direct cumulative:    ~$3.20  (pre-takeover testing)
AWS Bedrock cumulative:         ~$70.50 / $100 budget
Remaining AWS:                   ~$29.50

Suggested allocation of remaining:
  - 8-10 production sessions for paper transcripts:  ~$8-10
  - 1-2 retest runs after section-level lock fix:    ~$8-10
  - LLM topic-classifier validation + harness rerun: ~$5
  - Buffer:                                          ~$5-8
```

---

## Pointer files

- `progress_journal/_tracker.json` — index of all journal entries
- `progress_journal/2026-04-30_*.md` — Apr 30 baseline + Cluster fixes
- `progress_journal/2026-05-01_*.md` — full series of takeover work
- `progress_journal/2026-05-02_*.md` — eval results
- `docs/HANDOFF_NIDHI.md` — original handoff (Apr 30)
- `docs/SETUP_NOTES_NIDHI_2026-04-30.md` — clean-clone setup walkthrough
- `docs/external_reference/` — teammate's 90-scenario testing bank

---

## Branch state

Branch: `nidhi/reach-gate-and-override-analysis`
Last commit: see `git log --oneline -5`
Push status: pushed to `origin/nidhi/reach-gate-and-override-analysis`
Merge to main: NOT merged yet — Nidhi planning to merge after the
section-level lock fix + paper.
