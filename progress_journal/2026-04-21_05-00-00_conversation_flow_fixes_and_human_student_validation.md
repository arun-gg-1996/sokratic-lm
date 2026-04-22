# Conversation Flow Fixes + Human-Student Validation

Date: 2026-04-21

## Scope
- Applied P0 + P1 conversation-quality fixes identified by a two-run audit (S2 moderate, S4 overconfident) where multiple demo-breaking bugs were visible in live transcripts.
- Validated against three fresh transcripts (S1 strong, S2 moderate, S4 overconfident) driven by hand-authored student messages — NO LLM student simulator. Driver: `scripts/human_student_driver.py`.

## Bugs fixed in this pass

### B1 — Opt-in parser required literal "yes"/"no" (demo-killer)
- Before: `nodes.py:172` → `if student_msg == "yes": …`
  Any of "sure", "yeah", "go ahead", or a long substantive sentence was treated as ambiguous and re-asked the opt-in, looping forever on the demo.
- After: `conversation/nodes.py` — added `_classify_opt_in()` with prefix/keyword sets, plus an **implicit yes** path when the reply is ≥6 words (so detailed clinical answers don't loop the opt-in).

### B2 — Verbose `locked_answer` caused correct answers to classify as partial (demo-killer)
- Before: lock-anchors prompt said "2-10 words; hard max 15"; sanitizer rejected only `> 15` words. Models emitted full descriptive sentences like `"axillary nerve c5 c6 from posterior cord through quadrangular space innervates deltoid and teres minor"` and the sanitizer let them through.
- After:
  - `config.yaml` `dean_lock_anchors_static`/`_delta`: requires **1-5 words**, **noun phrase only**, explicit correct vs wrong examples.
  - `conversation/dean.py::_sanitize_locked_answer`: cap tightened to `> 6` words, plus sentence-marker rejection (`innervates`, `arises`, `branches`, `from the`, `through the`, etc.).
  - `_lock_anchors_repair_call` message upgraded to match.

### B3 — Topic-engagement cards invisible to non-UI clients
- Before: Teacher returned `{question, options}` but only `question` was appended to the tutor message. In any plain-text client (including our driver and, I'd bet, a poorly-configured UI render), the student had nothing to pick.
- After: `conversation/dean.py` run_turn → after `draft_topic_engagement`, the tutor message inlines numbered options and a "Reply with a number (1-N) or paste the option text" instruction. UI can still render cards in parallel.
- Also added numeric / ordinal selection handling in `_match_topic_selection` (accepts `"1"`, `"first"`, `"option 2"`, `"let's do #3"`, etc.).

### B4 — Rambly free-text accepted as topic
- Before: "I'll go with the first one about the axillary nerve pathway." was stored verbatim as `topic_selection`. Downstream retrieval and anchor lock ran on a sentence, not a topic.
- After: free-text fallback requires `1 ≤ tokens ≤ 10`; longer replies trigger a polite reprompt.

### B5 — "Narrower focus" loop with no cap
- Before: anchor lock failure → reprompt forever with no counter; every new attempt hit the same ambiguous topic.
- After: `state["debug"]["anchor_fail_count"]` counter. After **2 failures**, a deterministic fallback `_deterministic_anchor_fallback()` scans top retrieved propositions for a plausible 2-4-word anatomical noun phrase and uses that as `(locked_question, locked_answer)`. If nothing extractable, graceful "let's try a different angle" close — no infinite loop.

### B6 — Hardcoded `anatomy` in prompts blocked domain swap
- Replaced every occurrence of the bare word "anatomy" (not the `{domain_*}` placeholders) in `config.yaml` user-facing prompts. Changed fields:
  - `difficulty_rating`
  - `teacher_rapport_delta`
  - `teacher_socratic_static`/`_delta`
  - `dean_setup_classify_static`/`_delta`
  - closeout prompts referencing "connect anatomy to function"
- The `domain` block (`config.yaml:44-50`) was already parameterized; this pass aligns the prompt bodies with it.

### B7 — Card sub-angle overrode student's core question (discovered during validation)
- First validation run (S1 supraspinatus): card 1 was "Anatomical position of supraspinatus". Anchor lock picked `locked_answer="supraspinous fossa"` and the student was told they were wrong for naming "supraspinatus".
- Fix: `dean_lock_anchors_dynamic` now appends "CRITICAL ANCHOR-SELECTION GUIDANCE" that tells Dean to re-read the student's first message and use the interrogative form ("which muscle" → muscle name; "which nerve" → nerve name). Initial attempt with concrete examples leaked an example term into one run's output — removed examples, kept abstract rules.

## Validation transcripts (after fixes)

All three runs in `data/artifacts/human_convos/`. Driver: operator-authored student messages (no LLM student simulator). Model: claude-haiku-4-5-20251001 (both teacher and dean).

| Profile | Topic | reached | hint_final | locked_answer | Cost | Notes |
|---|---|---|---|---|---|---|
| S2 moderate | deltoid / axillary | ✅ True | 2 | `"quadrangular space"` | $0.12 | Reached via card pick "1", 2 wrong guesses, then correct. Clean opt-in, good clinical closeout. Anchor tangential but didn't break flow. |
| S1 strong | supraspinatus initiates abduction | ✅ True | 1 | `"supraspinatus muscle"` | $0.10 | First-try correct after card pick. Lenient opt-in worked ("Yes please"). Closeout praised the specific clinical signs the student named. |
| S4 overconfident | wrist drop / radial | ❌ False | 4 (exhausted) | `"radial nerve"` | $0.15 | Canonical 2-word anchor. Student stuck on "ulnar". System exhausted hints, then closed gracefully with targeted review advice. No loops, no re-prompts. |

### Before vs after snapshots

**S2 v1 (before fixes) — [full transcript](data/artifacts/human_convos/human_S2_381f94f5_2026-04-21T04-18-25.json):**
- `locked_answer = "axillary nerve c5 c6 from posterior cord through quadrangular space innervates deltoid and teres minor"` (15 words)
- Student said "axillary nerve" → classified partial → hint exhausted → reached=False
- Opt-in "yes please" not recognized → re-asked

**S2 v4 (after fixes) — [full transcript](data/artifacts/human_convos/human_S2_a1db1951_2026-04-21T05-04-59.json):**
- `locked_answer = "quadrangular space"` (2 words)
- Student said "axillary nerve" → reached=True on turn 5
- Opt-in "Sure, let's do the clinical question." → accepted
- Clinical answer → graceful memory_update close with accurate per-fact feedback

**S4 v1 (before fixes) — [full transcript](data/artifacts/human_convos/human_S4_3a764646_2026-04-21T04-22-44.json):**
- Tutor asked the clinical opt-in **three times** despite "Sure, go ahead with the clinical question" → detailed clinical answer
- Topic_selection stored as student confusion: `"No, I really think it's ulnar. Claw hand and wrist drop go together, right?"`

**S4 v2 (after fixes) — [full transcript](data/artifacts/human_convos/human_S4_7ce5fa91_2026-04-21T04-41-33.json):**
- Card pick "1" worked → topic_selection is the actual card text
- `locked_answer = "radial nerve"` (2 words)
- Student stubbornly wrong 3 times → hints 1→2→3→4 exhaust → graceful memory_update close
- No loops, no duplicate opt-in prompts

## Files touched

| File | Change |
|---|---|
| `conversation/nodes.py` | Added `_classify_opt_in` + prefix sets; lenient opt-in; implicit-yes on long reply |
| `conversation/dean.py` | Sanitizer cap tightened; numeric/ordinal card picks; inline numbered options in tutor message; free-text length guard; 2-attempt anchor-fail breaker; `_deterministic_anchor_fallback` helper; tighter repair-call instruction |
| `config.yaml` | Anchor lock prompts rewritten for 1-5-word canonical noun phrases; dynamic prompt adds core-intent guidance; every hardcoded "anatomy" replaced with `{domain_short}`/`{domain_name}` in user-facing prompts |
| `scripts/human_student_driver.py` | **New** — deterministic driver that feeds pre-authored student messages into the graph, rolls over turn_trace into all_turn_traces, dumps full export |
| `data/artifacts/human_scripts/*.json` | **New** — three pre-authored student scripts: S1_bicepstendon, S2_deltoid v2, S4_wristdrop v2 |

## Residual issues (not fixed in this pass)

1. **Anchor tangential to student intent on card-heavy topics.** E.g., S2 v4 anchored `"quadrangular space"` when the student's intent was really "axillary nerve". The Dean's semantic classifier is generous enough that reached=True still fires, but ideally the anchor should match what the student literally has to name. Path-forward: make `_lock_anchors_call` pass `state["messages"][0]` (the student's first message) explicitly labeled, separate from topic_selection, so the prompt can't conflate them.
2. **Banned lead-ins occasionally slip through.** S4 v2 turn 4: `"I hear you—…"` violates the banned-prefix list in `config.yaml:229-237`. Dean QC check exists but didn't catch this in the reviewed transcript. Needs a deterministic check on the teacher draft before `_quality_check_call` even runs.
3. **Reached=True sometimes fires on near-adjacent answers.** S2 v4 anchored `"quadrangular space"`, student said "axillary nerve" → reached=True. Works out pedagogically but means the reached-answer gate is looser than the locked_answer would imply.
4. **Ingestion generalizability still hardcoded.** `ingestion/*.py` still has 5 files with `"ot"` literals. Not touched in this pass — deferred to a dedicated ingestion-generalization session.

## Budget

- This pass's spend: ~$1.00 (7 live conversations across iterations + prompt tweaks)
- Prior validation spend: ~$0.55
- Cumulative session spend: ~$1.55 of $9 budget
- Remaining: ~$7.45. Well under the $7.50 stop threshold.

## How to rerun

```bash
.venv/bin/python scripts/human_student_driver.py \
    --profile S1 \
    --script data/artifacts/human_scripts/S1_bicepstendon_v1.json
# outputs → data/artifacts/human_convos/human_S1_<uuid>_<ts>.json
```

Author new scripts under `data/artifacts/human_scripts/` — a JSON with
`{"profile": "...", "messages": [...]}`.
