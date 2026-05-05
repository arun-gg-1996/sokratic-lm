# HANDOFF — Sokratic V2 Demo Prep (2026-05-05, Account Switch)

> **Why this doc exists:** Arun is switching to a different Claude Max account
> due to usage limits. The new session has **zero** prior context. This file is
> the single source of truth for picking up the work.

---

## ⚠️ READ THIS FIRST (60 seconds)

1. **Working dir:** `/Users/arun-ghontale/UB/NLP/sokratic`
   (NOT the worktree under `.claude/worktrees/…` — those are stale per stored memory.)
2. **Active branch:** `nidhi/reach-gate-and-override-analysis`
3. **Uncommitted state:** ~63+ files modified/untracked. Do NOT commit. Do NOT discard. Treat as in-flight work.
4. **Locked plan:** Q17–Q27 root causes + fixes are **fully designed** in this doc.
   **Implementation is BLOCKED** until Arun explicitly says "go A" / "go A,B,C" / "go all".
5. **Most recent action:** AWS Bedrock keys swapped to `sokratic_accessKeysArun.csv` (account `232939969301`, IAM user `sokratic`, region `us-east-1`). Verified working with a Haiku 4.5 invoke.
6. **🕓 4 HOURS TO DEMO.** Essential fixes are explicitly demarcated below ("ESSENTIAL — DEMO BLOCKERS"). Everything in "COSMETIC / POST-DEMO" must NOT be touched until essentials ship + Tier 1 verifies green.
7. **Read next:** `docs/PRE_DEMO_ISSUES.md` (173 KB; single source of truth for the issue log). Q17–Q27 live at lines **3897–4019**. Q-polish (Q1–Q13) lives at lines **998–1331**.

---

## 🚨 SESSION DELTA — what changed since this doc was first written

The session that wrote this doc continued for several hours. Below is everything that landed and everything that's now known. Treat this section as authoritative over the rest of the doc when they conflict.

### Cleanup work shipped (D1 / D2 / D3) — DO NOT REDO
- **D1 (V1→V2 single source of truth, Option B):** `SOKRATIC_USE_V2_FLOW` flag removed. V2 owns the per-turn graph. **V1 `dean.py` + `teacher.py` are RETAINED on purpose** because `topic_lock_v2.py` makes 7 calls to V1 dean methods (`_lock_anchors_call`, `_retrieve_on_topic_lock`, `_build_topic_ack_message`, `_prelock_refuse_call`, `_prelock_anchor_fail_call`). D1-bootstrap migration (porting these 4 methods into V2) is **deferred post-demo — architectural**. New V2 modules created: `conversation/lifecycle_v2.py` (~869 lines: rapport_node + memory_update_node + edges), `conversation/streaming.py` (~159 lines: callback registry), `conversation/reach_gate.py` (~280 lines: reach detection ported with direct anthropic calls).
- **D2 (eval consolidation):** `eval/`, `evaluation/`, `simulation/` merged under `data/artifacts/eval/`. 17 import sites updated, 10 files moved with `git mv`.
- **D3 (classifier split):** `conversation/classifiers.py` (was ~1120 lines) trimmed to **141 lines (shared infra only)**. New: `conversation/verifier_quartet.py` (~830 lines) and `conversation/preflight_classifier.py` (~347 lines). Import aliases updated in `retry_orchestrator.py`, `assessment_v2.py`, `preflight.py`.

### Verifier reliability shipped (Track A) — DO NOT REDO
- **Rule 0 added** to `_HINT_LEAK_SYSTEM` in `verifier_quartet.py` — explicitly forbids verbatim answer/alias mentions (case-insensitive whole-word).
- **Retry-on-OK wrappers** added to all 4 quartet checks (leak retries on "clean", sycophancy retries on "clean", shape retries on `pass=True`, pedagogy retries on `pass=True`). Asymmetric: trust "bad" verdict immediately, retry the lenient one. Reduces Haiku temp=0 stochastic miss rate ~30% → ~9% per check. Compound 16-verdict orchestrator scenarios (Tier 2 V6/V7/V9/V10) **still flake** — that's the residual risk, NOT a regression.

### CRITICAL bug introduced + fixed in this session
- **D1.4 graph.py rewrite passed `dean=None, teacher=None`** thinking they were vestigial after V1 removal. They are NOT — `topic_lock_v2.py` calls 7 V1 dean methods at lines 483, 499, 548, 596, 648, 730, 732. With `dean=None`, every prelock LLM call silently `AttributeError`'d → fell through to the templated string at `topic_lock_v2.py:730` ("I could not find a strong textbook match for that…").
- **Symptom seen by Arun:** `~/Downloads/sokratic_nidhi_2026-05-05T16-51-47-554Z.json` — every tutor turn was the templated fallback.
- **Fix shipped in `conversation/graph.py`:** restored `DeanAgent(retriever, memory_client)` + `TeacherAgent()` instantiation, passed via `partial()` into all 4 V2 nodes. Module imports clean. **Not yet end-to-end verified via a live prelock turn.**
- **Verification options offered to Arun (his pick when he resumes):**
  - Run Tier 1 full (~25 min) — confirms no broader regression
  - Run targeted bootstrap-path test (~2 min) — confirms LLM text replaces the templated fallback
  - Skip — fix is verified at module-import level only

### Metadata-flow audit (LLM prompt enrichment) — IMPORTANT FINDING
Arun asked whether the activity-log metadata he added a while ago (tool calls, haiku verdicts, full conversation history) is still being fed into tutor responses. Findings:

| Path | History renderer | snapshots? | system_events? |
|---|---|---|---|
| **V2 per-turn** (`dean_v2.plan`, `teacher_v2.draft`, `retry_orchestrator`) | `conversation/history_render.py` | ✅ yes | ✅ yes |
| **V1 bootstrap/prelock** (5 helpers in `dean.py` reached via `topic_lock_v2.py`) | `conversation/rendering.py` (plain `role+content` only) | ❌ no | ❌ no |

- V2 enrichment is **fully intact** (confirmed at `nodes_v2.py:459-460/647-648`, `dean_v2.py:551-552`, `teacher_v2.py:430-431/495-496`). NOT a regression from D1/D2/D3.
- V1 bootstrap path was **never enriched** — 18 calls to plain `render_history` in `dean.py`. This is pre-existing.
- **After the graph.py fix lands, prelock LLM calls will fire normally** but will produce thinner replies than V2 turns because they lack snapshots/events. To enrich them, do D1-bootstrap migration (deferred post-demo).

### Test status as of session end
- **Tier 1 (36 scenarios):** Last partial smoke at 5/5 PASS during the verifier-wrapper work. NOT re-run after the graph.py dean=None fix.
- **Tier 2 (11 verifier scenarios):** V1-V5 + V8 + V11 stable. V6/V7/V9/V10 flake on compound probability (residual Haiku stochasticity, retry wrappers reduced but didn't eliminate).
- **`tests/test_topic_lock_v2.py`:** 5/8 pre-existing failures (`_map_topic` mock signature stale). Not introduced this session — leave alone.

---

## Project at 30k feet

**Sokratic** is a Socratic-method tutoring system that:
- Teaches one locked concept per session via questions, not exposition.
- Drives 4 conversation phases: `rapport → tutoring → assessment → memory_update`.
- Preserves safety contracts (no answer leak, no sycophancy, no off-domain).
- Uses LangGraph for orchestration, Anthropic Claude (via Bedrock) for all LLM calls, mem0 for cross-session memory, Qdrant for chunk retrieval.

**Two flows coexist** behind a feature flag (`SOKRATIC_USE_V2_FLOW=1`):
- **V1 (legacy)** — graph nodes in `conversation/nodes.py`, hint ladder + IDK reveal threshold + Dean revisions. **The friend's `run_e2e_tests.py` (97 scenarios) targets V1.**
- **V2 (active)** — graph nodes in `conversation/nodes_v2.py`. Preflight classifier → Dean.plan() → retry orchestrator → verifier quartet. **All recent work, the demo, and Q17–Q27 are V2.**

**Active domain:** `ot` = `human anatomy` (textbook `openstax_anatomy`). Despite the `ot` filename slug, this is **NOT** "Old Testament" — it's anatomy/medical. Domain config: `config/domains/ot.yaml`. PDF: `data/raw/Anatomy_and_Physiology_2e_-_WEB_c9nD9QL.pdf`.

**Second domain coming:** `physics` (textbook `openStax_physics_v1`). PDF moved to `data/raw/openStax_physics_v1.pdf` today. Chunking pipeline NOT yet built — must mirror `ingestion/sources/openstax_anatomy/` structure (config.yaml + parse.py + filters.py + extract.py + prompt_overrides.py). **This is queued post-Q17–Q27 fixes.**

---

## Current state of the working tree

```
Branch:        nidhi/reach-gate-and-override-analysis
Last commit:   67ffdf6 fix(m1): surface exit_intent_pending / session_ended …
Modified:      35 files (mostly conversation/* — V2 stack scaffold from prior sessions)
Untracked:     28 files including:
  - conversation/registry.py        (NEW — vocabulary registry, BLOCK 5)
  - conversation/master_prompt.py   (NEW — master system prompt, BLOCK 5)
  - conversation/snapshots.py       (NEW — turn snapshots + system events)
  - conversation/history_render.py  (NEW — shared history renderer)
  - scripts/stress_test_flows.py    (~36 V2 scenarios)
  - scripts/stress_test_verifier.py (11 verifier scenarios)
  - tests/test_registry.py, test_master_prompt.py, test_snapshots_purity.py
  - docs/HANDOFF_2026-05-03_ANALYSIS_VIEW.md
```

**Do not commit yet.** The user prefers to commit at well-defined milestones. There's a `MEMORY.md` rule (see below) that says "no implementation without explicit per-item go-ahead" — committing falls under that. Wait for instruction.

---

## ⚠️ Critical user collaboration preferences (from auto-memory)

The new session will load these automatically from `~/.claude/projects/-Users-arun-ghontale-UB-NLP-sokratic/memory/MEMORY.md`, but here they are explicitly:

| Rule | Why | Apply when |
|---|---|---|
| **No implementation without explicit per-item go-ahead** | Past sessions over-implemented; user wants design/discuss until they say "go" on the specific item. | ALWAYS. Even if user says "fix bugs", design first, ask for go. |
| **No templated tutor fallbacks** | LLM failures must surface as error cards (component + class + message + retry), never fake tutor text. | When tempted to add a "safe default" string the tutor would say — render an error card instead. |
| **Mirror existing guards on parallel paths** | Past M6-class regressions: new code path missed a guard the sibling path had. | Adding a return path? Check sibling returns for guards (see Q17 below for an instance). |
| **Activity chip + user message must be paired** | Every status chip needs a paired explanation message; chips never preview an outcome the student hasn't seen yet. | Adding new activity status fires. |
| **Run eval scripts sequentially** | Parallel execution caused memory issues; default sequential, parallelize only within-turn LLM calls. | Running `scripts/stress_test_*.py`, `scripts/run_eval_chain.py`. |
| **Log issues during eval, don't fix mid-run** | Accumulate all failures, present consolidated list at end; never patch + re-run mid-eval. | When an eval surfaces a bug, log it to `docs/PRE_DEMO_ISSUES.md` and keep the eval going. |
| **OT = anatomy, not Old Testament** | The `ot` slug is legacy; current textbook is `openstax_anatomy`. | Any time you reason about the domain name. |

---

## 🔒 Locked plan: Q17–Q27 fixes + deferred polish

**Status: design complete, implementation BLOCKED until explicit "go".**

### 🕓 4-HOUR DEMO BUDGET — ESSENTIAL vs COSMETIC

**🔴 ESSENTIAL — DEMO BLOCKERS (ship in this order, ~50 min total + verification):**

| Order | Item | Est | Why essential |
|---|---|---|---|
| 1 | **Verify graph.py dean=None fix** (already shipped this session) — run targeted prelock LLM test ~2 min OR Tier 1 full ~25 min | 2-25 min | If broken, EVERY prelock turn shows templated "I could not find a strong textbook match" instead of LLM text. Demo will look broken on first off-topic message. |
| 2 | **A (Q17)** — anchor_pick_overrides merge in 3 early-return paths (`nodes_v2.py`) | 5 min | Without this, anchor chip click → next turn re-runs topic_lock with empty Q/A → demo flow breaks at the M4 hand-off. |
| 3 | **B (Q19)** — soft_reset hint_text override + Teacher temp 0.4→0.7 + anti-paraphrase preamble rule | 15 min | Without this, Cancel-modal exit produces verbatim `locked_question` repeat — feels robotic + breaks immersion. |
| 4 | **C (Q20–Q22)** — system event awareness in `history_render.py` + Teacher preamble + preflight | 30 min | Without this, Teacher accuses student of "jumping to the answer" right after they CLICKED an anchor chip. Most-likely-to-be-noticed bug in a demo. |
| → | **Verify Tier 1 (36 scen) + Tier 2 (11 scen)** sequentially. Log failures, do NOT fix mid-run (memory rule). | ~30-40 min | |

**Hard stop after C unless Arun explicitly says continue.** That's already ~1.5–2 hours for ESSENTIAL block including verification. Reserve the remaining ~2 hours for: (a) any regression Tier 1 surfaces, (b) demo dry-run, (c) buffer.

**🟡 COSMETIC / NICE-TO-HAVE — POST-DEMO unless ESSENTIAL+verify finishes with >2 hrs left:**

| Item | Est | Why deferable |
|---|---|---|
| D (Q23 anchor card filter) | 30 min | Edge case — only triggers when anchor sources include cross-topic picks. Demo path can avoid. |
| E (Q24 switch escape valve) | 25 min | Edge case — only triggers after 3 consecutive switch attempts. Demo won't repeat-switch. |
| G (Q26 single-question shape) | ~10 min | Aesthetic — compound questions are awkward but parseable by the student. |
| H (Q27 adjacency bridge) | 30 min | Edge case — only triggers on near-miss topic picks. |
| F (Q25 non-Teacher persona audit) | ~1 hr | Audit-shaped, broad surface. Risk of side effects > demo value in 4 hrs. |
| Polish Q1/Q2/Q3/Q5/Q9 (phrasing, greeting, personalization) | 3-4 hrs | Pure phrasing — demo works without. |
| Physics chunking | TBD | Anatomy is the demo domain; physics is post-demo. |
| E2E V2 script | TBD | Tier 1 is sufficient for demo verification. |
| **D1-bootstrap migration** (port 4 V1 dean methods to V2 namespace) | ~600 lines | **Architectural, post-demo.** Would also bring snapshots/events enrichment to bootstrap responses. Deferred per Arun's earlier decision. |
| **B (verifier prompt calibration)** beyond retry wrappers | ~hours | Diminishing returns; V6/V7/V9/V10 Tier 2 flake is residual stochasticity, not a demo blocker. |

**🚫 DO NOT TOUCH during the 4-hour window:**
- Anything in `eval/` or `data/artifacts/eval/` paths (D2 already landed; touching invites import drift).
- `conversation/dean.py` or `conversation/teacher.py` (V1 — bootstrap depends on them).
- `tests/test_topic_lock_v2.py` (5/8 pre-existing failures unrelated to demo).
- Verifier prompts beyond what's already in `verifier_quartet.py` (retry wrappers shipped; further calibration is diminishing-returns).
- Anything in the "COSMETIC" table above unless ESSENTIAL + verification fully complete with safety margin.

### Resolved scoping decisions (already confirmed by Arun)

- **Q1 (anchor cards scope):** filter to `locked_topic` chunks only — no cross-topic cards. Bridges (different feature, root cause H) handle adjacent-topic typed mentions.
- **Q2 (topic-switch escape threshold):** hardcoded `TOPIC_SWITCH_ESCAPE_THRESHOLD = 3` as a named constant. After 3 consecutive switch attempts, Dean offers a soft-unlock.
- **Q3 (Teacher temperature):** raise from `0.4` to `0.7` to break Cancel-modal verbatim-repeat. (Combined with B1+B2 below — three independent guards.)

### Root causes + planned fixes (grouped, not symptom-by-symptom)

#### ROOT CAUSE A — LangGraph reducer drops mid-node state mutations
**Symptom:** Q17 (anchor pick chip loses lock); cascades into Transcript 1 issues #3, #4, #5, #7, #8–11.
**Defect:** `dean_node_v2` in `conversation/nodes_v2.py` has 4 return paths. Only the bottom (engaged tutoring) one merges `anchor_pick_overrides`. The 3 early-return paths (lines ~400, ~414, ~516) skip the merge → when student clicks anchor chip → preflight fires on chip text → returns at one of those paths → `locked_question` / `locked_topic` / `phase` updates dropped by reducer → next turn re-runs `topic_lock_v2` with empty Q/A.
**Fix:** Mirror the existing merge pattern at the bottom (lines ~861–866) into the 3 early-return paths. ~5 lines per path. **Each merge looks like:**
```python
_ret = { ...existing dict... }
if anchor_pick_overrides:
    for _k, _v in anchor_pick_overrides.items():
        if _k == "messages":
            continue
        _ret[_k] = _v
return _ret
```
**Safety:** None touched (pure plumbing).
**Verification:** Replay Transcript 1 turn 2; confirm `locked_question` populated next turn.

#### ROOT CAUSE B — Locked-question text leaks into hint surface verbatim
**Symptom:** Q19 (Cancel exit → tutor repeats `locked_question` verbatim); Transcript 2 robotic regeneration.
**Defect (3 contributors):**
- B1: `nodes_v2.py` soft_reset path — `hint_text` falls back to `locked_question` verbatim when no `next_hint` queued.
- B2: `teacher_v2.py::_PROMPT_PREAMBLE` anti-repetition rule covers Tutor's prior turns but doesn't forbid echoing `locked_question`.
- B3: Teacher temperature `0.4` collapses similar-context regenerations to the same string.
**Fix:**
- Override `hint_text` in soft_reset return — emit a re-engagement framing, never `locked_question`.
- Add to `_PROMPT_PREAMBLE`: *"Never echo `locked_question` verbatim. Re-anchor with a fresh framing."*
- Raise Teacher `temperature=0.4` → `temperature=0.7` (line 588 of `teacher_v2.py`).
**Safety:** L1 (hint leak) STRENGTHENED. EULER preserved. ~15 lines.

#### ROOT CAUSE C — Teacher and Preflight blind to UI/system events
**Symptoms:** Q20 (anchor_pick_shown doesn't render), Q21 (Teacher says "you jumped straight to the question" after a chip CLICK), Q22 (preflight misclassifies post-event turns), partial Q26/Q27.
**Defect (3 layers):**
- C1: `history_render.py` may filter events with `after_turn=-1` (anchor_pick_shown fires pre-first-turn).
- C2: `teacher_v2.py::_PROMPT_PREAMBLE` has no rule recognizing chip-click as engagement.
- C3: `classifiers.py::_UNIFIED_INTENT_SYSTEM` (preflight) doesn't receive system events.
**Fix:**
- Verify `after_turn=-1` rendering in `history_render.py`.
- Add EVENT-AWARE rule to Teacher preamble: *"If `[anchor_pick_shown]` precedes student message, treat their text as picking from those options."*
- Thread recent `system_events` into preflight prompt context.
**Safety:** L4 (sycophancy) untouched. ~30 lines total.

#### ROOT CAUSE D — Anchor cards include picks the system can't honor
**Symptom:** Q23 (cards offered → user picks one → "I don't have material on that").
**Defect:** Anchor card source draws from a wider catalog than `locked_topic` exposes.
**Fix:** Locate anchor source (likely `backend/api/session.py` or `topic_lock_v2.py`) and intersect candidate cards with chunks under current `locked_topic` before render.
**Size:** ~30 min (locate + filter + verify).

#### ROOT CAUSE E — No escape valve for repeated topic-switch attempts
**Symptom:** Q24 (3–5 switch attempts in a row, all rejected, no escalation).
**Fix:**
- Add `consecutive_topic_switch_count` to `state.py`.
- Increment in `preflight.py` on `topic_switch` verdict; reset on engagement.
- After threshold (constant `TOPIC_SWITCH_ESCAPE_THRESHOLD = 3`), Dean offers soft-unlock: *"You've asked to switch a few times — want to move to X instead?"*
**Safety:** L2 (topic stickiness) relaxed only after explicit repetition; Dean retains decision authority. ~25 lines.

#### ROOT CAUSE F — Non-Teacher persona breakage
**Symptom:** Q25 (deterministic strings in non-tutor voice break immersion).
**Fix:** Audit deterministic user-visible strings in `nodes_v2.py`, `dean.py`, `preflight.py`. For each: route through Teacher (single-shot, mode=narrator) OR confirm it's a safety-net we keep deterministic by design. ~1 hr audit.

#### ROOT CAUSE G — Compound questions in one turn
**Symptom:** Q26 (Tutor asks two questions in one breath).
**Fix:** Add SHAPE rule to Teacher preamble: *"Ask exactly one question per turn."* Extend `haiku_shape_check` to flag compound asks. ~3 lines + verifier extension.

#### ROOT CAUSE H — Topic bridges missed when adjacent
**Symptom:** Q27 (student picks adjacent subsection → system rejects instead of bridging).
**Fix:** Extend `dean_v2.py` topic-switch handler with adjacency check; if adjacent, offer bridge mode. ~30 min.

### Deferred polish (post-critical, post-demo if needed)
- **Q1** Topic-confirm phrasing — warmer wording on lock confirm.
- **Q2** Greet returning student by name — pull from memory.
- **Q3** Personalized topic suggestions on cold-start.
- **Q5** Locked-question re-presentation phrasing varies.
- **Q9** Returning-student greeting distinct from new-student.

### 🔓 Locked plan ready to execute

The full plan below is **locked** — designs reviewed, scoping decisions confirmed (Q1/Q2/Q3 above), root causes identified per scenario. The next session can ship it surgically as soon as Arun gives the go signal.

```
🔴 ESSENTIAL (demo-blocker block, target ≤2 hrs incl verification):
0. Verify graph.py dean=None fix      — 2 min (targeted) or 25 min (Tier 1 full)
1. A (Q17)                            — 5 min     — anchor_pick_overrides merge, 3 early-return paths
2. B (Q19)                            — 15 min    — soft_reset hint_text + temp 0.4→0.7 + preamble rule
3. C (Q20-Q22)                        — 30 min    — system event awareness (renderer + Teacher + preflight)
   ↓ verify Tier 1 (36) + Tier 2 (11) sequentially, log failures don't fix mid-run

🟡 COSMETIC (only if essentials + verify done with >2 hrs left):
4. D, E, G, H                         — ~2 hrs    — anchor card filter, switch escape, single-Q, adjacency
5. F                                  — ~1 hr     — non-Teacher persona audit
6. Polish (Q1,Q2,Q3,Q5,Q9)            — ~3-4 hrs  — phrasing/personalization

🚫 POST-DEMO (do NOT touch in 4-hr window):
7. D1-bootstrap migration             — ~600 lines — port V1 dean prelock helpers to V2
8. Verifier prompt calibration        — hours      — diminishing returns past retry wrappers
9. Physics chunking                   — TBD        — anatomy is the demo domain
10. E2E V2 script                     — TBD        — Tier 1 is sufficient for demo
```

**To start, the next session should ask Arun for one of:**
- `go verify` — just confirm the graph.py dean=None fix end-to-end, then stop for review
- `go A` — ship just Q17 (5 min), then stop for review
- `go A,B,C` — ship the ESSENTIAL batch then verify with stress tests (RECOMMENDED for demo)
- `go all-essential` — verify graph.py fix + ship A,B,C + run verification (full essential block)
- `go all` — full sequence, no per-step pause (NOT recommended given 4-hr budget)
- `go <other>` — Arun's own scoping (e.g. `go A,B` or `go A,C`)

**Until the go signal arrives, do NOT edit any of the files in scope.** Use the time to read the files listed in the index below, run the boto3 smoke test, and confirm the locked plan still matches current code reality (file/line refs may have shifted slightly if other commits landed).

### Safety contracts to preserve across ALL fixes
- **L1 (hint leak):** B and C STRENGTHEN, none relax.
- **L4 (sycophancy):** verifier logic unchanged.
- **EULER (E/U/L/E):** preserved.
- **Verifier quartet:** all fixes run before/after verifier; verdict logic unchanged.
- **No-templated-fallback memory:** F honors this — failures still surface as error cards.

---

## Architecture essentials (read BEFORE editing any of these files)

### THE bug class: LangGraph reducer drops mid-node state mutations
**Critical to understand.** LangGraph's reducer merges only fields present in the dict your node returns. Direct `state["x"] = y` mutations inside a node are **dropped** unless you also include `"x": y` in the returned dict.

This bit us at:
- BLOCK 9 (cancel modal) — `state["cancel_modal_pending"] = False` was lost; fix added it to final_return dict.
- Q17 (anchor pick) — same defect, different return path. Fix is locked above.

When adding a return path, **always** ask: "What state fields did this code path mutate? Are they ALL in the return dict?"

### V2 stack overview

```
Student message
       ↓
   preflight.py::run_preflight   (8 verdicts including low_effort, deflection, off_topic, etc.)
       ↓
  ┌────┴────┐
  │ fired?  │
  ├─ YES → teacher_v2.draft(redirect/nudge/confirm_end)        (single attempt)
  └─ NO  → dean_v2.plan() → retry_orchestrator.run_turn()
            ↓
          retry orchestrator:
            attempt 1: teacher.draft() → verifier_quartet
            attempt 2: teacher.draft() → verifier_quartet
            attempt 3: teacher.draft() → verifier_quartet
            ↓ (all 3 failed?)
            dean.replan()
            attempt 4: teacher.draft() → verifier_quartet
            ↓ (still failed?)
            SAFE_GENERIC_PROBE (deterministic neutral re-anchor)
```

### Verifier quartet (`conversation/classifiers.py`)
- `haiku_hint_leak_check` — does draft leak `locked_answer` / aliases / chunk content?
- `haiku_sycophancy_check` — does draft confirm a wrong answer or over-praise?
- `haiku_shape_check` — does draft conform to plan.shape_spec (sentence count, ends with question, no_repetition)?
- `haiku_pedagogy_check` — does draft preserve Socratic stance (asks rather than tells)?

### Master prompt + vocabulary registry (BLOCK 5, NEW & UNCOMMITTED)
- `conversation/registry.py` — 6 vocabulary classes (intents, modes, tones, phases, modal events, hint transitions). Each emits `system_prompt_block()` for prompts and `annotate(...)` for history snapshots. Single source of truth — no duplication between prompts and code.
- `conversation/master_prompt.py` — assembles a master system prompt with phase descriptions, agent roles, state-field reference (referencing fields BY NAME, never VALUE), safety contracts.
- `conversation/snapshots.py` — `snapshot_student_turn()`, `snapshot_tutor_turn()`, `log_system_event()`. Has `_SENSITIVE_KEYS` frozenset (rejects locked_answer, aliases, chunks).
- `conversation/history_render.py` — `render_history(messages, snapshots, events, max_turns=50)`. Used by both Teacher and Dean.

### 2-tier Bedrock prompt cache
Master prompt + vocabulary blocks = **Tier 1 cache** (stable across all turns).
Body (current state, history, plan) = **Tier 2 cache**.
Verified working: ~2993 tokens cached read, ~24% latency improvement on warm calls.

### Anchor pick (M4) flow
- After topic prelock, system renders 3 anchor question cards under `state.pending_user_choice.kind = "anchor_pick"`.
- Student clicks chip → message text = chip label.
- `topic_lock_v2.run_topic_lock_v2` resolves the pick → returns `locked_question`, `locked_answer`, etc.
- `dean_node_v2` falls through to engaged tutoring (the "just_picked" path at line ~242).
- **Q17 bug:** if preflight fires on chip text after the resolution, the early-return paths drop the lock fields. Fix locked above.

### Cancel modal (BLOCK 9)
- Student types deflection → `exit_intent_pending=True` → frontend shows ExitConfirmModal.
- Student clicks Cancel → frontend sends `__cancel_exit__` sentinel → `state["cancel_modal_pending"] = True`.
- Next turn skips preflight (would re-trigger), forces `mode=soft_reset` on Teacher draft, clears flag in final_return.

### Rapport-decline direct close (BLOCK 14)
- Deflection during rapport stage (no `topic_confirmed`, no `locked_topic.path`) → skip the modal, go straight to `phase=memory_update` with `close_reason=exit_intent`.
- Student hasn't invested progress to confirm-exit over.

---

## Critical files index

| File | Lines | Role |
|---|---|---|
| `conversation/nodes_v2.py` | 912 | **Central V2 dispatcher.** All Q17 fixes happen here. |
| `conversation/dean_v2.py` | ~600 | Dean planner (single-call TurnPlan emitter). |
| `conversation/teacher_v2.py` | ~700 | Teacher draft + 2-tier cache. Temperature=0.4 (B3 changes to 0.7). |
| `conversation/preflight.py` | ~400 | 3 parallel Haiku checks + L55/L56/L58 strikes + low_effort escalation. |
| `conversation/classifiers.py` | **141 (post-D3)** | **Shared infra only** — `_haiku_call`, `_extract_json`, model constants. Verdict logic lives in the two files below. |
| `conversation/verifier_quartet.py` | **~830 (NEW post-D3)** | **All 4 quartet checks + retry-on-OK wrappers + Rule 0 leak fix.** Imported as `verifier_quartet as C` by `retry_orchestrator.py`. |
| `conversation/preflight_classifier.py` | **~347 (NEW post-D3)** | `haiku_off_domain_check` + `haiku_intent_classify_unified`. Imported by `preflight.py` and `assessment_v2.py`. |
| `conversation/topic_lock_v2.py` | ~400 | Topic lock + anchor pick handler. **Calls 7 V1 dean methods at lines 483/499/548/596/648/730/732.** Templated fallback at line 730 is what produces "I could not find a strong textbook match" when V1 dean calls fail. |
| `conversation/lifecycle_v2.py` | **~869 (NEW post-D1)** | rapport_node + memory_update_node + after_rapport/after_dean/after_assessment edges. |
| `conversation/streaming.py` | **~159 (NEW post-D1)** | Callback registry — 6 setters + 2 fire helpers + 1 getter. |
| `conversation/reach_gate.py` | **~280 (NEW post-D1)** | Reach detection (Step A.1/A.2 token overlap + Step B LLM paraphrase). Direct anthropic calls. |
| `conversation/dean.py` | **218 KB (V1 RETAINED)** | **Do NOT delete.** Bootstrap path's 5 prelock helpers live here. Removal blocked until D1-bootstrap migration (post-demo). |
| `conversation/teacher.py` | **28 KB (V1 RETAINED)** | Same — kept for legacy fallback paths bootstrap may hit. |
| `conversation/graph.py` | 138 lines | **Just fixed this session** — restored `DeanAgent(retriever, memory_client)` + `TeacherAgent()` instantiation. Do NOT revert to `dean=None`. |
| `conversation/state.py` | ~200 | TutorState shape. Add `consecutive_topic_switch_count` here for Q24. |
| `conversation/registry.py` | 321 | NEW. Vocabulary registry. |
| `conversation/master_prompt.py` | 132 | NEW. Master system prompt. |
| `conversation/snapshots.py` | 172 | NEW. Per-turn snapshots + system events. |
| `conversation/history_render.py` | 130 | NEW. Shared history renderer. |
| `docs/PRE_DEMO_ISSUES.md` | ~4000 | **Single source of truth for issues.** Q17–Q27 at lines 3897–4019. |
| `scripts/stress_test_flows.py` | ~1500 | 36 V2 scenarios. Run sequentially. |
| `scripts/stress_test_verifier.py` | ~500 | 11 verifier safety net scenarios. |
| `run_e2e_tests.py` (root) | 2682 | Friend's V1 e2e — 97 scenarios. **V1-only, does not exercise V2.** |
| `config/domains/ot.yaml` | 150 | Anatomy domain config (currently active). |
| `config/domains/physics.yaml` | 50 | Physics domain config (skeleton — chunking not done). |

---

## Environment verification (paste-able)

Confirm AWS Bedrock works on this machine before doing anything else:

```bash
cd /Users/arun-ghontale/UB/NLP/sokratic && source .venv/bin/activate && python3 -c "
import os, json
from dotenv import load_dotenv
load_dotenv()
import boto3
region = os.environ.get('AWS_REGION', 'us-east-1')
sts = boto3.client('sts', region_name=region)
ident = sts.get_caller_identity()
print(f'STS: account={ident[\"Account\"]} arn={ident[\"Arn\"]}')
br = boto3.client('bedrock-runtime', region_name=region)
resp = br.invoke_model(
    modelId='us.anthropic.claude-haiku-4-5-20251001-v1:0',
    body=json.dumps({'anthropic_version': 'bedrock-2023-05-31', 'max_tokens': 16,
                     'messages': [{'role':'user','content':'reply with exactly: ok'}]}),
)
print('Bedrock:', json.loads(resp['body'].read())['content'][0]['text'])
"
```

**Expected output:**
```
STS: account=232939969301 arn=arn:aws:iam::232939969301:user/sokratic
Bedrock: ok
```

If `ThrottlingException: Too many tokens per day`, the daily quota is exhausted — wait for UTC midnight or swap to another key set in `.env`.

If `AccessDeniedException`, the keys lack Bedrock model access — check IAM policy.

---

## Pending work pipeline (post-Q17–Q27)

### Physics chunking (mirror anatomy)
Source files needed in `ingestion/sources/openstax_physics/`:
- `__init__.py` (empty)
- `config.yaml` (textbook_id, pdf_path, parse heuristics, filters)
- `parse.py` (font-size heading parser; copy-adapt from anatomy)
- `extract.py` (sections → seed chunks → `data/processed/raw_sections_physics.jsonl`)
- `filters.py` (back-matter / sidebar removal)
- `prompt_overrides.py` (LLM-enrichment overrides per source)

Then run the existing pipeline (whatever drives anatomy) against the new source. Orphan chunks (those that fail metadata enrichment) get filled via Anthropic API calls — pattern is logged in one of the eval/ingestion logs (find via `grep -r "orphan" ingestion/ scripts/`).

### E2E V2 script
Friend's `run_e2e_tests.py` is V1-only. We need a V2 variant. Two possible approaches:
1. **Adapt the existing `scripts/stress_test_flows.py`** (already V2-aware, 36 scenarios) and add coverage for Q17–Q27 specific fixes once shipped.
2. **Port the friend's 97 scenarios** to use the V2 endpoints + V2 assertions. Bigger lift but provides direct V1↔V2 comparability.
Recommend (1) for demo speed; (2) for long-term regression value.

---

## Things to NOT do

- ❌ Don't implement Q17–Q27 without explicit per-item "go" from Arun.
- ❌ Don't commit anything without explicit ask.
- ❌ Don't change AWS credentials again — they were just verified working.
- ❌ Don't run eval scripts in parallel (memory issues).
- ❌ Don't use templated tutor fallbacks — render error cards on LLM failure.
- ❌ Don't refactor "while you're in there" — the user wants surgical changes.
- ❌ Don't trust the worktree path under `.claude/worktrees/…` — work in `/Users/arun-ghontale/UB/NLP/sokratic` directly.

---

## What to do FIRST in the new session

1. **Read this doc top-to-bottom — especially "🚨 SESSION DELTA" and "🕓 4-HOUR DEMO BUDGET".** ~7 min.
2. **Read `docs/PRE_DEMO_ISSUES.md` lines 3897–4019** (Q17–Q27 issue log, ~120 lines).
3. **Run the boto3 smoke test** below — confirm Bedrock works.
4. **Run `git status`** — confirm uncommitted files roughly match what's described here. Note: counts shift slightly (D2 file moves, D3 new files).
5. **Smoke-check the graph.py fix** at module level: `python3 -c "from conversation.graph import build_graph; print('graph.py imports cleanly')"`.
6. **Reply to Arun** with something like: *"Picked up the handoff. Bedrock auth confirmed (`ok` from Haiku 4.5). graph.py imports cleanly (D1.4 dean=None fix is in place at module level). Recommend `go A,B,C` for ESSENTIAL block (~2 hrs incl verification) given 4-hr demo budget. Awaiting your go signal."*
7. **Wait for explicit `go` signal** before any code changes. The recommended path is `go A,B,C` (ESSENTIAL only) given the time budget — but defer to Arun's call.

---

## Glossary (terms that show up in code without obvious meaning)

- **L1, L4, L43, L46, L49, L52, L53, L55, L56, L58, L62, L74, L78, L9, L10, L11, L22, L78** — internal track numbers from the L43-L62 tutor-flow-rewrite (Track 4.7b). Treat as semantic tags; don't try to derive meaning from the number.
- **EULER** — Engagement / Understanding / Learning / Effectiveness verifier quartet criteria.
- **M-FB** — milestone "no templated tutor fallbacks" (memory rule, listed above).
- **B6** — anchor_pick chip-click block (the M4 → BLOCK 6 sequence in earlier work).
- **BLOCK 5 (REAL-Q5)** — system-awareness scaffolding (registry, master prompt, snapshots, history render).
- **BLOCK 9 (S3)** — Cancel-modal handling.
- **BLOCK 14 (S4)** — rapport-decline direct close.
- **Tier 1 / Tier 2 stress tests** — `stress_test_flows.py` (Tier 1, 36 scenarios) and `stress_test_verifier.py` (Tier 2, 11 verifier scenarios).
- **mem0** — memory layer (cross-session). Read via `mem0_safe`, written via observation_extractor at session end.
- **OT** — anatomy domain slug (legacy name; NOT Old Testament).
- **D1 / D2 / D3** — cleanup tracks shipped this session. D1 = V1→V2 single source of truth (Option B, V1 dean.py retained for bootstrap). D2 = `eval/` consolidation under `data/artifacts/eval/`. D3 = `classifiers.py` split into `verifier_quartet.py` + `preflight_classifier.py`.
- **D1-bootstrap** — deferred post-demo work: port the 5 V1 dean prelock helpers (`_lock_anchors_call`, `_retrieve_on_topic_lock`, `_build_topic_ack_message`, `_prelock_refuse_call`, `_prelock_anchor_fail_call`) into V2 namespace. Would also gain snapshots/events enrichment for bootstrap responses.
- **Bootstrap path** — `topic_lock_v2.py` → V1 `dean.py` prelock helpers. NOT yet V2-enriched (no snapshots/events in prompt).
- **Per-turn / V2 path** — `dean_node_v2` → `dean_v2.plan` → `retry_orchestrator` → `teacher_v2.draft` + `verifier_quartet`. Fully V2, fully snapshots/events enriched.
- **Retry-on-OK** — verifier wrapper pattern: trust "bad" verdict immediately, retry the lenient verdict once. Reduces Haiku temp=0 stochastic miss rate ~30%→~9% per check.
- **Rule 0** — explicit anti-leak rule prepended to `_HINT_LEAK_SYSTEM` in `verifier_quartet.py` — forbids verbatim answer/alias mentions case-insensitively.

---

*Doc author: Claude (prior session). Doc consumer: next Claude session under different account. If anything here is wrong or stale, treat current code/state as authoritative and update this doc.*
