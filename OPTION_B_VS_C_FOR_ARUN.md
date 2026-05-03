# Architecture Decision: Option B vs Option C
For: Arun
From: Nidhi
Date: 2026-05-02
Branch: `nidhi/reach-gate-and-override-analysis`

Two post-paper architecture options on the table. Both fix the same
8 operational issues. They differ in how aggressively they prevent
hint leakage. Need your read before we commit.

Companion docs:
- `CLEAN_ARCHITECTURE_PROPOSAL_v2.md` — the full v2 design (Option B)
- `TODAY_VS_V2_CODE_MAPPING.md` — every code change mapped today→v2
- `HANDOFF_ARUN_2026-05-02.md` — current branch state

---

## Context — what we're solving

The Tier 1 #1.5 eval (59 convos, 66.1% reach, $66.14) surfaced 8
recurring issue classes:

1. Hint leakage (~3% post-Haiku gate)
2. Wrong-topic locks (~12% on S4/S5 paraphrases)
3. Empty-subsection chunks silently filtered (13.5% of corpus)
4. Long-tail Dean QC retry loops (~10% of convos, 28-50 min wall)
5. Teacher does two jobs (decide + write)
6. Help-abuse runs the full pipeline ($0.038 per turn)
7. mem0 only feeds rapport (no per-turn carryover)
8. Rapport is template-y (no student-state-aware tone)

Both Option B and Option C resolve issues 2-8 identically. They
differ on how to handle Issue 1 (hint leakage).

---

## What's shared between B and C

Both options include this layer stack:

```
Student message
       │
       ▼
PRE-FLIGHT (parallel Haiku × 2 + whitespace guard)
  - haiku_help_abuse  → catches "just tell me" demands
  - haiku_off_domain  → catches chitchat / jailbreaks
  If any fires → templated redirect, Dean SKIPPED ($0.001 turn)
       │
       ▼
DEAN PLANNER (Sonnet — _setup_call EXPANDED to also emit TurnPlan)
  Same _setup_call as today; output schema gets bigger.
  TurnPlan = {scenario, hint_text, permitted_terms, forbidden_terms,
              apply_redaction, tone, shape_spec, carryover_notes}
       │
       ▼
[REDACTION LAYER — Option B only; Option C skips this]
       │
       ▼
TEACHER (Sonnet, single entry point, self-policing)
  for attempt in 1..3:
    draft = teacher.draft(turn_plan, chunks, history)
    parallel Haiku × 3: leak_check / sycophancy_check / shape_check
    if all_ok → ship
  if 3 fails → 1 Dean escalation with fresh hint
       │
       ▼
   Ship to student
```

Both options also include:
- Empty-subsection skip removal (`dean.py:~1340`, 30 min fix)
- Haiku topic classifier (replaces fuzzy fallback at confidence ≥ 0.75)
- Retry cap (3 Teacher attempts + 1 Dean escalation, then ship anyway)
- Single Teacher entry point (4 methods → 1)
- Haiku rapport classifier (greeting tone awareness)
- mem0 string flows verbatim into TurnPlan.carryover_notes
- Sonnet Dean QC removed; replaced by Haiku shape_check

---

## Where B and C differ — leak prevention

### Option B — Structural redaction

Before Teacher reads chunks on hint turns, deterministically replace
the answer + aliases with `[REDACTED]`:

```
Original chunk:
  "The sinoatrial (SA) node, located in the right atrium near
   the superior vena cava, is the heart's primary pacemaker..."

After redaction (what Teacher sees):
  "The [REDACTED] ([REDACTED]) node, located in the right atrium
   near the superior vena cava, is the heart's primary [REDACTED]..."
```

Teacher physically cannot regurgitate the answer because the answer
isn't in its input. Self-check stays as backstop.

**Expected leak rate**: near 0% structurally.

**Risks**:
- Voice quality: Teacher with redacted context may write blander prose
- Synonym enumeration burden: `_lock_anchors_call` must enumerate ALL
  forms found in chunks. Missed synonym = leak survives
- Common-word answers: if locked_answer is "node", redaction
  over-blanks unrelated mentions ("lymph node", "AV node")
- Clinical phase exception: redaction off for affirm/clinical/close
- Implementation: +1 file (`conversation/redaction.py`, ~50 lines),
  expanded `_lock_anchors_call` prompt, alias-completeness unit test

### Option C — Rules + Dean planning + Haiku catcher

Teacher sees the full chunks (with answer). Dean's TurnPlan gives
Teacher: the pre-decided hint content, the forbidden terms list, and
the shape rules. Teacher writes a hint following the rules. Three
Haiku self-checks catch any leaks that slip through.

```
Teacher input (same as today, but with TurnPlan structure):
  - Full chunks (answer visible)
  - hint_text from Dean's pre-generated hint_plan[hint_level]
  - forbidden_terms = [answer, aliases, derived synonyms]
  - shape_spec = {max=4 sentences, exactly_one_question}
  - tone = "warm_tentative"
```

**Expected leak rate**: ~3% (today's measured rate, since this is
basically today's leak-prevention model with better Dean planning).

**Risks**:
- Leak rate stays at ~3% — depends on Haiku catcher's 96.7% accuracy
- Less paper-claimable than B's structural prevention

---

## Side-by-side

| Dimension | Option B (redaction) | Option C (rules + planning) |
|-----------|---------------------|----------------------------|
| Resolves Issue 1 (leakage) | Structurally (~0% leak) | Statistically (~3% leak) |
| Resolves Issues 2-8 | Yes | Yes (identical) |
| Files added | 2 (preflight.py, redaction.py) | 1 (preflight.py) |
| Lines added | ~+250 | ~+200 |
| `_lock_anchors_call` work | Major expansion (chunk-level synonym enum + completeness assertion) | Minor (optional alias hints) |
| Voice quality risk | MEDIUM (untested with redacted context) | LOW (Teacher has full context) |
| Edge case: common-word answers | Breaks (over-redaction) | Works fine |
| Clinical phase | Needs `apply_redaction = false` exception | No exception needed |
| Test rewrite | ~4 hr | ~3 hr |
| Implementation effort | ~14 hr post-paper | ~10 hr post-paper |
| Per-conv cost vs today's $1.12 | ~$0.91 (-19%) | ~$0.93 (-17%) |
| First-token latency | ~1.7s | ~1.7s |
| Help-abuse turn cost | $0.001 (-97%) | $0.001 (-97%) |
| Wrong-topic lock rate | ~3% (vs 12% today) | ~3% (vs 12% today) |
| Long-tail retry rate | 0% (capped) | 0% (capped) |
| Composability | n/a (B is the leak-prevention add-on) | C → B is a small Phase 6 add-on |
| Paper claim | "Teacher physically cannot leak" | "Leak rate empirically <X% via [stack]" |

---

## The decision

The 8 operational issues get fixed identically by either option. The
real question:

> **Does the paper need a structural-prevention claim ("Teacher
> physically cannot leak"), or does it need empirical leak-rate
> numbers + the clean architecture story?**

**If structural claim is required**: Option B. Accept the engineering
risk and the voice-quality unknown.

**If empirical numbers are sufficient**: Option C. Lower risk, faster
to ship, and B can be added on top later if leak rate isn't
acceptable.

---

## Nidhi's lean

C, with B as Phase 6 if measured leak rate is too high. Reasons:

1. C → B is a small additive change — TurnPlan already has the
   `apply_redaction` boolean field, defaulted to false in C. Flipping
   to conditional in a future phase is ~4 hr work.
2. Voice quality A/B test for B requires ~10 turns blind-rated, which
   we can't do pre-paper (May 6 deadline).
3. The 8 operational wins land identically in either option, so the
   paper-narrative core ("we resolved 8 known issue classes via [v2
   layer stack]") is the same.
4. C avoids the synonym-enumeration burden, which itself is a brittle
   layer (one missed synonym = one leak — same statistical failure
   mode as today, just relocated).

But this is a 60/40 call, not 90/10. The structural-prevention paper
narrative for B is real.

---

## What needs to happen regardless of choice

These are pre-decision. Both options need them:

**Phase 0 (until May 6)**: don't touch the branch. Paper writes on
current 66.1% reach.

**Phase 1 (optional pre-paper, ~2 hr)**:
- Drop `if not key[2]: continue` skip in `dean.py:~1340` (Issue 3)
- Hard-cap Dean QC retries at 3 (Issue 4 partial fix)

These are safe, contained, improve eval reproducibility, and are
common to B and C. Worth shipping pre-paper if you have an hour.

**Post-paper sequencing** (B or C):

| Phase | What ships | Effort | Common to B+C? |
|-------|-----------|--------|----------------|
| 2 | preflight.py + cheap exits | ~3 hr | YES |
| 3 | TurnPlan schema + Teacher refactor + self-loop | ~6 hr | YES |
| 4 | haiku_topic_classify + 50-case validation | ~3 hr | YES |
| 5 | Paired eval (10 today + 10 v3, same seeds) | ~2 hr | YES |
| 6 | redaction.py + alias enum + clinical exception | ~4 hr | **B ONLY** |

**Total**:
- C: Phase 2-5 = ~14 hr post-paper
- B: Phase 2-6 = ~18 hr post-paper

---

## What I need from you

1. Quick read on B vs C — which one are you leaning?
2. Do you have a strong opinion on whether the paper claim needs
   structural-prevention language, or is "empirical leak rate <X%"
   enough?
3. Anything in either design that pings as "this is going to break"
   based on parts of the codebase you know better than me?
4. Are you OK with the Phase 0 (no pre-paper refactor) call? Or do
   you want to push for Phase 1 to ship before May 6?

Respond on this doc or ping me on Slack.

---

## Pointer files (for context)

- `progress_journal/2026-05-02_03-52-44_tier1_1_5_50conv_eval_results.md`
  — eval that surfaced the 8 issues
- `docs/HANDOFF_ARUN_2026-05-02.md` — current branch state
- `docs/CLEAN_ARCHITECTURE_PROPOSAL_v2.md` — full v2 design (= Option B)
- `docs/TODAY_VS_V2_CODE_MAPPING.md` — line-by-line mapping
- `conversation/dean.py:~1340` — Issue 3 skip line (Phase 1 fix)
- `conversation/teacher.py` — Phase 3 refactor target
- `conversation/classifiers.py` — pattern for new Haiku calls
