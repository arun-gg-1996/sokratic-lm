# Prompt-optimization A/B test — compact vs full TOC on L9

**Date:** 2026-05-03
**Run:** `SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/compare_topic_resolvers.py --variant ab`
**Fixture:** 16 default queries (`scripts/compare_topic_resolvers.py::DEFAULT_QUERIES`)
**Cost of run:** $0.30 (16 × 2 = 32 L9 calls)

## Result

**Compact TOC does NOT achieve accuracy parity with the full TOC on L9.**
Recommend keeping `use_compact_toc=False` (default) for L9 production.

```
=== A/B compact-vs-full agreement ===
   15 ( 93.8%) ab_different_paths
    1 (  6.2%) ab_same_path

Total input tokens: full=1,613,434  compact=636,154  ratio=0.39 (61% reduction)
Total output tokens: full=3,842     compact=3,991    (~unchanged)
```

## Why they disagree

Compact and full pick the same **chapter + section** in nearly every case
and report nearly identical confidences (0.92 vs 0.92, 0.95 vs 0.95).
But they pick different **subsections** within that section.

Without the raptor 1-line summary that the full TOC carries per leaf,
display_label alone isn't discriminative enough to distinguish sibling
subsections. Examples:

  Query: "SA node"
    full   → "...Cardiac Muscle and Electrical Activity > Conduction System of the Heart"
    compact → "...Cardiac Muscle and Electrical Activity > [different sibling]"

  Query: "wrist drop"
    full   → "...Function of Nervous Tissue > Pathologies of the Nervous Tissue"
    compact → "...Function of Nervous Tissue > Motor Pathways"

The cost savings (61% input tokens, ~$0.012/call → $0.005/call cached)
are real but the accuracy regression makes the trade-off net-negative
for the topic-lock decision — locking the wrong subsection cascades into
wrong-anchor questions for the entire downstream tutoring loop.

## Decision (per audit Step 4)

| Step | Decision |
|---|---|
| **Step 1** — Build `build_toc_block_compact()` | ✅ Shipped in commit `571fb3a`. Available for opportunistic use. |
| **Step 2** — A/B compact vs full on L9 | ✅ Run. Result: parity NOT achieved (6.2% same-path). |
| **Step 3** — Inject compact TOC into 4 high-value call sites | ⛔ DO NOT proceed for the original 4 sites until per-site accuracy verification. The display_label-only signal is too weak when the model needs to disambiguate within a section. |
| **Step 4** — Re-run e2e + Tier 1 #1.5 eval; merge if green | N/A — nothing to merge. |

## What's still worth doing

The 4 high-value call sites in the original audit were:

1. `classifiers.haiku_off_domain` — every turn
2. `dean._exploration_judge` — tangents only
3. `dean._prelock_intent_call` — 1× per session
4. `dean._prelock_refuse_call` — only on lock failure

For sites #1 (off_domain) and #2 (exploration_judge) the model is making a
**boolean / categorical** decision about whether the student input is
in-domain — NOT picking a specific subsection. Those decisions don't
need fine-grained discrimination, so the compact TOC may achieve
parity there even though it doesn't on L9.

**Recommended next step (deferred — not on critical path):** A/B compact
vs full on those two specific call sites with their own fixtures.
Different decision boundary, different result expected.

For sites #3 and #4 — both are about to be replaced by `topic_lock_v2`
in the v2 flow (already shipped under `SOKRATIC_USE_V2_FLOW=1`), so
injecting compact TOC into the legacy paths is wasted effort.

## Builder still useful

`build_toc_block_compact()` stays in `retrieval/topic_mapper_llm.py` for:

- Future per-site A/B tests (off_domain, exploration_judge)
- New call sites where subsection-granularity isn't needed
- Cost-sensitive experiments where 61% input-token savings matter
- Fallback for low-budget environments

The function is ~30 LOC, has 3 unit tests, and zero ongoing maintenance
cost.
