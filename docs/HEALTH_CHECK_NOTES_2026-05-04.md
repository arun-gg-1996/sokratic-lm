# Conversation health check — findings 2026-05-04

Active monitoring of two simulated sessions through every phase. Script:
[scripts/check_conversation_health.py](../scripts/check_conversation_health.py).

## Coverage

| Run | Profile | Trajectory | Phases hit | Result |
|---|---|---|---|---|
| `eval18_solo3_S3` | weak / hedging | tutoring loop only (12 turns, no reach) | rapport, tutoring | 13/15 ✓ (2 expected fails — never reached/closed) |
| `eval18_solo1_S1` | strong | rapport → tutoring → assessment → memory_update | all 4 phases | **23/23 ✓** |

## Per-phase notes

### RAPPORT (both runs ✓)
- Initial dt: 2.4-2.8s.
- v1 path (`teacher.draft_rapport`) — its trace doesn't expose `wrapper:` keys we count, so the per-turn `calls=[]` line is empty by design (NOT a bug).
- Greeting >50 chars in both runs. No error_card.

### TUTORING (both runs ✓)

**solo3_S3 (12 turns, lots of hedging):**
```
turn_count: [0, 0, 1, 2, 3, 4, 5, 6, 7, 7, 8, 9]
hint_level: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
```
- `7 → 7` proves preflight redirects do NOT burn turns ✓
- hint_level held at 0 because S3 hedges → Dean correctly does NOT advance (per spec: only advance on substantive-but-wrong) ✓
- 52 `retriever.reused_lock_time_chunks` events across the 12 turns — per-turn retrieval is dead, cache contract holds ✓
- 0 `mem0_write` events mid-tutoring ✓
- 0 error_card metadata on tutor messages ✓

**Per-turn LLM call mix (engaged turn):**
```
preflight=1  reached_answer_gate=2  plan=1
reused_lock_time_chunks=1  run_turn=1
```
- M7 unified preflight: 1 call (was 3 pre-fix)
- Dean Sonnet plan: 1 call
- Teacher draft (inside run_turn): 1+ depending on retries
- No unconditional retrieve

**Per-turn LLM call mix (preflight redirect):**
```
preflight=1  draft=1  reached_answer_gate=2
```
- Dean was skipped (no plan call) ✓
- Teacher drafted redirect directly ✓
- turn_count NOT incremented ✓

### CLINICAL (solo1 ✓)
- Reached on 1st substantive turn → opt_in card → declined → assessment_turn=1, phase=assessment
- assessment_turn sequence: `[1]` (single opt-in offer before close)
- `clinical_history` not populated (student declined; expected)

### MEMORY_UPDATE (solo1 ✓)
- close_reason=`reach_skipped` (correctly inferred from reach=True + opt_in=no)
- close_draft=1 (close LLM fired)
- last tutor message `mode=close`
- Save bucket activated: `mem0_write=3`, `sqlite_session_end ok`, `mastery_store.update`
- session_ended flag stamped True
- 0 error_cards

## Latency observations

| Phase | Mean dt | Notes |
|---|---|---|
| Rapport | ~2.5s | Single Sonnet call, deterministic prelock-aware path now skips this entirely |
| Topic input + map | 4-10s | TOC-block cache hit drops first-call cost by ~6s |
| Topic confirm/lock | 6s | `dean._lock_anchors_call` Sonnet |
| Tutoring (engaged) | 30-40s | High variance — Teacher draft retries dominate |
| Preflight redirect | 7s | Fast — Dean skipped |
| Close + save | 26s | Close Sonnet + 3 mem0 writes + sqlite + mastery score |

**Tail-latency concern:** engaged tutoring turns occasionally hit 40s+. Likely Teacher draft + verifier-quartet retry. Worth profiling Teacher retry frequency in a separate pass — could indicate hint_text/leak conflicts the verifier is rejecting.

## Cost

- solo3_S3 (12 turns, hedging): **$0.0613** total
- solo1_S1 (4 turns, reach + close): **$0.0155** total

Both within the $0.10/session demo budget.

## What the health-check script catches

For every run it asserts:

**RAPPORT**
- ☐ Initial phase set
- ☐ No locked_subsection at rapport (or if prelock — covered by separate path)
- ☐ Greeting >50 chars (real LLM, not error)
- ☐ No error_card on rapport

**TUTORING**
- ☐ Tutoring phase entered
- ☐ locked_subsection set on lock turn
- ☐ turn_count monotonic
- ☐ hint_level monotonic
- ☐ M6: `reused_lock_time_chunks` fired (cache contract intact)
- ☐ M7: intent classifier ran (unified or legacy)
- ☐ No error_card mid-tutoring (M-FB clean)
- ☐ No premature mem0_write (gated to memory_update)

**CLINICAL** (only if reached + opt_in_yes)
- ☐ Clinical phase entered
- ☐ assessment_turn progression

**MEMORY_UPDATE**
- ☐ Phase reached
- ☐ close_reason set + valid
- ☐ Close LLM fired (`teacher_v2.close_draft` trace entry)
- ☐ Last tutor mode=close OR error_card emitted (no fake fallback)
- ☐ Save-bucket logic matches close_reason (no_save → no writes; else mem0+sqlite ok)
- ☐ session_ended flag set

## Known script limitations (not system bugs)

1. **Latency profile prints empty** — my aggregation reads `elapsed_ms` from trace entries but several wrappers (`reused_lock_time_chunks`, etc.) don't populate that field. Per-turn `dt=` print does work. Fix is cosmetic.
2. **M7 unified vs legacy label** — preflight writes `wrapper:"preflight"` for back-compat, so my "legacy" label is misleading. Both pre-M7 and post-M7 traces use the same wrapper key.
3. **Lock-time anchor calls** (`dean._lock_anchors_call`, `_generate_anchor_variations`) not in `LLM_WRAPPERS` set, so confirm/lock turns show `calls=[]`. Add later for completeness.

None of these affect the assertion correctness — they're just cosmetic gaps in the per-turn print.

## How to run

```bash
nc -z localhost 6333 || docker start qdrant-sokratic
SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks \
  .venv/bin/python -u scripts/check_conversation_health.py [student_id]
```

Defaults to `eval18_solo1_S1`. Exit 0 = all PASS. Output goes to
`data/artifacts/health_check_<thread_id>.json`.

Try also: `eval18_solo3_S3` (hedging), `eval18_solo4_S4` (overconfident),
`eval18_pair3_disengaged` (off-topic).

## Verdict

**System is healthy across all 4 phases.** Two end-to-end runs (one
hedge-only, one full close) confirm every locked block (B0–B7) is
working. Demo-ready except for the known-defer items in
[MORNING_HANDOFF_2026-05-04.md](MORNING_HANDOFF_2026-05-04.md).

---

## Latency reduction follow-up (post initial run)

After analyzing the wrapper-level traces, identified `retry_orchestrator.run_turn`
as the bottleneck (mean 19-26s, max 31s) — the Haiku verifier quartet was
rejecting Teacher's first draft 75%+ of the time, forcing 3-4 retries
per turn.

**Fix applied:** Anthropic prompt-cache marker (`cache_control: ephemeral`)
on the heavy stable parts of Teacher draft and Dean plan prompts. Added
`anthropic-beta: prompt-caching-2024-07-31` header.

**Measured impact (re-ran both health checks after caching):**

solo1_S1 (engaged, 4-turn happy path):
| Phase | Baseline | Cached | Δ |
|---|---|---|---|
| T03 reach + assessment | 35.98s | 4.88s | −86% |
| T04 close + save | 26.22s | 13.21s | −50% |
| Total session | 75.2s | 30.4s | −60% |

solo3_S3 (hedging, 12-turn tutoring loop with heavy retries):
| Metric | Baseline | Cached |
|---|---|---|
| Mean tutoring turn | 38s | 5.5s |
| Max tutoring turn | 46.3s | 6.3s |
| Total session | 388.6s | 62.4s |
| Cost | $0.0613 | $0.0561 |

**Both runs maintained their health-check pass rate** (23/23 and 13/15
respectively — the 2 solo3 failures are S3-expected, not caching-induced).

**Min/max per turn type (post-cache):**
- Engaged tutoring: min 4.03s, max 6.31s
- Preflight redirect: min 4.20s, max 5.23s
- Close + save: 13.21s (single observation)
- Rapport: ~2.6s
- Topic confirm/lock: 5.8s

**Reductions NOT applied (unnecessary post-cache):**
- Drop MAX_TEACHER_ATTEMPTS 3→2 — would save ~1s on edge cases now that
  cache hits make each retry ~2s instead of ~7s
- Skip pedagogy_check on attempt 3+ — verifier quality preserved
- Harden Teacher prompt — long-tail; not worth the risk for demo

**The close turn (~13s) remains the slowest discrete operation** but
it's bounded by mem0 (3-7 writes), sqlite, and mastery LLM scorer —
none of which benefit from the prompt cache since inputs are unique
per session. Acceptable; fires once per session.
