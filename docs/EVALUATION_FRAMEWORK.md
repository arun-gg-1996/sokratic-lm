# Conversation Quality Evaluation Framework

**Status:** design (2026-04-29) — pending Phase 1a implementation
**Owner:** Sokratic CSE 635 NLP thesis (UB)
**Scope:** end-to-end quality measurement for tutoring sessions, not just response-level checks.

---

## 1. Philosophy

**Dimensional scoring, not a single composite.** A tutoring session can be excellent on retrieval but mediocre on pedagogy — collapsing those into one number hides the signal we care about. Each dimension below stands on its own, and a session is only "passing" if it clears the **per-dimension thresholds we set**, not because some weighted average is high.

**Primary vs Secondary hierarchy.** Evaluations are organized in two tiers:

- **Primary (headline) metrics** — the standard, citable frameworks the thesis reports against. These are what go into the main results table and are compared head-to-head with prior work.
  1. **EULER** — Sokratic's 4-criterion tutoring-response evaluation (`question_present`, `relevance`, `helpful`, `no_reveal`). Already implemented.
  2. **RAGAS** — standard RAG evaluation suite (`context_precision`, `context_recall`, `faithfulness`, `answer_relevancy`, etc.). To be added.
  3. *(reserved)* — any additional framework we adopt later (e.g. BERTScore for response quality, human eval) goes here.

- **Secondary (diagnostic) metrics** — the 10 internal flow-specific dimensions (TLQ, RRQ, AQ, TRQ, RGC, PP, ARC, CC, CE, MSC). They tell us *which code path* failed when a primary metric drops. They live in the eval pipeline alongside primary metrics but aren't the headline numbers.

The relationship: **primary metrics describe *what* went wrong** (response unfaithful, retrieval imprecise, etc.); **secondary metrics describe *why*** (which dean wrapper produced bad output, which sanitization wiped which anchor, etc.).

Every metric — primary or secondary — must be:
1. **Grounded in real flow logic** — anchored to a specific `turn_trace` wrapper, `state["debug"]` field, or output of a named function in the codebase. No invented signals.
2. **Computable from a saved session JSON** — no live system calls during evaluation. The exporter writes everything we need at session end.
3. **Diagnostic when it fails** — a low score tells us *what* (primary) or *which code path* (secondary) to investigate, not just "session was bad."

---

## 2. Secondary (Diagnostic): The 10 Internal Dimensions

These are **not headline numbers**. They diagnose *why* a primary metric (EULER, RAGAS) moved. Each is scored 0.0–1.0. Sub-scores are also reported so we can drill down.

### TLQ — Topic Lock Quality

**Measures:** how cleanly the session moved from free-text input to a confirmed, grounded topic anchor.

| Sub-metric | Source | Formula |
|---|---|---|
| `intent_match` | `dean.prelock_intent.intent` | 1.0 if classified intent matches test ground-truth label; 0.0 otherwise. (Free runs: skip.) |
| `match_top_score` | `dean.topic_match.top_score` | `top_score / 100` (already a 0–100 float). |
| `vote_consensus` | `dean.topic_vote.{winner_weight, all_weights}` | `winner_weight / sum(all_weights)`. High = unambiguous winner. |
| `coverage_gate_pass` | `state.debug.coverage_gap_events`, lock attempts | `1 - (gate_events / lock_attempts)`. |
| `anchor_extraction_success` | `dean.anchors_locked` vs `dean.anchor_extraction_failed` | 1.0 if anchors_locked fired without anchor_extraction_failed first; 0.0 if the latter dominated. |
| `repair_clean` | presence of `dean._lock_anchors_repair_call` | 1.0 if repair NOT invoked; 0.7 if repair fixed it; 0.0 if repair also failed. |

**Aggregate:** mean of sub-scores.

**Failure modes:** low TLQ → topic resolution drifted (e.g. "Conduction System" matched "DNA Replication"), or anchor LLM produced sentence-form answers that got wiped.

---

### RRQ — RAG Retrieval Quality

**Measures:** quality of retrieved evidence used to ground the locked answer.

| Sub-metric | Source | Formula |
|---|---|---|
| `chunk_count_adequacy` | `dean.python_retrieval.retrieval` length | `min(1.0, chunk_count / 5)`. |
| `mean_top3_score` | per-chunk `score` field | mean of top-3 chunk scores. |
| `section_match_rate` | each chunk's `subsection_title` vs `locked_topic.subsection` | % equal. |
| `retrieval_calls_efficiency` | `state.debug.retrieval_calls` | `1.0 if == 1 else 1.0 / retrieval_calls`. |
| `coverage_gap_frequency` | `state.debug.coverage_gap_events`, total lock attempts | `1 - (events / attempts)`. |
| `exploration_appropriateness` | `dean.exploration_judge.needed`, `complexity_tier` | 1.0 if exploration only fired on tangential queries (where tier=tangential); 0.5 if fired off-tier; 1.0 if never invoked. |

**Failure modes:** chunks not from locked subsection (drift), retrieval re-run on same lock (signals retrieval guard malfunction).

---

### AQ — Anchor Quality

**Measures:** the locked question + locked answer + aliases as artifacts.

| Sub-metric | Source | Formula |
|---|---|---|
| `groundedness` | `dean.sanitize_locked_answer.action` | 1.0 if "kept", 0.5 if "kept" after one repair, 0.0 if "wiped_*" with no recovery. |
| `answer_brevity` | `locked_answer` word count | 1.0 if 2–5 words, 0.7 if 6–10, 0.3 if >10. |
| `answer_is_noun_phrase` | regex/POS check on `locked_answer` | 1.0 if no verbs/sentence markers, 0.0 otherwise (mirrors sanitizer's heuristics but as score). |
| `aliases_count_adequacy` | `locked_answer_aliases` length | `min(1.0, len / 4)`. |
| `aliases_diversity` | edit-distance / lexical-root analysis between aliases | 1.0 if all distinct lexical roots; lower for near-duplicates (e.g. "calf muscle pump", "calf pump" share roots). |
| `question_specificity` | `locked_question` heuristics | 1.0 if (ends with "?", contains a noun phrase, ≤ 25 words). |

**Failure modes:** locked_answer too long (sentence form), aliases redundant or missing, locked_question vague.

---

### TRQ — Tutor Response Quality (incorporates **EULER**)

**Measures:** per-turn correctness of the teacher's drafted message.

This is where the **EULER framework** lives:

| EULER criterion | Source | Notes |
|---|---|---|
| `question_present` | `_quality_check_call` result | Binary; exception 1.0 in assessment/summary phases. |
| `relevance` | `_quality_check_call` result | 0.0–1.0; on-topic + addresses student's last message. |
| `helpful` | `_quality_check_call` result | 0.0–1.0; meaningfully advances reasoning. |
| `no_reveal` | `_quality_check_call.leak_detected` | Binary; 1.0 if locked_answer not revealed verbatim. |

**Plus deterministic checks** from `_deterministic_tutoring_check`:

| Sub-metric | Source | Formula |
|---|---|---|
| `qc_pass_rate` | `dean._quality_check_call.decision_effect` per turn | % of turns with "qc_pass" (no rewrite needed). |
| `revised_draft_rate` | `dean.revised_teacher_draft_applied` | 1 − (% of turns with revisions). |
| `intervention_rate` | `state.debug.interventions` | 1 − (interventions / tutoring_turns). |
| `repetition_score` | tutor messages | 1 − max cosine similarity to prior 3 tutor questions. |
| `reason_codes_clean_rate` | `_deterministic_tutoring_check.reason_codes` | % of turns with empty reason_codes (no missing_question, multi_question, reveal_risk, sycophancy_risk, etc.). |

**Aggregate:** weighted mean of EULER (0.5 of TRQ) + the deterministic block (0.5).

**Failure modes:** sycophancy on incorrect answers, multi-question or zero-question drafts, locked_answer leaks.

---

### RGC — Reached-Gate Correctness

**Measures:** whether `reached_answer_gate` (Change 1) made the right call per turn.

| Sub-metric | Source | Formula |
|---|---|---|
| `false_positive_rate` | `dean.reached_answer_gate.path` + state.locked_answer/aliases + student msg | Count: `reached=True` ∧ no token overlap with answer/aliases ∧ no verbatim quote in evidence ÷ total reached=True. **Should be 0 post-Change-1.** |
| `false_negative_rate` | same | Count: student message contains alias content tokens (no hedge) ∧ `reached=False` ÷ total reached=False. |
| `evidence_quote_validity` | `gate_evidence` field | % of paraphrase-path entries where evidence is a verbatim substring of the student message. |
| `gate_path_distribution` | `dean.reached_answer_gate.path` | histogram across {overlap, paraphrase, no_overlap_no_paraphrase, hedge_block, no_lock, llm_no_quote, llm_parse_fail, llm_error}. Diagnostic, not scored. |

**Failure modes:** the gravity bug returning (false positive), or a literal-statement going unrecognized (false negative).

---

### PP — Pedagogical Progression

**Measures:** the trajectory of student understanding across turns.

| Sub-metric | Source | Formula |
|---|---|---|
| `hint_progression_monotonic` | `state.debug.hint_progress` entries | 1.0 if hint_level only increases over the session; lower if oscillating. |
| `hint_utilization` | final `hint_level`, `max_hints` | `min(1.0, final_hint_level / max_hints)` IF reached; else (max_hints - final_hint_level) / max_hints inverse penalty (used too few before timeout). |
| `student_engagement_rate` | per-turn `student_state` | 1 − (low_effort + irrelevant turns / total tutoring turns). |
| `state_trajectory_score` | sequence of `student_state` values | maps {incorrect→0, partial→0.5, correct→1} and computes ordinary least squares slope; positive slope = good. |
| `mastery_confidence_delta` | `student_mastery_confidence` first vs last | clipped to [0,1]; positive delta means session improved running confidence. |

**Failure modes:** student stuck in low_effort loop (no real progression), or `hint_level` jumped from 1 to 3 (skipping plan[1]).

---

### ARC — Answer-Reach vs Step-Correctness *(Change 3 focus)*

**Measures:** whether the system correctly distinguishes "student answered an intermediate scaffold" from "student stated the locked answer."

This is what the **"mine goes lower" pattern** exposes. The student answers a scaffold question correctly (intermediate), but the *locked* answer is something else. The mastery scorer must not credit the student for insights only the tutor revealed.

| Sub-metric | Source | Formula |
|---|---|---|
| `step_vs_reach_disambiguation` | turns where `student_state=correct` ∧ `reached=False` | % of those turns where the *next* tutor message correctly continues toward the locked answer (not falsely confirms). Heuristic: tutor message must NOT contain "you've identified", "you reached", "correctly identified" etc. |
| `low_effort_help_resistance` | `state.debug.help_abuse_count`, tutor message content | When help_abuse ≥ threshold, did the tutor cap (no further hint scaffolding) or did it give away the answer? Score 1.0 if capped, 0.0 if explanation given. |
| `mastery_attribution_grounding` | `score_session_llm.rationale` text vs all `messages` of role=student | LLM-judged: does the rationale's specific claims appear in **student** messages, or only in tutor messages? Score = % of rationale claims that appear in student utterances. |
| `intermediate_credit_avoidance` | turns w/ `student_state=correct` AND no overlap with locked_answer/aliases | % of those turns where the tutor's response acknowledges progress without claiming reach. |

**Failure modes:** the gravity transcript pattern — tutor says "you correctly identified that stroke volume drops" when the student never actually said it.

---

### CC — Conversation Continuity

**Measures:** invariants that should hold across the session.

| Sub-metric | Source | Formula |
|---|---|---|
| `topic_stability` | `locked_topic_snapshot` vs subsequent `locked_topic` | 1.0 if locked_topic.subsection unchanged from snapshot; 0.0 otherwise. |
| `anchor_stability` | `locked_question`, `locked_answer` per turn | 1.0 if both unchanged after first lock. |
| `phase_transitions_legal` | sequence of `phase` values across `all_turn_traces` | 1.0 if sequence ⊆ rapport→tutoring→assessment→memory_update; 0.0 if any illegal jump. |
| `invariant_violations` | `state.debug.invariant_violations` count | 1 − (violations / total_turns). |

**Failure modes:** locked_topic mutated mid-session, phase transitioned backward.

---

### CE — Cost & Efficiency

**Measures:** token economy.

| Sub-metric | Source | Formula |
|---|---|---|
| `cost_per_useful_turn` | `state.debug.cost_usd`, non-rapport non-fallback turn count | `cost_usd / useful_turns`. Lower = better. Score = `1 − min(1.0, cost / target=$0.20)`. |
| `cache_hit_rate` | per-call `cache_read`, `cache_write` totals | `total_cache_read / (total_cache_read + total_input_tokens)`. |
| `mean_turn_latency` | per-call `elapsed_s` | mean across all `_timed_create` entries. Score = `1 − min(1.0, mean_s / target=15s)`. |

**Failure modes:** session burned $1+ in tokens, cache misses on repeated lock attempts.

---

### MSC — Mastery Scoring Calibration

**Measures:** whether the per-concept mastery update was reasonable.

| Sub-metric | Source | Formula |
|---|---|---|
| `rationale_grounding` | `score_session_llm.rationale` | LLM-judged grounding score: do rationale claims map to actual session events? Higher = more grounded. |
| `confidence_appropriateness` | mastery, confidence values vs session length, `student_reached_answer` | Heuristic: if reached=False AND confidence>0.7 → penalize (overclaim). If reached=True AND confidence<0.4 → penalize (underclaim). |
| `EWMA_movement_appropriate` | prior mastery vs new mastery, EWMA blend | `1 - abs(blended - expected)` where expected is computed from reached + state trajectory. |

**Failure modes:** session ended in failure but mastery_store recorded high confidence.

---

## 3. Primary (Headline) Frameworks

These are the citable, standardized metrics that go into the thesis results table. **A session's primary report is EULER + RAGAS scores**, with the secondary dimensions attached as diagnostic context.

### 3.1 EULER (existing — Sokratic's framework)

Already implemented in `scripts/euler_eval.py` and `scripts/score_euler.py`. The 4 criteria are scored per tutor turn:

| Criterion | Range | Scope |
|---|---|---|
| `question_present` | binary 0/1 | Tutor message ends with exactly one question (exception: assessment/summary phases). |
| `relevance` | 0.0–1.0 | Addresses student's last message and is on-topic for locked_question. |
| `helpful` | 0.0–1.0 | Meaningfully advances student reasoning; isn't generic filler. |
| `no_reveal` | binary 0/1 | locked_answer not exposed via direct naming, one-hop inference, or sycophantic confirmation. |

**Pass thresholds (per existing convention):**
- `question_present ≥ 0.95` (per session, mean across tutoring turns)
- `relevance ≥ 0.70`
- `helpful ≥ 0.70`
- `no_reveal ≥ 0.95`

**Reported as:** mean per criterion across all tutoring turns in the session, plus the binary "did this session pass all 4 thresholds" flag.

**Cross-link to secondary dims:** EULER scores live inside TRQ.EULER_block (we copy them, don't duplicate computation).

### 3.2 RAGAS (new — to be added)

[RAGAS](https://docs.ragas.io) provides standardized RAG evaluation metrics. Computed per session against the locked_question, retrieved chunks, locked_answer, and tutor responses.

| RAGAS metric | Range | Scope | Computation against our session JSON |
|---|---|---|---|
| `context_precision` | 0.0–1.0 | retrieval | Of the chunks in `dean.python_retrieval.retrieval`, what fraction are relevant to the locked_question? |
| `context_recall` | 0.0–1.0 | retrieval | Did the chunks contain enough information to derive the locked_answer? Per-test ground-truth required. |
| `context_relevancy` | 0.0–1.0 | retrieval | Per-chunk relevance to the locked_question; mean across chunks. |
| `faithfulness` | 0.0–1.0 | generation | Are tutor-response statements entailed by the retrieved chunks? Per-turn LLM check, averaged. |
| `answer_relevancy` | 0.0–1.0 | generation | Is each tutor question relevant to the locked_question? Cosine sim or LLM-judged. |
| `answer_correctness` | 0.0–1.0 | generation | Only when `student_reached_answer=True`: does the student's stated answer match locked_answer? Second-opinion to our gate. |

**Pass thresholds (proposed; refine after baseline):**
- `context_precision ≥ 0.80`
- `context_recall ≥ 0.70`
- `faithfulness ≥ 0.85`
- `answer_relevancy ≥ 0.75`

**Cross-link to secondary dims:** retrieval-side RAGAS metrics inform RRQ; generation-side metrics inform TRQ + AQ; `answer_correctness` cross-checks RGC.

**Implementation:** add `scripts/score_ragas.py` using either the `ragas` Python package or hand-rolled equivalent prompts via Anthropic API for cost control. Decision deferred — see Section 8.

### 3.3 Reserved slots for future primary frameworks

Possible additions (not committed):
- **BERTScore** for tutor response semantic similarity to ground-truth ideal responses (requires reference set).
- **Human eval** — periodic blind review of N sessions per week.
- **Mastery-vs-ground-truth** — if we build a labeled set where each session has a known "actual" mastery outcome.

These would each get their own subsection here when adopted.

---

### 3.4 Why this hierarchy?

EULER scores the *tutor's pedagogical quality* (response-level). RAGAS scores the *retrieval grounding and answer correctness* (RAG-pipeline level). Together they're the two-axis primary report.

The 10 secondary dimensions add *flow-specific signals* that neither framework captures (topic lock quality, hint progression, the ARC step-vs-reach distinction, etc.). When EULER `helpful` drops, the secondary dims tell us whether the cause was retrieval (RRQ), anchor lock (AQ), gate misjudgment (RGC), or something else.

**The thesis reports primaries.** Diagnostics support the narrative when something moves.

---

## 4. Penalties (Hard Fails)

These are **separate from dimension scores** — they flag a session for manual review regardless of how the dimensions look.

| Penalty | Trigger | Severity |
|---|---|---|
| `LEAK_DETECTED` | `_quality_check_call.leak_detected = true` AND tutor message contained locked_answer verbatim | **Critical** |
| `INVARIANT_VIOLATION` | any entry in `state.debug.invariant_violations` (e.g. tutoring_without_anchors) | **Critical** |
| `FABRICATION_AT_REACHED_FALSE` | `reached=False` for a turn AND tutor message contains "you've identified" / "correctly identified" / "great job, the answer is" / etc. | **Critical** |
| `OFF_TOPIC_DRIFT_NOT_REDIRECTED` | student message contains off-topic markers AND tutor response engages with the off-topic content | **Major** |
| `MASTERY_OVERCLAIM` | `score_session_llm.mastery > 0.7` AND `student_reached_answer=False` AND no clinical correctness | **Major** |
| `HELP_ABUSE_RESPONDED_WITH_ANSWER` | `help_abuse_count >= threshold` AND tutor message reveals or paraphrases locked_answer | **Major** |

A session with any **Critical** penalty is reported as *failed* in the dashboard regardless of dimension scores.

---

## 5. Test Scenario Coverage Matrix

| Test ID | Description | Stages exercised | Dimensions exercised |
|---|---|---|---|
| **T1** | Wrong-answer (AV node) regression | 4, 7 | TRQ, RGC, ARC |
| **T2** | Direct-statement positive (SA node) | 7 | RGC.overlap, TRQ |
| **T3** | Paraphrase positive (Step B LLM) | 7 | RGC.paraphrase, TRQ |
| **T4** | Gravity false-positive regression | 7 | RGC.fp, ARC |
| **T5** | Off-topic deflection (vape, etc.) | 1, 2 | TLQ, penalty checks |
| **T6** | Low-effort spam | 7 | ARC.help_abuse_response, PP.engagement |
| **T7** | Intermediate-question trap ("mine goes lower") | 7 | ARC.step_vs_reach_disambiguation, MSC |
| **T8** | Mastery attribution after T7 | 9 | ARC.mastery_attribution_grounding, MSC |
| **T9** | Cross-section drift | 3, 7 | RRQ.exploration_appropriateness, CC.topic_stability |
| **T10** | Multi-session continuity | 1, 9 | MSC.EWMA_movement, mem0 cycle |

Each test must produce a session JSON that the scorer can ingest, and assertions checking specific dimension thresholds.

---

## 6. Output Format

### Per-session JSON (written by scorer)

Layout reflects the hierarchy: `primary` first (headline), `secondary` second (diagnostic), `penalties` separately.

```json
{
  "session_id": "...",
  "test_id": "T7_intermediate_question",
  "timestamp": "2026-04-29T...",

  "primary": {
    "EULER": {
      "question_present": 1.0,
      "relevance": 0.85,
      "helpful": 0.80,
      "no_reveal": 1.0,
      "passes_all_thresholds": true
    },
    "RAGAS": {
      "context_precision": 0.90,
      "context_recall": 0.75,
      "faithfulness": 0.88,
      "answer_relevancy": 0.82,
      "answer_correctness": 0.78,
      "passes_all_thresholds": true
    }
  },

  "secondary": {
    "TLQ": {"score": 0.87, "sub": {"intent_match": 1.0, "match_top_score": 0.88, "...": "..."}},
    "RRQ": {"score": 0.91, "sub": {"...": "..."}},
    "AQ":  {"score": 0.95, "sub": {"...": "..."}},
    "TRQ": {"score": 0.78, "sub": {"euler_ref": "see primary.EULER", "qc_pass_rate": 0.83, "...": "..."}},
    "RGC": {"score": 1.00, "sub": {"...": "..."}},
    "PP":  {"score": 0.62, "sub": {"...": "..."}},
    "ARC": {"score": 0.45, "sub": {"...": "..."}},
    "CC":  {"score": 1.00, "sub": {"...": "..."}},
    "CE":  {"score": 0.70, "sub": {"...": "..."}},
    "MSC": {"score": 0.80, "sub": {"...": "..."}}
  },

  "penalties": [
    {"code": "FABRICATION_AT_REACHED_FALSE", "severity": "critical", "evidence": "tutor turn 5: 'you correctly identified...'"}
  ],

  "verdict": "failed_critical_penalty",

  "raw_signals": {
    "turn_count": 8,
    "max_turns": 25,
    "interventions": 1,
    "cost_usd": 0.12
  }
}
```

### Aggregate dashboard (across N sessions)

For each dimension: distribution (mean, p25, p50, p75, p95) across sessions. Penalty flags grouped by code with example session IDs.

```
Dimension     | mean | p25  | p50  | p75  | p95  | n_failed
TLQ           | 0.84 | 0.78 | 0.86 | 0.92 | 0.95 | 2
RRQ           | 0.88 | 0.83 | 0.90 | 0.94 | 0.98 | 0
ARC           | 0.51 | 0.40 | 0.50 | 0.65 | 0.85 | 6  ← needs attention
...

Critical penalties: 4 sessions
  FABRICATION_AT_REACHED_FALSE: 3 (T7 baseline, ...)
  LEAK_DETECTED: 1 (...)

Major penalties: 7 sessions
  ...
```

---

## 7. Implementation Plan

**Phase 1a — scorer (no flow code changes):**
- `scripts/score_conversation_quality.py` — reads exported session JSON, computes all 10 internal dimensions + penalties.
- `scripts/score_ragas.py` — runs RAGAS metrics (or hand-rolled equivalents) against the same JSON.
- `scripts/run_eval_dashboard.py` — aggregates N session results into the dashboard format above.
- Writes per-session results to `data/artifacts/eval/{session_id}.json` and dashboard to `data/artifacts/eval/dashboard.json`.

**Phase 1b — baseline existing tests:**
- Run scorer on the 4 existing gate test JSONs (T1–T4) + the gravity-bug session export.
- Verify the scorer correctly flags:
  - T1, T4: FABRICATION_AT_REACHED_FALSE = 0 (post-Change-1)
  - Gravity export (pre-Change-1): FABRICATION_AT_REACHED_FALSE = 1+ (regression evidence)
- Establish per-dimension baseline ranges.

**Phase 2 — Change 3 (low-effort + intermediate-question + mastery-attribution):**
- Tighten dean's setup-classify prompt (low_effort detection)
- Add explicit step-vs-reach disambiguation logic
- Update mastery_scorer prompt to require student-utterance grounding
- Validate with ARC dimension scores pre/post

**Phase 3 — new tests T5–T10:**
- Build harness scenarios for each.
- Each must produce assertions on specific dimensions, not just `expected_reached`.

**Phase 4 — RAGAS integration:**
- Add `ragas` package or equivalent prompt-based implementation.
- Score the same sessions; cross-check with our internal dimensions for correlation.

---

## 8. Open questions / decisions

1. **Per-dimension pass thresholds.** What does "passing" mean per dimension? Suggested defaults (refine after baseline):
   - TLQ ≥ 0.80, RRQ ≥ 0.75, AQ ≥ 0.85, TRQ ≥ 0.75 (EULER pass + qc_pass_rate ≥ 0.80), RGC ≥ 0.95 (false-positive rate < 5%), PP ≥ 0.60, ARC ≥ 0.70, CC = 1.00, CE ≥ 0.50, MSC ≥ 0.70.
2. **Grounding LLM judgments for ARC.mastery_attribution_grounding.** Use Haiku for cost? Sonnet for accuracy? Decision affects eval cost.
3. **Should the scorer block CI?** A session that fails any Critical penalty = test failure. Major penalties = warning. Dimension below threshold = warning.
4. **Trace versioning.** The trace schema may evolve; the scorer should declare a `trace_schema_version` it expects and refuse mismatched inputs.

---

## 9. Why this design and not a single composite

A weighted score (e.g. CQS = 100 × Σ wᵢ·Dᵢ) hides per-dimension diagnostics. A session with great retrieval (RRQ=0.95) but a fabrication (RGC=0.0) would score ~0.70 weighted — looks "OK" — when it's actually a critical fail. Dimensions stay separate; penalties surface critical failures explicitly; the dashboard shows distributions per dimension. Decisions about which dimension to invest in optimizing become explicit, not buried in weights.

---

## Appendix A — Wrapper → Dimension cross-reference

For implementers: which trace wrappers feed which dimensions.

| Wrapper | Dimensions |
|---|---|
| `dean.prelock_intent` | TLQ |
| `dean.topic_match` | TLQ |
| `dean.topic_vote` | TLQ |
| `dean.coverage_gate` | TLQ, RRQ |
| `dean.python_retrieval` | RRQ |
| `dean.exploration_judge` | RRQ |
| `dean.exploration_retrieval` | RRQ |
| `dean.anchors_locked` | TLQ, AQ |
| `dean.anchor_extraction_failed` | TLQ |
| `dean.sanitize_locked_answer` | AQ |
| `dean._lock_anchors_repair_call` | TLQ |
| `dean.hint_plan_initialized` | PP |
| `dean.topic_ack_emitted` | (Change 2 sanity check) |
| `dean._setup_call` | TRQ, ARC |
| `dean.reached_answer_gate` | RGC |
| `dean.confidence_score` | RGC, PP |
| `dean.hint_progress` | PP |
| `dean.complexity_classifier` | RRQ |
| `dean._deterministic_quality_check` | TRQ |
| `dean._quality_check_call` | TRQ (EULER), penalties (LEAK_DETECTED) |
| `dean.revised_teacher_draft_applied` | TRQ |
| `dean.fallback` | TRQ |
| `dean.force_assessment_loop_guard` | PP, CC |
| `dean_node.invariant_violation` | CC, penalties |
| `dean._clinical_turn_call.dedupe_guard` | (assessment, separate) |
| `dean._close_session_call.dedupe_guard` | MSC |
| `score_session_llm` (mastery_store) | MSC, ARC.mastery_attribution_grounding |
| `state.debug.interventions` | TRQ |
| `state.debug.coverage_gap_events` | RRQ, TLQ |
| `state.debug.invariant_violations` | CC, penalties |
| `state.debug.cost_usd` | CE |
| `state.debug.api_calls` | CE |
| `state.debug.cache_*` (per call) | CE |
