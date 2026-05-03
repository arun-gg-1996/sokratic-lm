# Brief for Codex — Architecture Plan Audit

**Owner:** Arun
**Date:** 2026-05-02
**Purpose:** Brief Codex on what to audit, what code to compare against, and what plan to compare against.

---

## Your Task

Audit the **architecture plan** (`docs/AUDIT_2026-05-02.md`) against the **current code state** (Nidhi's branch). For each module / file / behavior:

- **KEEP** — current code matches the plan, no change needed
- **CHANGE** — current code exists but the plan modifies it
- **ADD** — plan introduces something not in current code
- **REMOVE** — plan removes something currently in code

Produce a punch-list grouped by file path (or by the audit doc's L-decision number). Flag any contradictions, ambiguities, or missing details where the plan doesn't tell you enough to implement.

---

## Where Things Live

### Code state to audit AGAINST

**Branch:** `nidhi/reach-gate-and-override-analysis` (HEAD `0747506`)

This is the most current runtime code:
- AWS Bedrock + Sonnet 4.6 swap (replaces Anthropic Direct API)
- Haiku-tier behavioral classifiers in `conversation/classifiers.py`
- K-of-N partial reach for multi-component locked answers
- X1 Ch20 corpus metadata fix + X2 callout chunk drop
- Letter-hint regex extensions (ALL-CAPS abbreviations, acronyms)
- Sonnet sycophancy patterns + contraction-bypass fix
- e2e regression harness in `scripts/run_e2e_tests.py` (8 starter scenarios)
- Tier 1 #1.5 eval results: 59 conversations, 66.1% reach, +27pp vs Apr 29 baseline

**Pull command:**
```bash
git fetch origin
git checkout nidhi/reach-gate-and-override-analysis
git log --oneline -20    # see her 16 commits
```

**Note:** This branch is **NOT merged into `main`**. `main` is at `4577ef4` (older state). Audit against `nidhi/reach-gate-and-override-analysis` since that's the freshest reflection of running code.

### Plan to audit (what we want to ship)

**File:** `docs/AUDIT_2026-05-02.md` (in the working tree on `main`)

**Status:** **uncommitted** — sits in working dir, not yet pushed. Read directly from the file.

**Stats:** ~1,835 lines, 79 numbered locked architectural decisions (L1–L80 with L14 deleted, L24 deferred, L35 retracted). Net: 77 active locked decisions.

**Path:** `/Users/arun-ghontale/UB/NLP/sokratic/docs/AUDIT_2026-05-02.md`

---

## Plan Doc Structure (11 sections)

| Section | L range | Topic |
|---|---|---|
| 1 | L1–L34 | Data layer + rapport core + topic mapper + opener + cards |
| 2 | L35–L41 | Gap-closing decisions (terminology, scorer updates, etc.) |
| 3 | L42 | Past-chats sidebar drawer removal |
| 4 | L43–L63 | Tutor flow (TurnPlan + Teacher self-policing + reach gate + memory_update) |
| 5 | L64 | Empty-subsection chunk skip removal |
| 6 | L65–L75 | Clinical phase (reuses tutor flow + clinical-aware mastery) |
| 7 | L76 | Ingestion + RAG metadata cleanup (no orphans, LLM-recovery) |
| 8 | L77 | VLM / image input flow |
| 9 | L78 | Domain genericity (physics swap) |
| 10 | L79 | Accessibility (TTS + STT, browser-native) |
| 11 | L80 | UX polish pass (8 sub-improvements) |

---

## High-Stakes Areas to Audit Carefully

These are the decisions most likely to require deep code changes:

1. **L9 — Topic Mapper LLM** — replaces fuzzy matcher (`retrieval/topic_matcher.py:match`). Drops UMLS / scispacy entirely. This is a major retrieval rewrite. Verify: is the fuzzy matcher actually still in use, or did Nidhi already replace it?

2. **L43 — Option C (no redaction)** — uses `forbidden_terms` in TurnPlan instead of structural redaction. Verify: does Nidhi's branch already have this pattern, or does it still use a different leak-prevention approach?

3. **L46 — TurnPlan schema** — single-source contract from Dean to Teacher. Verify: does Nidhi's branch have a TurnPlan-shaped contract, or does Dean still emit ad-hoc fields?

4. **L48 — 4 parallel Haiku checks** — replaces Sonnet Dean QC. Verify: which checks does Nidhi already have (`classifiers.py:haiku_*_check`)? Plan adds `haiku_pedagogy_check` (NEW) and `haiku_shape_check` (extended).

5. **L50 — Retry cap with feedback loop** — bounds Teacher retries at 3 + 1 Dean re-plan. Verify today's retry behavior — is it bounded?

6. **L1 / L2 — SQL migration** — moves session_summary, topics_covered, open_thread OUT of mem0 INTO a new SQLite database. Big change. Verify: today's storage is mem0 + per-student JSON file. Migration scope is real.

7. **L29–L34 + L37 — My Mastery page** — full UI page that may not exist today. Verify what UI exists for mastery viewing.

8. **L77 — VLM flow** — new feature entirely. Verify if any VLM code exists today.

9. **L78 — Domain genericity** — sweep through prompts + paths to remove hardcoded "anatomy" strings. Audit: how much hardcoding exists today?

10. **L80 — UX polish** — 8 frontend changes (counter panel, activity feed redesign, phase indicator, disabled state, transitions, loading states, mastery refinements, tooltips). Audit: what UI exists today vs what plan specifies.

---

## Companion Docs to Read

For full context (in this priority order):

1. **`docs/AUDIT_2026-05-02.md`** — THE plan (your audit target)
2. **`OPTION_B_VS_C_FOR_ARUN.md`** — Nidhi's architectural proposal that we discussed extensively. Plan picks Option C. Helps understand the rationale.
3. **`docs/HANDOFF_NIDHI.md`** — original handoff from a prior session (some items now superseded by AUDIT_2026-05-02; specifically P0/P1 items in HANDOFF_NIDHI map to L43–L62 in the plan).
4. **`docs/architecture.md`** — current architecture spec on Nidhi's branch.
5. **`progress_journal/`** — Nidhi's investigation journals. Read selectively for "why" context on her recent decisions.
6. **`docs/SESSION_JOURNAL_2026-04-30.md`** — decision log from prior work session.
7. **`README.md`** — current setup + flow overview.

---

## Things to Know About the Plan

### The doc has self-references (retractions, deferrals)

Some decisions explicitly retract or defer earlier ones — read each L section in full:

- **L14** — REMOVED (content was duplicated into L23, L27, L28). Numbering skips from L13 to L15.
- **L18** — RETRACTED a previously-locked "Resume Coronary Circulation" button. Final state: returning students always see 6 cards via TopicSuggester, no resume button.
- **L24** — DEFERRED (deferred-question handling). Topic mapper ships emitting only `student_intent: "topic_request"`. The deferred path gets added in a follow-up.
- **L35** — RETRACTED (mid-session reconnect via 24h auto-resume). Final state: no reconnect, browser close = session abandoned.

### The doc has cross-references that DO get propagated

Some decisions mention "extends L9" or "updates L46". The cross-referenced sections have been updated to include the new content inline (e.g., L46's TurnPlan schema actually shows the `image_context` field added by L77; L2's sessions table actually shows the `status` and `image_path` columns added by L21 and L77). So you don't need to chase the cross-references — the canonical content is in the original L sections.

### Implementation timing per the plan

Per L25 + L41: implementation ships in one execution pass — hours, not weeks (because Claude implements). Don't audit for "is this realistically scoped" — just audit for "does the plan tell us enough to build it." Treat scope as the user's call.

---

## Output Format Codex Should Produce

Suggested structure for your audit report:

```markdown
# Plan Audit Report

## Summary
- Total decisions audited: 77 active L-decisions
- KEEP / CHANGE / ADD / REMOVE breakdown
- Critical contradictions: N
- Ambiguities flagged: N

## Per-Module Punch List

### conversation/dean.py
- L9 (topic_mapper_llm): ADD — no equivalent today
- L43 (Option C): KEEP — Nidhi's branch already uses forbidden_terms via classifiers
- L46 (TurnPlan schema): ADD — today's _setup_call returns ad-hoc fields, not TurnPlan
- ...

### conversation/teacher.py
- L49 (single Teacher entry point): CHANGE — today has 4 functions, plan collapses to 1
- ...

### memory/
- L1 (SQL/mem0 split): MAJOR ADD — SQLite doesn't exist today, all storage is mem0 + JSON
- ...

### frontend/src/
- L29 (My Mastery tree): ADD — verify if this page exists
- L80 (UX polish): MIXED — some elements exist, others new
- ...

### scripts/build_topic_index.py
- L19 (display label batch): ADD
- L38 (summary coverage check): ADD or EXTEND
- L40 (rewrite trigger): ADD
- L76 (ingestion metadata cleanup): MAJOR ADD
- ...

## Contradictions / Ambiguities Found

1. [If you find any — describe + propose resolution]
2. ...

## Implementation Readiness Assessment

For each major area, rate readiness 1-5:
- Spec completeness (is the plan precise enough to implement without more questions?)
- Code-state clarity (do you know what's in the existing code?)
- Risk (what could break)
```

---

## Direct Contact

If something in the plan is unclear, ambiguous, or contradictory, flag it explicitly in the audit report. The plan was locked through a long iterative session; some decisions reference "we discussed X" without spelling it out.

If you need to verify code state on Nidhi's branch:
```bash
git fetch origin
git checkout nidhi/reach-gate-and-override-analysis
# inspect any file
git checkout main  # come back
```

The user (Arun) will read your audit report and decide which items to act on before implementation begins.

---

## Quick Status Recap

| Item | Where | State |
|---|---|---|
| Current runtime code | `nidhi/reach-gate-and-override-analysis` (HEAD `0747506`) | live |
| Architecture plan | `docs/AUDIT_2026-05-02.md` | uncommitted, source-of-truth |
| Reference proposal | `OPTION_B_VS_C_FOR_ARUN.md` | committed, plan picks Option C |
| Prior handoff (partially superseded) | `docs/HANDOFF_NIDHI.md` | committed |
| Codex's job | audit the plan against the current code | TODO |

Good luck.
