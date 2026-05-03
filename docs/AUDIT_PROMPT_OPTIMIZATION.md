# Prompt-Optimization Audit (TOC injection + caching coverage)

**Date:** 2026-05-02
**Status:** AUDIT ONLY — no code changes. Implement after main rebuild
(tracks 1–5) lands.

Two parallel questions:

1. **Where besides L9 would injecting the TOC (or a compact TOC) improve
   accuracy?** — i.e. where is the model guessing about "what's in this
   textbook?" when it could just read.
2. **Where is Bedrock prompt caching NOT being used today?** — every
   uncached system prompt sent every turn is wasted spend.

---

## Part 1 — TOC injection opportunities

For each LLM call site, asking: would seeing the textbook's table of
contents (or a 4× smaller "compact" version with just paths +
display_labels, no summaries) help the model get a more accurate
answer?

The compact TOC is ~25K tokens (1,070 entries × ~24 chars). Versus the
full TOC's 102K tokens.

### High value — recommended for compact-TOC injection

| Call site | Current pain | TOC helps because | Effort |
|---|---|---|---|
| **dean._exploration_judge** (when tangent detected) | Must judge "is this tangent in-domain or off-topic" without seeing the domain's TOC. Can over-refuse legitimate side-questions or accept off-topic ones. | Compact TOC tells model exactly what's covered → cleaner in/out-of-scope decision. | Low (1 prompt edit) |
| **classifiers.haiku_off_domain** (every turn, ~$0.001/call) | Decides substance / chitchat / jailbreak / answer-demand. Currently no domain awareness — relies on the term "anatomy" being in the system prompt. | Compact TOC anchors the "domain" concretely → fewer false positives on legit anatomy phrasings model hasn't seen. | Low (system block extension) |
| **dean._prelock_intent_call** (1× per session) | Classifies the student's first message as greeting / topic-request / off-topic. Without TOC, an unusual phrasing of a real topic ("the squishy part above the kidney") might get classified off-topic. | Compact TOC makes "is this in our textbook?" cheap to verify. | Low |
| **dean._prelock_refuse_call** (when topic doesn't lock) | Generates the refusal copy + suggested alternatives. Today: alternatives come from `topic_matcher.sample_diverse` (random within domain). | Could ask LLM "given the student typed X and we couldn't lock, suggest 3 closest TOC entries" — sharper, more relevant suggestions. | Medium (refactor flow) |

### Medium value — would help but more careful tradeoff

| Call site | Why TOC could help | Why I'd skip for now |
|---|---|---|
| **teacher.draft_rapport** (1× at start) | Could surface 2-3 specific topic suggestions in the opener | Already has weak_topics + past_memories input. Adding TOC = $0.003/session uplift for marginal greeting quality. Low priority. |
| **dean._classify_complexity** (every turn) | Better simple/tangential/complex discrimination | Marginal. Current classifier works. Cost would be 25 turns × $0.003 = $0.07/session. Not worth it. |
| **retriever._hyde_rewrite** (HyDE rescue, when needed) | Knowing what's in the textbook could improve hypothetical-passage rewrites | Hard to measure win without an A/B. Already optional. |

### No value — TOC injection wouldn't help

Skip these — they operate on already-locked content:
- `dean._lock_anchors_call` — operates on chunks of one subsection
- `dean._setup_call` — classifies a single student turn
- `dean._hint_plan_call` — generates hints for known answer
- `dean._reached_check_llm` — answer-matching against known answer
- `dean._quality_check_call` — judges teacher's draft against known answer
- `dean._clinical_turn_call` / `dean._close_session_call` — operate on locked topic
- `teacher.draft_socratic` / `draft_clinical*` — operate on locked content
- `classifiers.haiku_hint_leak` — judges leak against known answer
- `classifiers.haiku_sycophancy` — judges affirmation tone (no TOC needed)
- `summarizer` — compresses turn pairs (no TOC needed)
- `memory_manager` extraction — one-off extraction of specific signals
- `mastery_store.score_session_llm` — scores against known locked subsection

---

## Part 2 — Prompt-caching coverage

### Already cached today (verified)

| Module | What's cached | Threshold |
|---|---|---|
| `conversation/dean.py::_cached_system` | Block 1: role + wrapper_delta + chunks (stable across turns); Block 2: history (cached when ≥1500 tokens) | 1500 tokens |
| `conversation/teacher.py::_cached_system` | Same 2-block pattern | 1500 tokens |
| `conversation/classifiers.py::_cached_system_block` | Static system prompt for each Haiku classifier (~1K tokens each) | Wraps unconditionally; Bedrock silently ignores below threshold |
| `retrieval/topic_mapper_llm.py` (just added) | Header + TOC + abbreviations as one ephemeral block | 100K tokens — well above any threshold |
| `ingestion/core/propositions_dual.py` | Few-shot example block during ingestion | (not runtime-critical) |

### Not yet cached — opportunities

| Module | Why not cached | Cacheable portion | Estimated savings / session |
|---|---|---|---|
| `retrieval/retriever.py::_hyde_rewrite` | Single one-shot call, no system block | System prompt (`hyde_reformulate` template, 903 chars / ~225 tokens) | Below threshold — caching wouldn't help |
| `memory/mastery_store.py::score_session_llm` | One-shot per session; system prompt ~5K chars / ~1.3K tokens | System prompt (`mastery_scorer_static`) | $0.002/session (Sonnet, 1× call) — small but free |
| `memory/memory_manager.py` mem0 extraction | Will be rewritten in Track 3 (L4-L6). System prompts here are throwaway. | n/a | Will be designed with caching from the start |
| `conversation/summarizer.py` | Each call summarizes one turn-pair; system prompt is small (~345 chars) | Below threshold | n/a |
| `simulation/student_simulator.py` | Eval-only path, not production | n/a | n/a |

### Worth doing in production

The big runtime calls (dean / teacher / classifiers) are already cached.
The remaining opportunities are 1-shot calls that fire once per session
(rapport, score_session, close_session) — collectively maybe $0.01
savings per session at scale. **Not urgent.**

The *real* gap is L9 — which I just added in track 2.2.

---

## Part 3 — Recommended implementation order (after main tracks land)

Once tracks 1–5 are merged, batch these as one optimization commit:

### Step 1: Build `build_toc_block_compact()` (paths + labels only)

```python
def build_toc_block_compact(topic_index_path: Path) -> str:
    """Compact variant: <chapter> > <section> > <subsection>: <display_label>
    No summaries. ~25K tokens vs 102K for the full version."""
```

Estimated size: 1,070 entries × ~75 chars per line ≈ 80K chars / 20K tokens.
4× smaller than full TOC.

### Step 2: A/B test compact vs full on the L9 comparison harness

Run `scripts/compare_topic_resolvers.py` with both variants. If compact
matches full on accuracy, ship compact as the default. Save ~75% on
every L9 call.

### Step 3: Inject compact TOC into the 4 call sites flagged "high value"

Order by ROI:
1. `classifiers.haiku_off_domain` — every turn, biggest aggregate impact
2. `dean._exploration_judge` — fires on tangents only
3. `dean._prelock_intent_call` — 1× per session
4. `dean._prelock_refuse_call` — only on lock failure

Each gets the compact TOC as a cached system block (the TOC is identical
across calls → high cache hit rate). Per-call uplift: ~$0.003 (cached).

### Step 4: Re-run e2e suite + Tier 1 #1.5 eval

Verify (a) accuracy didn't regress, (b) cost didn't balloon. If both
green, merge. Otherwise revert specific call sites.

---

## Bottom line

- **L9 already does the right thing** — full TOC, prompt-cached.
  Compact-TOC is a future optimization, not a regression.
- **dean.py + teacher.py + classifiers already cache aggressively.**
  Nothing urgent to fix.
- **The 4 high-value TOC-injection sites would meaningfully improve
  domain-awareness** in classifiers + tangent judgment + prelock
  intent. Should land as one focused commit after main rebuild.
- **Sonnet 4.5 vs 4.6 caching:** both supported on Bedrock with same
  cache_control API. No reason to prefer one over the other for
  caching reasons.

Ship after tracks 1–5 are merged.
