# Sokratic Tutor — Handoff to Nidhi (2026-04-30)

This document is your starting point. Everything you need to evaluate the
current system, run the deployment-ready test pass, and ship two
remaining features (vision model flow + secondary textbook integration).

**Deadline**: Sunday EOD (extensive testing + deployment).

Read order:
1. This doc — **start with the "Priority 0 / 1 issues from v3 batch" section below**
2. `README.md` §First-time setup — clone → .env → HF data pull → Qdrant → run
3. `data/artifacts/eval_run_18/REPORT_v1_v2_v3.md` — the v3 eval batch findings (this is the qualitative + quantitative comparison; it's the ground truth for what's actually broken)
4. `docs/SESSION_JOURNAL_2026-04-30.md` (decision log)
5. `docs/EVALUATION_FRAMEWORK.md` (eval framework spec)
6. Codebase familiarisation: `conversation/dean.py` + `evaluation/quality/`

---

## Priority 0 / 1 issues from the v3 batch (start here)

These are the issues surfaced by the v3 read-each-dialog qualitative review on
2026-04-30. They eclipse Tasks 1–6 below in priority — **fix or characterize
these first**, then run the simulation eval against the fixes. Full context in
`data/artifacts/eval_run_18/REPORT_v1_v2_v3.md`.

### P0-A. Coverage-gate runaway loop (`triple2_s1` pattern)

**Symptom.** Student asks a legitimate textbook topic that the matcher routes
to a node whose retrieval fails the coverage gate. The dean then suggests 3
alternative cards, the student picks one — coverage gate rejects again.
Repeat. In v3 this hit 32 turns / 14 rejected topics in a single session.

**Concrete example (from `eval18_triple2_exploratory_session1.md`):**
- Turn [1] student: *"Walk me through chemical digestion of carbohydrates"*
- Turn [2] tutor: *"Pick one of these or type a more specific term: 1. Respiratory Rate and Control of Ventilation, 2. The Epidermis, 3. Descending Pathways"*
- Student then accepts cards through 14 successive rejections, including: **The Liver**, **Atoms and Subatomic Particles**, **Mendel's Theory of Inheritance**, **Conduction System of the Heart**, **Cognitive Abilities**, **Transport across the Cell Membrane** — all of which exist in the OpenStax corpus.
- Final state: `locked_question=""`, `locked_answer=""`, `hint_final=0`, no penalty triggered, session ran out of turns at 32.

**What to investigate.**
1. `retrieval/retriever.py` coverage gate threshold (look for `out_of_scope_threshold` and the cross-encoder cutoff at -3.0 mentioned in the original RAG handoff).
2. Why suggested cards fail their own gate at lock time. Either:
   - The card-suggestion pool isn't filtered by `teachable=True` from `topic_index.json` (it should be — see `topic_matcher.sample_diverse`).
   - OR the gate has tightened post-Phase-1 in a way the card pool doesn't know about.
3. Add a hard cap: after N=4 consecutive coverage-gate rejections in one session, fall back to a "let's freeform — what specifically interests you?" path or terminate gracefully.

**Files to look at.** `retrieval/retriever.py`, `conversation/dean.py` (coverage gate handler), `retrieval/topic_matcher.py:sample_diverse`.

**Validation.** Add a `T_coverage_gate_runaway` test scenario to `scripts/test_reached_gate_e2e.py` that simulates 4× failed locks and asserts graceful fallback.

---

### P0-B. Lock drift on free-text questions (matcher fuzzy-vs-semantic mismatch)

**Symptom.** Student asks a topical free-text question. The fuzzy
`token_set_ratio` matcher returns a `strong`-tier match (score ≥ 78) on a TOC
node that shares surface tokens but isn't the right concept. Result: the
locked question is conceptually wrong, the entire session is misdirected, but
no penalty fires because the `STRONG_MIN=78` threshold was met.

**The threshold bump didn't fix this.** v2 had `STRONG_MIN=65`; we bumped to
78 in Round 5 expecting these cases to fall to "borderline" and surface
disambiguation cards. They didn't — fuzzy still scores >78 on these.

**Concrete examples from v3:**

| Session | Student asked | Locked as | Why it scored high |
|---|---|---|---|
| `solo3_S3_session1` | *"What is the structure of a long bone?"* | "What are the three types of muscle tissue, and how do their structures differ?" | Tokens "structure" and "tissue/muscle" overlap with several anatomy nodes; "long bone" got drowned out |
| `pair3_disengaged_session1` | *"What are the phases of the cardiac cycle?"* | "What are the three distinct phases that occur during a single muscle twitch...?" | Both contain "phases" — fuzzy doesn't distinguish "cardiac cycle" from "muscle twitch" |
| `pair3_disengaged_session2` | *"How does heart rate affect cardiac output?"* | "What is the difference between systolic and diastolic pressure...?" (pulse pressure) | "blood pressure" subsection token-overlaps with "heart rate" / "cardiac output" |

**Why threshold tuning is at its ceiling.** RapidFuzz's `token_set_ratio`
operates on surface tokens. It cannot encode "long bone is a structural
classification of bone, distinct from muscle types" without a semantic model.

**The actual fix.** Replace (or augment) the fuzzy scorer with embedding
cosine similarity:

```python
# retrieval/topic_matcher.py — sketch
def _score(self, query: str, e: TopicMatch) -> float:
    fuzzy = self._fuzzy_score(query, e)             # current path
    if fuzzy < BORDERLINE_MIN:
        return fuzzy                                # short-circuit cheap reject
    sem = self._embed_cosine(query, e.full_path)    # NEW: add embedding scorer
    # take min: BOTH fuzzy and semantic must agree on "strong" before we lock
    return min(fuzzy, sem * 100)
```

Use the same OpenAI `text-embedding-3-large` already wired up for chunk
embeddings — embed the TOC node titles once at startup (~360 entries × <10
tokens each = trivial cost). Cache them on the `TopicMatcher` instance.

**Validation.** Re-run the v3 eval batch. Expect `solo3`, `pair3_s1`,
`pair3_s2` to fall to "borderline" tier and surface disambiguation cards
instead of auto-locking. EULER `relevance` should recover from 0.733 toward
v2's 0.811.

**Files.** `retrieval/topic_matcher.py`, possibly `retrieval/retriever.py` if
we share embeddings.

---

### P1-A. Multi-anchor support for list-style answers

**Symptom.** Round 5 added a 1–5 word hard cap on `locked_answer`. This works
for single-concept questions ("renal corpuscle", "pulse pressure", "myelin")
but breaks for questions whose canonical answer is a list of N items.

**Concrete example (`triple1_progressing_session3`):**
- `locked_question` = *"What are the five mechanisms by which the small intestine absorbs nutrients across the epithelial cells?"*
- `locked_answer` = `"active transport passive diffusion facilitated diffusion co transport endocytosis"` (12 words)
- The cap was supposed to prevent this. The lock LLM violated it because the question's canonical answer is genuinely 5 distinct terms.

Also `pair3_disengaged_session1`: `latent period contraction phase relaxation phase` (6 words — borderline).

**Two fixes to consider:**

1. **Multi-anchor schema.** Replace `locked_answer: str` with `locked_answers: list[str]` where each item is 1–5 words. The reached-answer gate matches if the student names ≥K of N items (K=N for strict, K=ceil(N/2) for lenient). Update:
   - `conversation/state.py` — schema change
   - `conversation/dean.py` — `reached_answer_gate` matching logic
   - `config/base.yaml` — `dean_lock_anchors_static` prompt to emit a list
   - `evaluation/quality/penalties.py` — adjust cap-violation check

2. **Cleaner alternative — split N-item questions into N sub-questions.** The lock prompt could refuse to lock a question whose answer requires >5 words and instead emit a "split this into 2–3 sub-questions" instruction. Then the dean walks the student through each sub-question sequentially. More work, but pedagogically better — it forces atomic checks.

**Pick one.** I'd lean #1 (less disruptive, ships faster). Document the choice
and validate via the gate test suite.

**Files.** `conversation/state.py`, `conversation/dean.py`, `config/base.yaml`
(`dean_lock_anchors_static` + `prompts.reached_answer_step_b`),
`evaluation/quality/penalties.py`.

---

### P1-B. Subtle "name-the-feature → student-parrots" leak

Round 5's teacher prompt eliminated the obvious anagram-leak pattern (v2's
*"starts with 'P' and ends with 'pressure'"* did NOT recur in v3 ✓). But a
subtler pattern survived: tutor names a defining feature, student parrots the
term back, tutor accepts.

**Concrete example (`solo3_S3_session1` turns 4–6):**
- T4 tutor: *"...the material doing the work—is what we're naming. Since you know it's attached to bones and powers voluntary movement, **what structural feature would help you identify it under a microscope?**"*
  — That's a Wikipedia-grade definition of skeletal muscle.
- T5 student: *"Oh wait, is it muscle? Like skeletal muscle with all those striations?"*
  — Student didn't reason; they pattern-matched the description.
- T6 tutor: *"Excellent—you've nailed skeletal muscle and its striations. Now, **you know cardiac muscle is in the heart and also has striations**, but skeletal muscle has many nuclei along its edges while cardiac muscle typically has just one nucleus in the center."*
  — Tutor introduces "cardiac muscle" by name. Student didn't.
- T7 student: *"Yeah, so like... would it be the number of nuclei and where they're located? That's the main difference you just said, right?"*

The locked_answer was "skeletal cardiac and smooth muscle". The tutor named
all three components. The student named none independently.

**Concrete example #2 (`triple1_progressing_session2` T10):**
- After 4 turns of student stonewalling, tutor caved: *"Sodium is actively pumped out of the tubule into the tissue, creating a concentration gradient. Water then moves passively across the membrane to dilute that high sodium concentration—this is called osmosis."*
- That's the question's mechanism explained verbatim.

**The fix is hard because the tutor isn't violating an obvious rule.**
Naming a defining feature isn't "leak" by Round 5's prompt rules. Two
directions:

1. **Strengthen the gate.** `reached_answer_gate` Step A currently checks
   token overlap of *student utterances* against the locked anchor. Add a
   companion check: count how many of the locked answer's component tokens
   appeared in the *tutor's* utterances first. If the tutor named ≥K of N
   answer components before the student did, the gate should treat the
   reach as "tutor-led recovery" (not full reach) and downgrade mastery.

2. **Tighten the prompt with concrete patterns.** Add to
   `teacher_socratic_static`:
   > **Forbidden: defining-feature reveals.** Do not describe a structure's
   > defining or diagnostic feature in a way that uniquely identifies the
   > target term. If the question is "name the muscle type with striations
   > attached to bones for voluntary movement", do not say "the tissue with
   > striations attached to bones for voluntary movement is what we're naming"
   > — that is the answer in question form. Ask instead about a
   > *non-diagnostic* property and let the student abstract.

Probably do BOTH. (1) catches it post-hoc as a quality signal; (2) reduces it
proactively.

**Files.** `conversation/dean.py` (gate logic), `config/base.yaml`
(`teacher_socratic_static`).

---

### P1-C. Hint indicator UI is ugly

**Symptom.** The hint badge currently renders as raw markdown italic
appended to the tutor message body. In the live UI it shows as the literal
underscored string at the bottom of the bubble, e.g.:

> "What word might describe tissue like that? `_— Hint 1 of 3 —_`"

It's inline with the message, has no visual hierarchy, and the `<StreamingText>`
renderer doesn't process markdown — so the underscores are literally visible.
Looks unfinished. (User feedback 2026-04-30: "the way hints are there is so
poor looking now.")

**Where the string is emitted.** `conversation/dean.py:1834–1862`:

```python
if cur_hint == max_hint:
    suffix = f"\n\n_— Last hint ({cur_hint} of {max_hint}) — give it your best try. —_"
else:
    suffix = f"\n\n_— Hint {cur_hint} of {max_hint} —_"
approved_response = f"{approved_response}{suffix}"
```

The comment on the block already anticipates this: *"Frontend can detect
the marker and style as a small amber pill if desired."*

**Where the bubble renders.** `frontend/src/components/chat/MessageBubble.tsx:50`
calls `<StreamingText text={message.content} ... />` which dumps the raw text.

**The fix (suggested approach).** Don't append the suffix as text at all —
emit it as **structured metadata** on the message and let the React component
render it as a styled chip outside the bubble flow.

1. **Backend** (`conversation/dean.py` + `conversation/state.py`):
   - Add a `hint_indicator` field on the appended message dict:
     ```python
     state["messages"].append({
         "role": "tutor",
         "content": approved_response,           # WITHOUT the suffix
         "phase": "tutoring",
         "hint_indicator": {                     # NEW
             "current": cur_hint,
             "max": max_hint,
             "is_last": cur_hint == max_hint,
         },
     })
     ```
   - Stop appending the markdown suffix to the content.

2. **Backend WS payload** (`backend/api/chat.py`): include `hint_indicator`
   in the streamed/persisted message envelope so the frontend receives it.

3. **Frontend** (`frontend/src/components/chat/MessageBubble.tsx` +
   `types.ts`):
   - Add `hint_indicator` to the `ChatMessage` type.
   - Render as a small chip below the message body (or top-right corner of
     the bubble), e.g.:
     ```tsx
     {message.hint_indicator && (
       <div className="mt-2 flex items-center gap-1.5 text-xs">
         <span className={[
           "rounded-full px-2 py-0.5",
           message.hint_indicator.is_last
             ? "bg-amber-100 text-amber-900 border border-amber-300"
             : "bg-slate-100 text-slate-700 border border-slate-300"
         ].join(" ")}>
           {message.hint_indicator.is_last
             ? `Final hint (${message.hint_indicator.current}/${message.hint_indicator.max})`
             : `Hint ${message.hint_indicator.current} of ${message.hint_indicator.max}`}
         </span>
       </div>
     )}
     ```
   - Optional: add a small icon (lightbulb / hint-bulb) for the regular
     case; switch to a warning icon for `is_last`.

4. **Eval/scorer compatibility.** The eval scorer reads `message.content`,
   so removing the suffix is fine — but verify that no scoring rule keys on
   the literal "Hint X of Y" string. Grep `evaluation/quality/` for "Hint "
   and adjust if needed.

5. **Streaming.** `<StreamingText>` types out the content character-by-character.
   The hint chip should appear **after** streaming completes, not during —
   render it conditionally on `!message.shouldStream || message.streamComplete`
   so it doesn't flash in mid-stream.

**Acceptance.** Take a screenshot of any tutoring turn with `hint_level >= 1`.
The hint should be visually distinct from the message text — a small chip,
amber/slate background, separate line. The underscores must not be visible
anywhere.

**Bonus polish (defer if time-pressed).** Consider also styling the
"step counter" UX in the sidebar's debug panel similarly: right now the hint
counter is a plain "hint_level: 2" line.

**Files.**
- `conversation/dean.py` (lines 1834–1862) — stop appending suffix; emit metadata
- `conversation/state.py` — extend message TypedDict with `hint_indicator`
- `backend/api/chat.py` — pass `hint_indicator` through WS envelope
- `frontend/src/types.ts` — add to `ChatMessage`
- `frontend/src/components/chat/MessageBubble.tsx` — render the chip

---

### P0-C. Dean classification taxonomy is missing a "deflection" state

**Symptom.** The `student_state` enum (`incorrect`, `partial_correct`,
`low_effort`, `off_topic`, `question`, `correct`) has no slot for **meta-
disengagement** — student utterances like "can we stop?", "let's finish",
"I want to skip this", "next topic", "this is taking too long". The dean
correctly recognises these in its `critique` field but routes them to
`low_effort`, silently inflating the help-abuse counter and triggering
strike-4 hint advances the student didn't actually earn.

**Concrete example (thread `arun_e0158b1d` — local export 2026-04-30 09:39).**

Topic: erectile-tissue chambers. Student had successfully named "spongiosum"
earlier (genuine engagement). Then at turn 18 the student typed:

> *"let's finisg the conversation"* (sic — "finish")

Dean classified as `low_effort`. The classification's own critique field
admitted what was happening:

> *"Student explicitly requests to end the conversation ('let's finish the
> conversation') rather than attempt the locked question. **This is a
> dismissal/deflection** after multiple hints and failed attempts. … The
> request to finish signals disengagement and refusal to continue attempting,
> meeting low_effort criteria."*

The result:
- 4th consecutive `low_effort` strike fired one turn later → hint 3→4 advance
- The tutor's "let me be more direct" cap-narration turn revealed both
  `corpus spongiosum` and `corpora cavernosa` verbatim — exactly the
  leak-then-parrot pattern Round 5 was supposed to suppress
- If turn 18 had been off_topic / deflection, only 3 low_effort strikes
  would have accumulated and the leaky strike-4 narration wouldn't have fired

**Why it happens.** `low_effort` and `off_topic` model two different fail
modes — *fatigue/idk on-domain* (strike → hint advance) and *off-domain
content* (strike → terminate). Meta-conversation deflection is a third
mode the taxonomy doesn't capture. The dean's prompt has no enum slot for
"refusing to engage at all", so the LLM picks the closest available, which
is `low_effort`.

**Fix paths (pick one).**

1. **Add a `deflection` state.** Extend the enum, add a `deflection_count`
   counter that resets per session, and define a graceful wrap-up action at
   threshold (e.g., "okay, let's wrap up — share one thing you learned today")
   instead of either a hint advance or a hard terminate. This is the cleanest
   model but touches state, prompt, scorer, and UI.

2. **Fold meta-conversation requests into `off_topic`** with an explicit
   prompt addition listing concrete patterns. Cheaper to ship, but pollutes
   the off_topic counter (which is meant for off-domain content) and the
   off_topic terminate path may be too harsh — the student isn't being
   abusive, just exhausted.

I'd lean **option 1** for a thesis-quality story. The student's intent is
"end this gracefully," and conflating it with either fatigue or off-domain
abuse loses signal that's useful for both research and UX.

**Files.** `conversation/state.py` (counter + state schema),
`config/base.yaml` (`dean_setup_classify_static` — add `deflection` to enum
+ examples), `conversation/dean.py` (counter handler + threshold action),
`evaluation/quality/penalties.py` (don't penalise this pathway as a leak).

**Validation.** Add a `T_deflection` test: pre-locked topic, student says
"let's finish" three times → expect `deflection_count` increments + a
graceful wrap-up turn at threshold (not a hint advance, not a terminate,
not a leak).

---

### P1-D. Always emit a textbook-grounded summary at session end

**Why.** The closure (`memory_update`) message currently varies wildly. On
reach=True it's a freeform "you did well today, keep practicing"; on
reach=False it's "we'll come back to this next session." Neither anchors the
**actual answer** to the textbook. From a thesis-grounding standpoint, every
session should end with a brief, citation-bearing description of the locked
answer regardless of whether the student reached it — that's what makes the
system *demonstrably* textbook-grounded rather than LLM-improv.

User feedback (2026-04-30): *"whether student got the answer right or wrong
the LLM needs to give a chunk-based description or some description that
grounds to the textbook."*

**What to build.** A `textbook_reference` block on the final tutor message —
2–4 sentences synthesised from the top-K retrieved chunks, with a
chapter/section citation. Always surfaced, regardless of reach status:

- **Reach=True:** appears alongside the success narrative as a
  *"From the textbook"* footer that confirms what the student worked out.
- **Reach=False:** appears alongside the recap as a *"For your reference"*
  footer giving them the answer with grounding so they can study it.

**Implementation sketch.**

1. **Backend.** In `conversation/nodes.py:memory_update_node` (or wherever
   the closing message is composed), before sending the final tutor
   message, run a small LLM call:
   ```
   You are summarising the textbook content related to the question:
   {locked_question}

   The expected answer is: {locked_answer} ({full_answer if different})

   Below are top-K chunks retrieved at lock time:
   [chunks with chapter/section/page metadata]

   Write a 2-4 sentence textbook-grounded description of the answer.
   Cite chapter and section inline. Do NOT extrapolate beyond the chunks.
   ```
   Stash result in `state["textbook_reference"]` with structure:
   ```python
   {
       "summary": "The shaft of the penis contains three columns of erectile tissue ...",
       "citations": [
           {"chapter": 27, "section": "27.4", "subsection": "The Penis", "page": 1156},
       ],
   }
   ```

2. **WS payload.** `backend/api/chat.py` — pass `textbook_reference` through
   the final-message envelope.

3. **Frontend.** New `<TextbookReference>` component below the final
   tutor bubble, styled distinctly (e.g., subtle slate background with a
   book icon, "From OpenStax — Ch 27.4 The Penis" header).

4. **Eval.** `evaluation/quality/dimensions.py` — extend `RGC` (response-
   grounding-correctness) to verify the citations point to chunks that
   were actually retrieved at lock time. This converts the
   "demonstrably grounded" claim from anecdote to dimensional metric.

**Cost.** ~$0.001/session (one Haiku call, ~500 in / ~150 out). Negligible.

**Files.** `conversation/nodes.py` or `conversation/dean.py` (closure node),
`conversation/state.py` (add `textbook_reference` field), `backend/api/chat.py`
(WS envelope), `frontend/src/components/chat/TextbookReference.tsx` (new),
`frontend/src/components/chat/MessageBubble.tsx` (mount point).

**Acceptance.** Every session export's final tutor message has a
`textbook_reference` field. Manually verify on 5 sessions that the
citations resolve to real chapter/section pairs in `textbook_structure.json`.

---

### Future work — CRAG + Perplexity for out-of-corpus questions

User suggestion (2026-04-30): use [CRAG (Corrective RAG, arXiv
2401.15884)](https://arxiv.org/abs/2401.15884) with Perplexity's Sonar API
as the web-search fallback when corpus retrieval is weak.

**The case for it.** This is the cleanest fix for the **`triple2_s1`
coverage-gate runaway** pattern (P0-A above). When the student asks
something the OpenStax corpus genuinely doesn't cover (or covers poorly),
the current system loops through card rejections until the session dies.
CRAG-style retrieval evaluation classifies retrieval quality as `confident`
(use as-is), `ambiguous` (augment with web), or `incorrect` (fall back to
web) — and the Perplexity Sonar API is the right external search because
it returns inline citations, which preserves the "grounded to a source"
contract.

**My take.** Yes — it's a good direction, but ship it **behind a feature
flag, default-off for thesis eval runs**. Reasoning:

| Pros | Cons |
|---|---|
| Fixes the coverage-gate hard fail mode (`triple2_s1`-style loops) | Pollutes the "demonstrably OpenStax-grounded" thesis claim — need clear UI distinction (textbook citation vs web citation) |
| Lets the system handle edge questions (recent research, rare conditions) | Adds 2–5s latency per turn it fires |
| Perplexity returns citations, so groundedness is preserved | ~$0.005/req on Sonar Pro — adds up across long sessions |
| Bigger story for the thesis discussion ("we extended beyond textbook") | Web results introduce content the dean's leak-prevention rules weren't designed for — new prompt-injection surface |
| | Eval batches become non-reproducible if web fallback fires |

**Suggested wiring.**

1. **Retrieval quality classifier.** `retrieval/retriever.py` after the
   cross-encoder rerank, score the top chunk:
   - max CE score ≥ T_HIGH → `confident` (current path)
   - T_LOW ≤ max CE score < T_HIGH → `ambiguous` (augment with web)
   - max CE score < T_LOW → `incorrect` (fall back to web entirely)

2. **Web adapter.** New `retrieval/web_search.py` — wraps Perplexity Sonar
   API. Returns chunks with the same schema as the existing retriever
   (so callers don't branch), but with `source_type="web"` and a citations
   list.

3. **Feature flag.** `cfg.retrieval.web_fallback_enabled` (default
   `false`). When on, the coverage gate calls the web adapter instead of
   surfacing rejection cards. When off, current behaviour.

4. **UI distinction.** TextbookReference (P1-D above) renders blue/slate;
   WebReference renders amber, with clear "from web — perplexity" labelling.

5. **Eval.** Add a `from_web` boolean to retrieved chunks in scorer input.
   `RGC` rule extension: web-sourced chunks count toward grounding only if
   the citation URL resolves and the surface text appears in the page.

**Defer if blocking other tasks.** This is a thesis-extension feature, not
a fix for any current eval failure. P0-A (coverage-gate runaway) can be
addressed more cheaply by adding a graceful "we couldn't find this in the
textbook — want to try a related topic?" terminator after N consecutive
rejections, without bringing in web search at all. CRAG+Perplexity is the
*good* solution but the *cheap* solution may be enough for the deadline.

---

### P1-E. Memory-recall intent + cleaner memory writes

**Two coupled issues** surfaced from the local export
`sokratic_arun_2026-04-30T19-13-44-883Z.json`. They're related and should
ship together — neither fix is hard.

#### Issue 1 — mem0 fact-extraction shreds our session summaries

**What's happening.** In `memory/memory_manager.py` we write 5 structured
multi-line mem0 entries per session (session_summary, misconceptions,
open_thread, topics_covered, learning_style). Example from
`_build_open_thread`:

```python
return (
    f"[Open thread from session {ts}]\n"
    f"Student was working on: {topic}\n"
    f"Status: did not reach final answer in {turns} turns. "
    f"Resume this topic in next session."
)
```

mem0 doesn't store this verbatim — it runs an LLM fact-extraction pass that
splits each multi-line entry into **atomic claims** before storing. So the
above becomes separate stored memories like:

- *"Did not reach final answer in 9 turns"*
- *"Resume this topic in next session"*
- *"Topics covered on 2026-04-30"*

…all of which have lost the context they need to mean anything (*resume
what topic?*). The rapport prompt's `Past session context` block then ends
up looking like a bullet list of context-less fragments mixed with the
actually-meaningful items.

**Fix (~half a day).** Replace the multi-line structured entries with
single-paragraph self-contained natural-language sentences where the topic
name is baked into every claim. Example:

```python
return (
    f"On {ts} the student worked on '{topic}' (Chapter {chapter}, "
    f"{section}). They explored the anchor question '{locked_q}' "
    f"but did not reach the answer in {turns} turns. Mastery is "
    f"partial; resume this topic next session."
)
```

After mem0 fact-extracts, each atomic claim still carries the topic name
("did not reach the answer on Correlation Between Heart Rates and Cardiac
Output in 9 turns"). Memory pool stays meaningful.

**Files.** `memory/memory_manager.py` (5 `_build_*` methods) — straight
prompt-style rewrite, no schema changes.

#### Issue 2 — No `memory_query` intent in the dean's classifier

**The flow that's missing.** Students will naturally reference past
sessions: *"what did we cover last time?"*, *"continue from where we
stopped"*, *"remember when we talked about X?"*, *"what was that thing
about cardiac cycle phases?"*. None of these are anatomy questions and
none are off-topic — they're **meta-conversation requests for prior
session content**. Right now the dean classifier doesn't have a slot for
this; it routes them into one of `incorrect`, `question`, or `low_effort`,
all of which produce wrong behavior:

- `incorrect` → tutor scaffolds an anatomy hint that's irrelevant
- `question` → tutor restates the locked anchor question, ignoring the meta-request
- `low_effort` → strike counter increments; eventually triggers a hint advance the student didn't earn

**Fix (~1 day).** Three small changes that ship together:

1. **Add `memory_query` to the `student_state` enum** in
   `config/base.yaml` (`dean_setup_classify_static` prompt). Detection
   patterns to add:
   - "what did we cover last time/before/yesterday?"
   - "remember when we talked about X?"
   - "continue from where we stopped/left off"
   - "what was that thing about Y?"
   - "go back to the X session"
   Make explicit in the prompt that this is **distinct from** `question`
   (which is about the *current* topic) and **distinct from** `off_topic`
   (off-domain content) and the new `deflection` state from P0-C.

2. **Route `memory_query` to a new handler in `conversation/dean.py`** —
   call it `handle_memory_recall(state)`. The handler:
   - Pulls the last N session summaries from mem0 (filter to current
     student_id, sort by timestamp).
   - Reads `data/student_state/{student_id}.json` for per-concept mastery
     (already has the structured per-topic outcomes).
   - Composes a single Haiku call:
     > "Student asked: '{student_msg}'. Here are the last N session
     > summaries and mastery cues. Reply in 2–3 sentences with the
     > specific topic, what was reached vs. not, and offer to resume or
     > pick a new topic. Cite the date."
   - Returns the response as a tutor message, **does not** increment hint
     counters or strike counters, **does not** advance the topic-lock
     state machine. It's a side-channel turn.

3. **State-machine wiring.** `memory_query` happens *before* topic-lock
   (the student is asking about the past, not engaging with the current
   anchor) and *during* tutoring (the student might pause mid-session to
   ask). So the handler runs at any phase ≥ rapport. After the handler
   responds, the next student turn re-enters the normal classification
   path — the memory-recall turn is "outside" the tutoring loop.

**Why these two go together.** The cleaner memory writes (Issue 1) make
the memory_query handler's output good. Without Issue 1's fix, the
memory_query handler would surface the same noisy fragments back at the
student, which is worse than the current "wrong intent" behavior.

**Validation.** Add `T_memory_recall_*` test scenarios:
- Mid-session: pre-locked topic, student asks "what did we cover last
  time?" → expect a 2–3 sentence summary that names the prior topic +
  outcome, no hint counter increment, normal flow resumes on next turn.
- Pre-lock (rapport phase): student says "continue where we stopped" →
  expect a one-shot lock to the prior open-thread topic + a tutoring
  question (not a fresh card UI).

**Files.** `config/base.yaml` (`dean_setup_classify_static`),
`conversation/dean.py` (new `handle_memory_recall` + routing in the
classifier dispatch), `conversation/state.py` (any new fields the
handler needs, probably none), `evaluation/quality/penalties.py` (don't
penalize `memory_query` turns as fabrication).

**Acceptance.** Run a 3-session arc on a single student; in session 3,
ask "what did we cover the first two sessions?" — the response should
name both prior topics with their outcomes. The chat doesn't drift; the
hint counter doesn't move; the next student utterance re-enters the
normal dean flow cleanly.

#### Issue 3 (same flow) — Rapport opener should reference the last 2–3 sessions, not just 1

**Current behavior.** `config/base.yaml:teacher_rapport_static` has a hard
cap *"at most ONE prior item; no recap, no list"*. Made sense when memory
was noisy (Issue 1) but is too restrictive once the writes are clean — a
returning student benefits from seeing a brief arc of recent work, not
just a single name-drop.

**The constraint.** "Subtly" — not a bullet list, not a recap. One sentence
that weaves 2–3 topics into a continuity statement. The LLM picks the
phrasing; reach status is implicit ("you've been working through" vs "you
wrapped up"), not spelled out as success/failure.

**Pattern to aim for** (LLM should produce, not literal templates):

| Prior sessions | Rapport opener style |
|---|---|
| 0 | Fresh — no prior reference (current behavior preserved) |
| 1 | *"Last time you were working through cardiac output…"* (current) |
| 2 | *"We've spent the last two sessions on cardiovascular physiology — cardiac output, then SA node rhythm. Keep going or pivot?"* |
| 3 | *"Over the last few sessions you've moved through cardiovascular: cardiac output → SA node → pulse pressure. Round it out or jump to a new system?"* |

Cap at 3 sessions even if more are stored — beyond that the opener gets
unwieldy.

**Fix.**

1. **`memory/memory_manager.py`** — when reading at session start, fetch
   the last K=3 session-summary entries (sort by `ts` field embedded in
   the memory string, descending). Pass them to the rapport node.
2. **`conversation/nodes.py:rapport_node`** — instead of dumping all
   mem0 hits into the prompt's `Past session context` block, structure
   them as an explicit `recent_sessions: [{date, topic, outcome}]` list
   so the LLM has clean fields, not a bullet salad.
3. **`config/base.yaml:teacher_rapport_static`** — replace the *"at most
   ONE prior item"* guardrail with:
   > *"If `recent_sessions` is empty, treat as a fresh session. If it has
   > 1 entry, briefly reference it as the offered starting point. If it
   > has 2–3 entries, weave them into a single continuity sentence (e.g.,
   > 'We've spent the last few sessions on X, then Y, then Z'). Never
   > more than 3, never as a bullet list, never reveal past answers, never
   > spell out reach success/failure."*

**Validation.** Run a 4-session arc on a single student. Session 1 opener
is the fresh template. Session 2 names session 1's topic. Session 3 weaves
sessions 1+2. Session 4 weaves sessions 1+2+3 (caps at 3 even though 3
prior sessions exist). Eyeball that none of them feel like recaps.

**Why this ties to Issues 1 + 2.** Same memory-recall theme — clean
writes (Issue 1) feed both the rapport opener (Issue 3) and the
memory_query handler (Issue 2). All three change the same files
(`memory_manager.py`, `nodes.py`/`dean.py`, `base.yaml`) so they're one
coherent piece of work, ~2 days total.

---

### P2. Scorer false-positive: `FABRICATION_AT_REACHED_FALSE` in clinical phase

This is contributing 20 of v3's 47 critical penalties (and 32 of v2's 64).
It's a scorer bug, not a system bug — but it makes every "passed-tutoring,
ran-clinical-assessment" session show up as `failed_critical_penalty` in the
dashboard.

**The pattern.** A strong student reaches the answer in tutoring (turn 3),
then enters clinical assessment. In the clinical scenario, the tutor narrates
*"What you got right: You correctly identified isovolumic contraction"* —
referencing tutoring-phase content. The scorer checks the per-turn
`student_reached_answer` flag for *that specific turn* (which is False in
clinical, legitimately, because the student is now learning the *clinical
extension*) and fires `FABRICATION_AT_REACHED_FALSE`.

**The fix.** Either:
- Use the *session-level* `final_student_reached_answer` flag for this
  penalty rather than per-turn, OR
- Mask penalties from clinical-phase turns when the session reached True in
  tutoring.

**Files.** `evaluation/quality/penalties.py` — find the
`FABRICATION_AT_REACHED_FALSE` rule, gate it on `phase != "assessment"` or
on session-level reach.

**Validation.** All sessions where `solo1`, `solo4`, `solo6`, `pair1_*`,
`pair2_s1`, `triple2_s2` clean tutoring → clinical run → `passed` instead of
`failed_critical_penalty`. Expected outcome: critical-penalty rate drops from
17/18 to roughly 10/18.

---

### Bootstrap path Nidhi will use (verify this works)

Setup is documented in detail in `README.md` §First-time setup. The end-to-end
path is:

```bash
git clone <repo>
cd sokratic
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r backend/requirements.txt

cp .env.example .env                                # fill in ANTHROPIC_API_KEY (+ OPENAI_API_KEY for rebuild path)

.venv/bin/python scripts/bootstrap_corpus.py        # pulls JSONL + BM25 + topic_index from HF
scripts/qdrant_up.sh                                # docker run qdrant
# either restore snapshot (preferred) OR reindex_chunks.py --collection sokratic_kb --fresh

uvicorn backend.main:app --reload --port 8000      # backend
cd frontend && npm install && npm run dev          # frontend (separate terminal)
```

**Punch-list for Arun before this handoff is fully clean:**

- [x] **Re-publish the HF dataset for the chunks-only architecture.**
  *(Completed 2026-04-30: tag `v1-chunks-only`, snapshot uploaded, `*_ot.*`
  files pruned — see [`arun-ghontale/sokratic-anatomy-corpus`](https://huggingface.co/datasets/arun-ghontale/sokratic-anatomy-corpus).
  Steps below preserved for the next re-publish.)*

  Original plan: the previous published tag (`v0-messy-metadata`) carried
  the deprecated propositions-era files (`chunks_ot.jsonl`,
  `propositions_ot.jsonl`, `bm25_ot.pkl`, `raw_*_ot.jsonl`). On 2026-04-30
  `scripts/publish_corpus.py` was rewritten to publish only the live
  chunks-only artifacts:

  - `data/processed/chunks_openstax_anatomy.jsonl` (~11 MB)
  - `data/indexes/bm25_chunks_openstax_anatomy.pkl` (~17 MB)
  - `data/textbook_structure.json`
  - `data/topic_index.json`
  - **NEW** `data/indexes/qdrant_sokratic_kb_chunks.snapshot` (~600 MB,
    optional include — generate first; without it Nidhi must rebuild the
    dense index, ~$0.10 / ~10 min)

  Run on a host with the populated `sokratic_kb_chunks` collection:

  ```bash
  # 1. snapshot the chunks Qdrant collection
  NAME=$(curl -s -X POST http://localhost:6333/collections/sokratic_kb_chunks/snapshots | jq -r '.result.name')
  curl -o data/indexes/qdrant_sokratic_kb_chunks.snapshot \
    "http://localhost:6333/collections/sokratic_kb_chunks/snapshots/$NAME"

  # 2. publish (the script's INCLUDE_FILES is already set to the chunks-only list).
  #    --prune deletes the deprecated *_ot.* files from the remote repo so
  #    the published dataset is clean. Without --prune they linger as dead
  #    weight; bootstrap_corpus.py won't fetch them either way (it follows
  #    MANIFEST.json) but pruning saves ~100 MB of HF storage.
  .venv/bin/python scripts/publish_corpus.py --dry-run                            # preview
  .venv/bin/python scripts/publish_corpus.py --prune --tag v1-chunks-only         # ship it
  ```

  Once that lands on HF, `bootstrap_corpus.py` picks the snapshot up
  automatically (it iterates the published manifest) and step 4 of the
  README simplifies to "Path A only".

- [x] **Flip the runtime collection name to chunks.**
  *(Completed 2026-04-30: `kb_collection: "sokratic_kb_chunks"` in both
  `config/domains/ot.yaml` and `config/base.yaml`. Smoke-tested via
  `Retriever()` — connects, returns 16 chunks for "What does the axillary
  nerve supply?".)*

- [ ] **Verify the clean-clone walkthrough.** Spin up a scratch directory,
  run the README §First-time setup steps verbatim, confirm the demo
  conversation works end-to-end. (Owner: Arun, before sending this doc.)
  README's "Smoke test" subsection at end of §6 is the final gate.

- [ ] **Confirm Nidhi has access** to: GitHub repo, HF dataset (currently
  public so token not required), Anthropic console (for API key).

---

### Reference v3 numbers (your baseline to beat)

| Metric | v1 | v2 | v3 | target after fixes |
|---|---:|---:|---:|---:|
| Penalties total | 155 | 69 | 49 | < 30 |
| LEAK_DETECTED | 10 | 15 | 10 | < 5 |
| FABRICATION_AT_REACHED_FALSE | 30 | 32 | 20 | < 5 (scorer fix) |
| EULER no_reveal | 0.894 | 0.876 | 0.914 | ≥ 0.95 |
| EULER relevance | 0.884 | 0.811 | 0.733 | ≥ 0.85 (matcher fix) |
| RGC | 0.759 | 0.852 | 0.889 | ≥ 0.90 |
| Reach rate | 13/18 | 9/18 | 9/18 | 9–11/18 (no inflation) |

The reach rate going up past ~11/18 is a **regression signal**, not progress
— v1's 13/18 was the leak-then-parrot pattern that v2 eliminated. Genuine
reach should sit in the 9–11 range with the current S1–S6 mix.

---

---

## Current state

All recent changes are documented in `docs/SESSION_JOURNAL_2026-04-30.md`.
Quick recap of what's shipped + validated:

- **Change 1** — `reached_answer_gate` (token-overlap + LLM paraphrase with verbatim-quote enforcement)
- **Change 2** — topic-lock acknowledgement turn (deterministic ack message)
- **Change 3** — dean classification tightening + mastery scorer attribution rules
- **Change 4** — counter system (help_abuse + off_topic), strike warnings, hint-advance at threshold, off-topic terminate
- **Change 5.1** — clinical-phase counters
- **Eval framework** — primary (EULER + RAGAS) + 10 secondary dimensions + penalties; CLI scorer at `scripts/score_conversation_quality.py`

You'll find:
- 7 test scenarios already in `scripts/test_reached_gate_e2e.py` (T1, T2, T3, T4, T6, T7, T11, T12)
- T5, T8, T9, T10 may also be present (added in Phase 1 — check the file)
- Saved test outputs in `data/artifacts/gate_e2e/`
- Scorer outputs in `data/artifacts/eval/`

---

## Your tasks (in priority order)

### Task 1 — Manual evaluation: 10 conversations (4 hours)

**Goal**: surface real-world failures the test harness misses.

1. Start the dev stack:
   ```bash
   cd backend && uvicorn main:app --reload --port 8000
   cd frontend && pnpm dev
   ```
2. Open http://localhost:5173, run 10 conversations as a real student would. Cover:
   - 3× happy path (genuine engagement, 1 reach)
   - 2× partial-correct on intermediate scaffold (the "mine goes lower" pattern — see Change 3 in journal)
   - 2× low-effort spam ("no", "idk", "just tell me" 4× — should hit help-abuse cap with hint advance)
   - 2× off-domain spam (4 vaping/sex/profanity messages — should terminate with farewell)
   - 1× domain-tangential drift ("locked on SA node, what about veins?" — should fire exploration_retrieval, no counter increment)
3. **Export each conversation** via the WS export endpoint (`/api/session/{thread_id}/export`) — saves the full state JSON.
4. **Score each** with the eval framework:
   ```bash
   .venv/bin/python scripts/score_conversation_quality.py path/to/exported_session.json
   ```
5. **Document failures** in `data/artifacts/eval/manual_review_2026-04-30.md`:
   - For each session: verdict, dim scores, penalties, what surprised you
   - Critical penalties = MUST FIX before deploy
   - Major penalties = file as known issues

**Success criterion**: ≤ 1 critical penalty across 10 sessions.

### Task 2 — Simulation eval: 50-60 conversations (2-3 hours runtime)

**Goal**: stress-test the system at scale + produce Sonnet/Haiku A/B data.

1. **Run the existing harness** with all profiles × multiple topics:
   ```bash
   .venv/bin/python scripts/run_final_convos.py
   ```
   This runs 6 profiles (S1–S6) over the curated topic set. Add more topics in `PROFILE_TOPICS` if needed to hit 50-60 conversations.
2. **CRITICAL: do not ask questions artificially.** The `StudentSimulator` should drive responses naturally based on its profile. Don't tweak prompts to manufacture nice transcripts.
3. **Score every saved JSON** through the eval framework:
   ```bash
   for f in data/artifacts/final_convo/*.json; do
       .venv/bin/python scripts/score_conversation_quality.py "$f" --quiet
   done
   ```
4. **Aggregate**: write `scripts/eval_aggregate.py` to roll up per-dimension means + penalty counts across all 50-60 sessions. Output as a single dashboard JSON + a one-page markdown report.

**Success criteria**:
- Per-dimension mean scores meet thresholds in `docs/EVALUATION_FRAMEWORK.md` §8
- < 5% of sessions trigger any critical penalty
- EULER mean ≥ 0.85 across all 4 criteria

### Task 3 — Sonnet vs Haiku A/B test ($5–$10)

**Recommendation**: only swap **dean** + **mastery_scorer** to Sonnet. Keep teacher and evaluator on Haiku.

```yaml
# config/base.yaml — A/B variant A (test)
models:
  teacher: "claude-haiku-4-5-20251001"
  dean: "claude-sonnet-4-6"            # ← swap
  summarizer: "claude-haiku-4-5-20251001"
  evaluator: "claude-haiku-4-5-20251001"
# memory/mastery_store.py uses cfg.models.dean — that flips Sonnet automatically
```

Run **10 conversations per config** (variant A above + variant B = current Haiku-only). Use the same topics/profiles. Score both, compare:

- Per-dimension mean scores
- Critical penalty rate
- Cost per session

**Decision criterion**: if Sonnet improves ARC by ≥ 0.10 AND TRQ by ≥ 0.05 with cost increase ≤ 4×, switch dean to Sonnet permanently. Otherwise revert.

### Task 4 — Vision model flow (1-2 days)

**Goal**: handle student-uploaded anatomy diagrams with Sonnet 4.6 (Haiku doesn't support vision well).

Existing scaffolding:
- `state.is_multimodal: bool`
- `state.image_structures: list[str]` (vision model output)
- `cfg.models.vision = "claude-sonnet-4-6"`

What to add:
1. Frontend: image upload UI in chat composer (multipart/form-data via WS or HTTP).
2. Backend: image storage (data/uploads/) + vision call.
3. Vision flow node: take image + locked_question, output a list of identified structures + brief description. Append as a special `[image-context]` block to the next dean turn's input.
4. Tests: a `T_vision_*` scenario uploading a known diagram and verifying tutor reasoning incorporates the structures.

Defer if blocking other tasks — this is the ONE feature where deadline slip is acceptable.

### Task 5 — Secondary parallel source integration (1-2 days)

**Hard constraint** (confirmed 2026-04-30): the OpenStax topic_index TOC is
the **canonical structure** for the framework. Any secondary source must map
*onto* the existing chapter → section → subsection tree. Sources that
introduce new topics or restructure the hierarchy break the lock-time
matcher, the dean's coverage gate, and the eval framework's TLQ dimension.
Drop chunks that don't map; never add new index entries.

**Suggested candidate sources** (decided 2026-04-30): **StatPearls** and
**Gray's Anatomy**. Both are open-access and complement OpenStax in
genuinely different ways — they're not interchangeable substitutes, they
attack different gaps in the framework's coverage. Pick one to start; the
two-collection retrieval design (below) supports adding the second later.

#### Candidate A — StatPearls (NCBI Bookshelf)

- **License**: open access, NIH-funded
- **Volume**: ~10,000 articles; ~300–500 retain after OpenStax-TOC mapping
- **Programmatic access**: NCBI E-utilities (`efetch`)
- **Structural fit with OpenStax**: high. Each article has a stable
  sub-heading template — *Introduction / Structure and Function /
  Embryology / Blood Supply and Lymphatics / Nerves / Muscles / Physiologic
  Variants / Surgical Considerations / Clinical Significance* — that maps
  cleanly onto an OpenStax subsection. Mapping rule: merge the first 3
  sub-headings under the matching OpenStax subsection; route "Clinical
  Significance" to a new `clinical_supplement` field on that subsection,
  used only at clinical-assessment phase.
- **What it adds that OpenStax lacks**: clinical correlation depth,
  surgical considerations, disease associations, continuously-updated
  peer-reviewed content, 3–4× more prose per anatomical structure.
- **Voice**: clinical reference. Good for the **clinical-assessment
  phase** of the dean's flow.

#### Candidate B — Gray's Anatomy

Two editions worth considering:

| Edition | License | Volume | Verdict |
|---|---|---|---|
| Gray's Anatomy of the Human Body (1918, Bartleby/Project Gutenberg) | Public domain | Full text, ~600k words | Free but archaic terminology requires normalization (e.g., *"internal mammary artery"* → *"internal thoracic artery"*) |
| Gray's Anatomy for Students (Drake, Vogl, Mitchell — 4th ed. or later) | Licensed | Definitive modern student text | Gold-standard regional anatomy reference, but **requires institutional license**. Use only if licensing clears |

- **Volume**: ~500–700k words of anatomical detail, deeper than OpenStax
  per-region for skeletal / muscular / neurovascular topics.
- **Structural fit with OpenStax**: medium. **Gray's organises by *region***
  (head & neck / thorax / abdomen / pelvis / limbs), while OpenStax
  organises by *system* (cardiovascular / nervous / muscular). This means
  one Gray's chapter on "Upper Limb" gets fragmented across multiple
  OpenStax subsections (axillary artery → cardiovascular; brachial plexus
  → peripheral nervous system; biceps brachii → muscular; humerus →
  skeletal). This is not a defect — it's actually a feature for our
  framework: regional anatomy is how clinicians think about questions
  ("what's in the cubital fossa?"), and OpenStax doesn't natively
  organise that view.
- **What it adds that OpenStax lacks**: regional anatomy ("everything in
  the popliteal fossa"), gross-anatomical detail (named ligaments, fascial
  layers, exact landmark positions), surgical/clinical regional
  correlation. This is the half of medical anatomy education that
  system-based texts under-serve.
- **Voice**: classical anatomical reference. Good for the **lock-time
  retrieval phase** when the student asks structurally-specific
  questions.

#### Why both, eventually

| Phase / question type | Best source |
|---|---|
| Pedagogical scaffolding (tutoring) | **OpenStax** (canonical) |
| Clinical correlation (assessment) | **StatPearls** |
| Regional / gross-anatomical detail | **Gray's Anatomy** |
| Mechanism-first physiology | **OpenStax** (canonical) |
| Surgical considerations | **StatPearls** |
| "What's in this anatomical region?" | **Gray's Anatomy** |

#### Sources considered but not recommended as primary

- **Wikipedia anatomy articles** — open, structurally compatible, but
  edit-drift / vandalism risk on niche topics; voice is encyclopaedic.
  Reasonable as a *third* source for pure breadth, not a primary.
- **OpenStax Microbiology / Concepts of Biology** — same publisher and
  TOC philosophy, but adjacent domains; useful only if expanding scope
  beyond anatomy.
- **NIH MedlinePlus / NIDDK** — patient-education depth, weak on
  mechanism. Not a textbook substitute.
- **Khan Academy transcripts** — surface-level, conversational; better
  treated as a *style* reference for tutor scaffolds than a content
  source.
- **Tortora / Marieb** — gold-standard parallels, but require licensing.
  If institutional license is available, prefer Tortora over either of
  the open-access options as the primary parallel textbook (OpenStax
  modelled its TOC on Tortora's).

**Recommended pipeline.**

1. **Ingest:** new source `ingestion/sources/statpearls/` mirroring the
   `openstax_anatomy/` layout. Each article → multiple chunks, one per
   sub-article section.
2. **Map at ingestion time:** for each StatPearls chunk, run a
   matcher pass against `data/topic_index.json` (re-use `topic_matcher.py`
   logic with `STRONG_MIN=85` since both surface forms are clinical/Latin
   — even higher confidence threshold than student free-text). Drop
   chunks that don't strong-match. Tag retained chunks with `source:
   "statpearls"` and the resolved OpenStax `path`.
3. **Two-collection retrieval:** new Qdrant collection
   `sokratic_kb_chunks_statpearls`. Modify `Retriever.retrieve` to query
   BOTH collections, RRF-merge, then cross-encoder rerank as one set.
   Each retrieved chunk's `source_type` field (already in schema) becomes
   `"openstax"` or `"statpearls"`.
4. **Phase-aware weighting (optional but valuable):** during tutoring,
   prefer OpenStax chunks (pedagogical voice); during clinical assessment,
   prefer StatPearls chunks (clinical depth). Implement as a small RRF
   weight bias by phase.
5. **Evaluation:** rerun the 18-convo batch with `secondary_source=on`.
   Expect: clinical assessment EULER `helpful` improves; tutoring metrics
   stable (no drift). RGC dim should *strengthen* because StatPearls
   citations add a second grounding source.
6. **UI:** TextbookReference (P1-D above) renders OpenStax-sourced
   citations one way; StatPearls-sourced citations a slightly different
   way ("From StatPearls" with the article slug). Both still grounded;
   user knows where it came from.

**Why not just merge into one collection?** Two-collection lets you (a)
A/B between OpenStax-only and dual-source for the thesis chapter, (b)
re-ingest StatPearls quarterly without touching the OpenStax index, and
(c) compute per-source contribution to retrieval quality (a publishable
breakdown).

**Validation gate.** Before declaring this task done, prove:
- All retained StatPearls chunks have a valid `path` matching an existing
  OpenStax subsection (no orphans).
- T1–T12 gate tests pass with `secondary_source=on`.
- 18-convo eval shows non-regression on TLQ + RGC + reach rate.
- A small qualitative read confirms StatPearls chunks aren't "stealing"
  topic locks from OpenStax chunks for the same subsection (the merge
  shouldn't change *which subsection* gets locked, only what gets
  retrieved within it).

### Task 6 — Update docs

I'll have already written:
- `docs/SESSION_JOURNAL_2026-04-30.md` (decisions)
- `docs/HANDOFF_NIDHI.md` (this doc)
- `docs/EVALUATION_FRAMEWORK.md` (framework spec)

The user will update:
- `README.md` (current state of all changes)
- `docs/architecture.md` (full pipeline post-Change-6)

You should:
- Update `docs/EVALUATION_FRAMEWORK.md` §5 to reflect actual T5/T8/T9/T10 results once you run them
- Document any new failure modes you find in `data/artifacts/eval/manual_review_2026-04-30.md`
- Document Sonnet A/B results in `docs/sonnet_vs_haiku_2026-04-30.md`

---

## Reference: how to use the eval scorer

```bash
# Score one session with full LLM eval (~$0.005)
.venv/bin/python scripts/score_conversation_quality.py path/to/session.json

# Deterministic only (free, no API calls — useful for quick iteration)
.venv/bin/python scripts/score_conversation_quality.py path/to/session.json --no-llm

# Skip the cheapest LLM call (anchor quality)
.venv/bin/python scripts/score_conversation_quality.py path/to/session.json --skip-anchor

# Output goes to data/artifacts/eval/{stem}_eval.json
# Console summary line shows verdict + EULER + RAGAS + failed dimensions
```

Output schema is in `docs/EVALUATION_FRAMEWORK.md` §6.

---

## Reference: thresholds for "passing" a session

From `docs/EVALUATION_FRAMEWORK.md` §8:

```
EULER:        question_present ≥ 0.95, relevance ≥ 0.70,
              helpful ≥ 0.70, no_reveal ≥ 0.95
RAGAS:        context_precision ≥ 0.80, context_recall ≥ 0.70,
              faithfulness ≥ 0.85, answer_relevancy ≥ 0.75
Secondary:    TLQ ≥ 0.80, RRQ ≥ 0.75, AQ ≥ 0.85, TRQ ≥ 0.75,
              RGC ≥ 0.95, PP ≥ 0.60, ARC ≥ 0.70, CC = 1.00,
              CE ≥ 0.50, MSC ≥ 0.70
Penalties:    0 critical (any critical = session fails)
```

A session that fails ANY critical penalty is reported as `failed_critical_penalty`
regardless of dimension scores. See `evaluation/quality/penalties.py` for the full list.

---

## Things to watch (from prior validation)

1. **Teacher reveal-on-hint-advance**: T6 (4-strike low-effort) ran successfully but the teacher named the answer in its post-cap message ("SA node (sinoatrial node)"). The narration brief said "deliver the new hint" and the teacher took that too far. Prompt tightening needed in `cfg.prompts.teacher_socratic_*`.
2. **EULER nondeterminism**: gravity export EULER scores moved 1.00 → 0.75 between consecutive runs on identical input. Acceptable noise but flag if you see big swings.
3. **`context_precision` baseline 0.40–0.60**: chunks include some off-topic content. Reranking should help; if your batch shows the same, investigate cross-encoder cutoffs.

---

## Daily checkpoint format

Post a brief update each evening to a shared doc with:
- Tasks completed today
- Critical issues found
- Cost spent today
- Plan for tomorrow

Stop blocking dependencies the morning of (so I can unblock by lunch).

---

## Done criteria (Sunday EOD)

- [ ] Manual eval (10 sessions) complete + report written
- [ ] Simulation eval (50-60 sessions) complete + dashboard generated
- [ ] Sonnet A/B run + decision recorded
- [ ] Vision model flow shipped + tested
- [ ] Secondary textbook integrated + T1-T12 re-run + scores stable
- [ ] All docs updated
- [ ] Critical penalty rate < 5% across the simulation batch
- [ ] Per-dimension mean scores meet thresholds in §8 of eval framework

If anything blocks you, ping me before EOD. Don't sit on issues overnight.

---

## Update 2026-04-30 (post-qualitative review of 18-convo batch)

After running the 18-convo eval batch + reading every dialog turn-by-turn,
five known issues + their status:

### Already shipped (Round 4 fixes)
- **Eval framework**: INVARIANT_VIOLATION dedup (one penalty per session, not per turn)
- **Teacher prompt**: explicit forbidden patterns added — no letter hints, no mid-component reveals, refuse "just tell me" caves
- **Lock prompt**: stricter 1–5 word constraint on locked_answer, examples for multi-component cases (parts of nephron → "renal corpuscle" anchor + full list in full_answer)
- **Matcher threshold**: bumped STRONG_MIN 65 → 78 and STRONG_GAP 5 → 10. Ambiguous matches now fall to "borderline" tier (cards UI) instead of auto-locking.

### Known issues to watch in your eval batch

1. **Topic-resolution drift** (~5/18 sessions in v2): student asked about long bone structure, system locked on muscle types; cardiac cycle → muscle twitch. The threshold bump (STRONG_MIN=78) should reduce this — please verify in your batch whether drift recurs.

2. **Sentence-form locked_answer** (5/18 sessions in v2): the lock LLM was producing 9–11 word sentence-form anchors. Two-tier prompt updated; if you still see ≥6 word locked_answers, flag the pattern.

3. **Subtle leak patterns** (3 sessions in v2): tutor caved with "the medical term starts with 'P' and ends with 'pressure'" or "That phase is called the contraction phase" when student stonewalled. Teacher prompt now forbids these explicitly. If you still see them in your batch, the prompt needs further tuning.

4. **Sycophancy / over-attribution**: tutor saying "you nailed it" when student hedged ("I think... maybe?"). The deterministic `_has_strong_affirmation` check was removed in the audit cleanup. If your batch shows this pattern repeatedly, consider re-introducing the deterministic check.

5. **Coverage-gate over-strictness on broad topics**: triple2_s1 ("walk me through chemical digestion of carbohydrates") was rejected with random alternatives (Hip Bone, Muscles of Abdomen). Student couldn't resume the original topic. May need threshold adjustment.

### Reference outputs to compare against

- v1 (pre-fix) batch: `data/artifacts/eval_run_18_v1_pre_4fixes/` — 18 sessions, $9.24
- v2 (post-fix) batch: `data/artifacts/eval_run_18/` — 18 sessions, $8.50
- Qualitative report: `data/artifacts/eval_run_18/REPORT_qualitative_v2.md` — per-session writeups
- Comparison: `data/artifacts/eval_run_18/REPORT_v1_vs_v2.md`

Use these as your baseline. If your 50–60 simulation batch shows the same patterns, the issues are systemic. If your batch is cleaner, that's a promising signal.

### Quick re-run command (for after Sonnet A/B etc.)

```bash
cd /Users/arun-ghontale/UB/NLP/sokratic
.venv/bin/python scripts/run_eval_18_convos.py
# Then score:
for f in data/artifacts/eval_run_18/eval18_*.json; do
  .venv/bin/python scripts/score_conversation_quality.py "$f" \
    -o "data/artifacts/eval_run_18/scored/$(basename "$f" .json)_eval.json" --quiet
done
```
