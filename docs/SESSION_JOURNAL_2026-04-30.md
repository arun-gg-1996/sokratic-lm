# Session Journal — 2026-04-30

Captures decisions, rationale, and the phased plan agreed on this date.
Companion to `docs/EVALUATION_FRAMEWORK.md` and `docs/HANDOFF_NIDHI.md`.

---

## Context entering this session

Prior changes already shipped + validated:

| Change | What | Validated by |
|---|---|---|
| **Change 1** | reached_answer_gate (token-overlap Step A + LLM Step B with quote-the-student) | T1, T2, T3, T4 + gravity export regression |
| **Change 2** | topic-lock acknowledgement turn (deterministic ack message stating topic + locked_question before any hints) | T1–T4 + sidebar UI |
| **Change 3** | dean_setup_classify tightening + mastery scorer attribution rules | Re-ran gravity, T6/T7 |
| **Eval framework** | 10 secondary dims + EULER + RAGAS + penalty channel; CLI scorer | Gravity baseline + T1–T7 scores |
| **Change 4** | counter system (help_abuse_count, off_topic_count, telemetry totals), strike warnings 1–3, hint advance at strike 4, off-topic terminate at strike 4 | T6 (cap fired hint 0→1), T11 (off-domain), T12 (B vs C distinction) |
| **Change 5.1** | clinical-phase counters (clinical_low_effort_count, clinical_off_topic_count) capping at threshold 2 (ends clinical phase only, not session) | scaffolded; deferred test |
| **Audit cleanup** | dropped sycophancy_risk + generic_filler regex from `_deterministic_tutoring_check` (LLM QC subsumes them) | T1–T4 regression pass |

Total LLM cost across all validation runs: ~$1.50.

---

## Decisions made this session

### 1. Hint system architecture — Change 6 (queued for Phase 1)

**Decision: hybrid — dimensional hints at lock-time, dynamic SELECTION per turn (not regeneration).**

Three options were considered:
- A. Pure adaptive (regenerate hint each turn) — too costly, fragile
- B. More hints, same progressive directness (5 vs 3) — marginal gain
- **C. Dimensional hints + dynamic selection ← chosen**

**Concrete v2 design:**

At lock-time, `_hint_plan_call` generates **4–5 dimensional hint angles** (not progressive levels):
- `structure` — anatomical/spatial scaffold
- `function` — what it does
- `mechanism` — how it works
- `analogy` — real-world parallel
- `clinical` — when it matters

Per turn, the dean picks WHICH angle based on what the student tried (avoid repeating angles they failed). Teacher renders that angle's scaffold as the question.

**Cost:** lock-time prompt slightly bigger (negligible). Per-turn selection is deterministic + 1 line in dean_critique. **No extra LLM cost per turn.**

Plus: hint indicator in tutor response ("— Hint 2 of 3 —") for gamification urgency.

### 2. Chunk window — DO NOT increase

Current settings:
- `qdrant_top_k: 20`, `bm25_top_k: 20`, `top_chunks_final: 7`
- Window expansion: surrounding sentences added per chunk for context

**Eval baselines tell the story:**
- `context_recall: 1.00` — we have the info
- `context_precision: 0.40–0.60` — sometimes noisy

We have **recall, not precision.** Increasing the window adds noise without adding info. The fix is better reranking (cross-encoder already in place) or tighter section filtering (already done via `locked_topic.subsection`). **Skip increasing window.**

### 3. Counter & gamification design (Change 4 — already shipped)

- `help_abuse_threshold = 4` (reverted from 2). Behavior at threshold: **advance hint** with LLM-narrated transition. Strikes 1–3 emit warnings via dean_critique.
- `off_topic_threshold = 4`. Behavior at threshold: **terminate session** (core_mastery_tier=clinical_mastery_tier="not_assessed").
- Counters reset on engaged turns. Telemetry counters (`total_low_effort_turns`, `total_off_topic_turns`) never reset; mastery scorer reads them.
- Categories distinguished:
  - **A** on-topic attempt → counters reset
  - **B** domain-tangential (handled by exploration_judge) → no counter increment
  - **C** off-domain (vaping, profanity, etc.) → off_topic_count++

### 4. CRAG / propositions — DEFER

- **Propositions deprecated**: broke chunks into atomic factoids, lost narrative flow. Anatomy needs context. Chunks won.
- **CRAG** (LLM judges chunk quality post-retrieval, fall back to alternate sources): partially implemented as **HyDE rescue** (parallelized via ThreadPoolExecutor when section-filtered retrieval returns nothing). Full CRAG with web-search fallback **deferred — out of scope for thesis.**

### 5. UI changes (Phase 1)

- ✅ Sidebar conversation-health pills (just shipped: help-abuse, off-topic, total telemetry)
- ⏳ Locked-topic display: make it a **collapsible details** with chapter + section + subsection visible
- ⏳ Activity log: surface dean wrapper / tool calls (Claude-style "tool call X, Y, Z") for granular debugging

### 6. Sonnet vs Haiku — Nidhi A/B test

My recommendation as input to Nidhi:
- **Dean → Sonnet 4.6**: highest-stakes calls (classification + QC). Sonnet would tighten leak detection + student_state accuracy.
- **Teacher → keep Haiku**: high-frequency, lower-stakes per call.
- **Mastery scorer → Sonnet 4.6**: 1 call/session, attribution grounding matters.
- **Evaluator (eval framework) → keep Haiku**: 4 calls/session, cost-sensitive.

Nidhi runs A/B on 10 sessions per config and compares EULER + ARC scores.

### 7. Secondary textbook constraint

Must tie to existing OpenStax topic_index structure. Don't add arbitrary topics. Suggested overlapping-structure textbooks: Tortora (Principles of A&P) or Marieb (Human A&P). Maps subsection-to-subsection with the existing index.

### 8. Vision model flow — Nidhi

**Decision: Sonnet 4.6 for vision** (Haiku doesn't support vision well). Already partially scaffolded: `state.is_multimodal`, `state.image_structures`. Nidhi adds the actual flow that handles uploaded diagrams.

---

## Phase plan agreed (deadline: Sunday)

### Phase 1 — me, this session (~next hour)

1. Journal (this doc) — DONE
2. `docs/HANDOFF_NIDHI.md` (so Nidhi can start in parallel) — DONE
3. **T5** (rapport-phase off-topic deflection) — DEFERRED to Nidhi manual eval (existing harness assumes prelock; T5 needs no-prelock variant; Nidhi will catch this naturally in 10 manual conversations)
4. **T8** (mastery attribution end-to-end) — RAN, but session terminated at hint_level=3 (max_hints) without reaching memory_update_node, so mastery_scorer never invoked. T8 PASSED at the gate level (reached=False, no fabrication keywords) but DID NOT validate Change 3C end-to-end. **Nidhi must re-run T8 with extended turns** to push past hint_level=max_hints+1.
5. **UI**: collapsible locked-topic — DONE (`Sidebar.tsx`)
6. **UI**: surface reached-gate decision in activity log ("Checking if your message reached the answer" → "Answer recognized (matched your wording)" / "Answer not yet reached, continuing") — DONE
7. **Change 6 partial**: hint indicator in tutor response ("— Hint 2 of 3 —" / "— Last hint (3 of 3) — give it your best try. —") — DONE. Suppressed on topic-ack turns and post-cap turns to avoid double-tagging. **Dimensional hints (the larger Change 6 piece) DEFERRED** — too invasive for the time budget; queued as future work.
8. **T9 + T10** — DEFERRED to Nidhi sim eval (T9 partly covered by T12; T10 covered by Nidhi's natural Sonnet A/B + multi-session work).

### Phase 2 — split

- Vision model flow → **Nidhi**
- README + architecture docs → **User**

### Phase 3 — Nidhi (Sat–Sun)

1. 10 manual conversations + score via `scripts/score_conversation_quality.py`
2. 50–60 simulation conversations across S1–S6 profiles via `scripts/run_final_convos.py`
3. Sonnet vs Haiku A/B per recommendation above
4. Secondary textbook integration (constraint: overlap existing topic_index)
5. Eval reporting + writeup

### Phase 4 — deferred (only if time permits)

- Change 5.2 (engagement-reset narration)
- Change 5.3 (dean_retry_count surfacing)
- T13 (clinical-cap test — needs happy-path tutoring run)
- Teacher reveal-on-hint-advance prompt tightening
- Full CRAG with web-search fallback

---

## Open questions / signals to monitor

1. **EULER nondeterminism**: gravity export EULER scores moved 1.00→0.75 between runs (same data). Acceptable noise but worth tracking across Nidhi's eval batch.
2. **Teacher reveal on hint advance**: T6 final tutor message named the answer ("SA node (sinoatrial node)"). The dean's narration brief said "deliver the new hint" — teacher took that too far. Prompt tightening needed but separate from Change 4 mechanism.
3. **`context_precision` baseline 0.40–0.60**: lower than ideal. Cross-encoder reranking should help; investigate post-Phase-3 if Nidhi's batch shows the same.
4. **Help-abuse threshold revert**: now 4 (with warnings + hint advance). Validate with Nidhi's batch that this isn't too lenient on real students.

---

## Cost ledger

| Phase | Cost so far | Estimated remaining |
|---|---|---|
| Changes 1–5.1 + eval framework | ~$1.50 | — |
| Phase 1 (this session) | — | ~$1.50 |
| Phase 2 (Nidhi vision + user docs) | — | ~$1–2 |
| Phase 3 (Nidhi full eval) | — | ~$5–10 |
| **Estimated total through Sunday** | | **~$10–15** |
