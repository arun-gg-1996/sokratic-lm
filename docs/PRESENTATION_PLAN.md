# Sokratic 8-slide HTML deck — presentation plan

**Audience:** non-domain presenters (do not know anatomy). Story-driven walkthrough.
**Format:** 8 self-contained interactive HTML slides.
**Demo concept:** Body Planes — *"What's the key difference between a midsagittal plane and a parasagittal plane?"*
**Story spine:** A single fictional student "Nidhi" carries through every slide.

---

## Why 8, not 6

The original ask listed 7 distinct topics (rapport, tutoring, clinical, memory_update, memory_injection, analysis, architecture). Adding a cover/story-setup slide = 8. Compressing to 6 would either drop memory injection (a key technical differentiator) or merge clinical + memory_update (two very different flows). 8 is the minimum that keeps each slide self-contained.

## Story spine

```
SLIDE 1: meet "Nidhi" (returning student, 4:30pm)
SLIDE 2: she opens the app — system overview
SLIDE 3: rapport — personalized greeting fires
SLIDE 4: tutoring — she picks a card, struggles, hints escalate
SLIDE 5: clinical — she reaches the answer, opts into bonus
SLIDE 6: close — mastery score, mem0/SQLite save
SLIDE 7: RAG + memory injection — what was happening behind the scenes
SLIDE 8: analysis page next day + architecture journey + what's next
```

## Demo concept rationale

**Body Planes** chosen because:
- Already in the existing transcript (real artifact teammate can cite)
- Universally intuitive — anyone can grasp "cuts that divide the body left/right"
- The 3 anchor variations + the locked answer ("Equal vs unequal division of right and left sides") are documented in `~/Downloads/sokratic_nidhi_2026-05-05T17-47-13-126Z-repeated.json`

The teammate can quote real snippets from that JSON to ground the story.

---

# Slide-by-slide brief

For each slide: **purpose · layout · story beat · technical content · interactive element · code refs**.

---

## SLIDE 1 — Cover & story setup

**Purpose:** establish what Sokratic is in 5 seconds; hook the audience with the demo question.

**Layout:** full-bleed dark screen, centered title, three small badge cards underneath.

**Story beat:** *"Meet Nidhi. She's a med student studying anatomy. It's 4:30pm and she just opened her tutor."*

**Content:**
- Title: **Sokratic** — a Socratic-method tutoring system
- Three badges:
  - 🛡️ Never reveals the answer
  - 🧠 Remembers across sessions
  - 📊 Scores mastery, not attendance
- Demo question shown small at the bottom: *"What's the key difference between a midsagittal and parasagittal plane?"*

**Interactive:** hover each badge → 1-line tooltip explanation (no extra slide jump).

**Code refs:** none — pure framing slide.

---

## SLIDE 2 — System overview (4-phase graph)

**Purpose:** show the full architecture in one diagram. Everyone gets oriented.

**Layout:** central animated graph (4 nodes connected) + small "story progress" bar showing where Nidhi is right now (just entered).

**Story beat:** *"Nidhi's session will move through 4 phases."*

**Content:**

```
    [START]
       │
       ▼
   ┌────────┐    ┌──────────┐    ┌────────────┐    ┌─────────────────┐
   │RAPPORT │ ─→ │TUTORING  │ ─→ │ASSESSMENT  │ ─→ │ MEMORY_UPDATE   │
   │greeting│    │loop      │    │opt-in +    │    │ close + save    │
   │        │    │(N turns) │    │clinical    │    │                 │
   └────────┘    └──────────┘    └────────────┘    └─────────────────┘
                       │  ▲                              ▲
                       └──┘                              │
                  (max 25 turns,                         │
                   reach gate, etc)        ──────────────┘
                                           (early exits: end session,
                                            off-topic strike, etc)
```

Side card on the right: 4 background services (always running):
- **Bedrock** — LLM inference (Sonnet 4.6 + Haiku 4.5)
- **mem0** — long-term cross-session memory
- **SQLite** — per-session records + mastery
- **Qdrant + BM25** — textbook retrieval

**Interactive:** click any phase node → side panel slides in showing the 1-paragraph "what happens here" + LLM call count for a typical turn.

**Code refs:**
- `conversation/graph.py` — `build_graph()` wires all 4 nodes
- `conversation/lifecycle_v2.py` — `after_rapport`, `after_dean`, `after_assessment` (the routing edges)
- `conversation/registry.py` — `PhaseVocabulary` (the 4 phase names + transitions)

---

## SLIDE 3 — Rapport phase

**Purpose:** show how the system warms up + personalizes before any tutoring happens.

**Layout:** vertical timeline with 4 steps on the left; right-side "what gets initialized" panel.

**Story beat:** *"Sokratic checks Nidhi's history before saying hello."*

**Content (the timeline):**

| Step | What | Where | Time |
|---|---|---|---|
| 1 | Load prior session memory | SQLite (`recent`, `weak_topics`, `open_threads`) + mem0 | <1s |
| 2 | Build personalized greeting context | Flatten 8 most relevant memories into bullets | inline |
| 3 | Teacher generates greeting | Sonnet, mode=rapport, time_of_day-aware | ~3s |
| 4 | (Pre-locked path) Show 3 anchor question cards | See SLIDE 4's "topic lock" sub-flow | ~5-8s |

**State initialized at turn 0:**
- `turn_count: 0/25`
- `hint_level: 0/3`
- `consecutive_low_effort: 0/4`
- `help_abuse_count: 0/4`
- `off_topic_count: 0/4`
- `phase: rapport → tutoring` after greeting + first card click

**Routing logic:** rapport always exits to either `tutoring` (engaged) or `memory_update` (rapport-stage decline like "no thanks").

**Interactive:** clickable "Step 1" expands to show the actual SQL queries; "Step 3" expands to show the prompt template.

**Code refs:**
- `conversation/lifecycle_v2.py:48-283` — `rapport_node` (the whole flow)
- `conversation/teacher_v2.py` — `_MODE_INSTRUCTIONS["rapport"]` (the prompt template)
- `memory/sqlite_store.py` — `list_sessions()`, `weak_topics()` (data sources)

---

## SLIDE 4 — Tutoring phase (the engine)

**Purpose:** show what happens on every tutoring turn — the most complex flow, but also the most important.

**Layout:** big horizontal pipeline diagram (4 stages), with counter pills at the top.

**Story beat:** *"Nidhi clicks a card. Now the real loop begins."*

**Content (the per-turn pipeline):**

```
                      ┌───────────── COUNTERS (always visible) ─────────────┐
                      │ Turn 1/25  Hint 0/3  Low-effort 0/4  Help-abuse 0/4 │
                      └─────────────────────────────────────────────────────┘

[Student msg] → [PREFLIGHT] ──fired──→ [Teacher: redirect/nudge/close]
                  Haiku                                          
                  8 verdicts                                      
                    │
                    └─not-fired──→ [DEAN.plan] → [TurnPlan] → [RETRY ORCHESTRATOR]
                                    Sonnet      dataclass         │
                                                                   ▼
                                              ┌────────────────────────────────┐
                                              │ Attempt 1 → verifier quartet  │
                                              │ Attempt 2 → verifier quartet  │ (4 Haiku checks
                                              │ Attempt 3 → verifier quartet  │  in parallel)
                                              │   ↓ (all 3 fail)              │
                                              │ Dean.replan                    │
                                              │ Attempt 4 → verifier quartet  │
                                              │   ↓ (still fails)             │
                                              │ SAFE_GENERIC_PROBE backup     │
                                              └────────────────────────────────┘
```

**The 8 preflight verdicts:**

| Verdict | Example | Result |
|---|---|---|
| `on_topic_engaged` | "is it the heart?" | continue to Dean.plan |
| `low_effort` | "idk" | counter +1; at 4 → hint advances |
| `help_abuse` | "just tell me" | counter +1; at 4 → hint advances |
| `off_domain` | "what's the weather?" | counter +1; at 4 → session ends |
| `deflection` | "end this" | exit modal pops |
| `opt_in_yes/no/ambiguous` | "yes" / "no" / "ok" | clinical opt-in only |

**TurnPlan dataclass** (shown as code block):

```python
@dataclass
class TurnPlan:
    scenario: str            # e.g. "tutoring_default", "redirect_help_abuse"
    hint_text: str           # Dean's intended scaffolding
    mode: str                # socratic / redirect / nudge / honest_close / soft_reset / clinical
    tone: str                # encouraging / neutral / firm / honest
    forbidden_terms: list    # locked_answer + aliases
    permitted_terms: list    # vocabulary anchors
    shape_spec: dict         # max_sentences, exactly_one_question
    carryover_notes: str     # mem0 injection point
    clinical_scenario: str   # only when mode=clinical
    clinical_target: str     # only when mode=clinical
```

**The verifier quartet** (4 Haiku checks running in parallel):
- 🛡️ `hint_leak` — does the draft reveal the answer or aliases?
- 🛡️ `sycophancy` — does it confirm a wrong answer or over-praise?
- 🛡️ `shape` — does it have exactly 1 question, ≤max_sentences, no repetition?
- 🛡️ `pedagogy` — does it stay Socratic (asks rather than tells)?

Each retries on a "clean" verdict (asymmetric — trust "bad" instantly, double-check "good") to reduce stochastic miss rate.

**Counter triggers (callout box):**
- Each strike type **resets** on engagement
- At threshold (4): help_abuse / low_effort → hint level bumps + counter resets
- At threshold (4): off_topic → session ends (no recovery — student has drifted away)
- Hint at cap (3) + further strikes → no more advance, session winds down

**Interactive:** click any pipeline stage → side panel with the actual prompt template. Click any counter pill → hover tooltip with example messages.

**Code refs:**
- `conversation/nodes_v2.py:77` — `dean_node_v2` (the central orchestrator)
- `conversation/preflight.py:287` — `run_preflight` + the 8-verdict logic
- `conversation/preflight_classifier.py:177` — `_UNIFIED_INTENT_SYSTEM` (the prompt)
- `conversation/dean_v2.py` — `DeanV2.plan` (Sonnet planner)
- `conversation/retry_orchestrator.py:198` — `run_turn` (the retry loop)
- `conversation/verifier_quartet.py` — the 4 verifier functions
- `conversation/turn_plan.py` — the dataclass

---

## SLIDE 5 — Clinical phase

**Purpose:** show that "reach the answer" unlocks an optional applied scenario; same engine, different mode.

**Layout:** flow diagram on the left (opt-in → scenario gen → 7-turn loop → close); right side "what's reused" callout.

**Story beat:** *"Nidhi answers correctly. Sokratic offers her a clinical bonus."*

**Content:**

```
[Reach gate fires]
    ↓
[Opt-in offer]: "Want to apply this to a clinical case?"
    ↓
   ┌── yes ──┐         ┌── no ──┐
   ▼         ▼         ▼        ▼
[Dean: scenario gen]  [reach_close → memory_update]
    ↓ (~5-10s, heaviest LLM call in this phase)
[Clinical scenario rendered]
    ↓
┌──────────────────────────────────────────────┐
│ CLINICAL LOOP (max 7 turns, then natural close)│
│   per turn:                                   │
│   - Dean.plan (clinical continuation)          │
│   - SAME retry_orchestrator (3 attempts +     │
│     replan + safe probe)                      │
│   - SAME verifier quartet                     │
│   - separate counters: clinical_low_effort,    │
│     clinical_off_topic                        │
└──────────────────────────────────────────────┘
    ↓
[7-turn cap hit]
    ↓
[clinical_natural_close → memory_update]
```

**Right-side callout — "What gets reused":**
- ✅ `retry_orchestrator.run_turn`
- ✅ Same verifier quartet (`hint_leak`, `sycophancy`, `shape`, `pedagogy`)
- ✅ Same Bedrock client + caching
- ❌ Hint advance disabled (clinical doesn't ladder)
- ❌ Off-topic does NOT end session (deferred to natural cap)

**Code refs:**
- `conversation/assessment_v2.py:61` — `assessment_node_v2` (entry)
- `conversation/assessment_v2.py:128` — `_render_opt_in`
- `conversation/assessment_v2.py:293` — `_enter_clinical_phase` (scenario gen)
- `conversation/assessment_v2.py:402` — `_run_clinical_turn` (loop body)
- `conversation/assessment_v2.py:725` — `_render_clinical_close`

---

## SLIDE 6 — Memory update / Close (mem0 vs SQLite split)

**Purpose:** show what persists after a session ends — and what intentionally doesn't.

**Layout:** two-column split (mem0 left, SQLite right) with a center "decision tree" showing what triggers each save.

**Story beat:** *"Nidhi closes the tab. What did Sokratic remember?"*

**Center — the decision tree:**

```
[close_reason determined]
   ↓
   ├── exit_intent          → no-save bucket → skip mem0 + skip SQLite
   ├── off_domain_strike     → no-save bucket → skip mem0 + skip SQLite
   ├── reach_full           → save full to mem0 + SQLite
   ├── reach_skipped        → save to mem0 + SQLite (no clinical score)
   ├── hints_exhausted       → save to mem0 + SQLite (record gap in needs_work)
   ├── tutoring_cap         → save to mem0 + SQLite
   ├── clinical_cap         → save to mem0 + SQLite
   └── clinical_natural_close → save to mem0 + SQLite
```

**Left column — mem0 (cross-session, long-term):**
- `[Recent session]` summary text — what was studied, mastery tier, reach status
- `learning_style` cues — drawn from session behavior
- `misconceptions` — wrong-answer patterns
- `weak_topics` — concepts that need re-visiting
- *Used by:* SLIDE 3's rapport-greeting personalization + SLIDE 7's mid-session memory injection

**Right column — SQLite (structured, per-session):**
- `sessions` table: status, locked_topic_path, locked_question, locked_answer, mastery_tier, hint_level_final, turn_count, key_takeaways (the demonstrated/needs_work blob)
- `subsection_mastery` table: P(L) Bayesian Knowledge Tracing score, confidence, sessions count, recent_rationales[]
- `data/artifacts/conversations/{student_id}_{thread_suffix}_turn_N.json` — full transcript snapshot

**Bottom callout — close-LLM:**
- Single Sonnet call generates `{message, demonstrated, needs_work}` JSON
- Sees engagement metrics inline (low_effort count, help_abuse count, hint_advances_fired) so judgment is grounded in real student behavior, not tutor scaffolding text
- "no templated fallbacks" — if LLM fails, frontend renders an ErrorCard, never a fake tutor goodbye

**BKT scoring (right column footnote):**
> Per-concept knowledge tracing extends Corbett & Anderson 1995. LLM scores each session into mastery (point estimate) + confidence (coverage). Updates accumulate in `subsection_mastery` for cross-session learning trajectory.

**Code refs:**
- `conversation/lifecycle_v2.py:545` — `memory_update_node` (the orchestrator)
- `conversation/lifecycle_v2.py:342` — `_draft_close_message` (the close-LLM)
- `conversation/lifecycle_v2.py:478` — `_write_transcript_snapshot` (artifact writer)
- `conversation/lifecycle_v2.py:815` — `_persist_session_end_to_sqlite`
- `memory/mastery_store.py` — `score_session_llm`, `MasteryStore.update`
- `memory/sqlite_store.py` — schema + queries

---

## SLIDE 7 — RAG + Memory injection (cross-cutting)

**Purpose:** show two flows that run *behind the scenes* during tutoring — neither phase owns them, but they're critical.

**Layout:** two stacked sections (top = RAG, bottom = memory injection); shared example query running through both.

**Story beat:** *"Behind the scenes, every Dean and Teacher call is being grounded by retrieved chunks and informed by what we know about Nidhi from before."*

**TOP HALF — RAG retrieval flow:**

```
[Query construction]
   "Body Planes" + (subsection match)
       ↓
[Hybrid retrieval — runs in parallel]
   ├── BM25 lexical search    → top-K text chunks
   └── Qdrant vector search   → top-K dense embeddings
       ↓
[Score fusion + dedupe]
       ↓
[Metadata filter]
   chapter_num=1, section="Anatomical Terminology",
   subsection="Body Planes", element_type="paragraph"
       ↓
[Window expansion]
   For each primary chunk → fetch neighbor_prev + neighbor_next
   (so context flows naturally; the tutor sees the same paragraph
    sequence the textbook intended)
       ↓
[Final result: 6 chunks]
   primary + 2 neighbors × 2 primary chunks
```

> **Sidebar — "Why hybrid?"**
> BM25 catches exact-term matches the embeddings miss (e.g. anatomical terms that aren't well represented in the vector model). Qdrant catches semantic neighbors. Together: high recall.

**BOTTOM HALF — Memory injection points:**

There are exactly **two** in-conversation memory injections (separate from the rapport-time history load on SLIDE 3):

| # | When it fires | What it injects | Effect |
|---|---|---|---|
| **1** | At topic lock | Mem0 query: *"prior notes about this subsection for this student"* → carryover_notes | Dean's first turn plan can reference prior knowledge state |
| **2** | At hint advance | Mem0 query: *"learning_style cues for this student"* → carryover_notes | Dean's plan can prefer the student's preferred analogy/scaffold style |

Both injections are **soft** — they inform, never override. If mem0 returns nothing or errors, planning continues normally (safe wrapper).

**Bottom-right callout — "Removed: Proposition indexing":**
> Earlier architecture used proposition indexing — claims extracted from chunks indexed separately. **Removed** because: extracted claims drifted from chunk semantics, retrieval noise increased, scoring became unreliable. Reverted to raw-chunk hybrid retrieval which proved more grounded.

**Code refs:**
- `retrieval/retriever.py` — hybrid BM25+Qdrant
- `retrieval/topic_matcher.py` — BM25 over TOC for topic lock
- `conversation/mem0_inject.py` — `read_topic_lock_carryover`, `read_hint_advance_carryover`
- `conversation/topic_lock_v2.py:481` — retrieval call site
- `conversation/nodes_v2.py:585-624` — hint-advance injection call site

---

## SLIDE 8 — Analysis page + Architecture journey + What's next

**Purpose:** the closing slide. Shows post-session UI + the engineering story behind the demo + what we'd do with another sprint.

**Layout:** three horizontal bands. Top band: analysis page screenshot. Middle band: timeline of architecture decisions. Bottom band: open work as a checklist.

**Story beat:** *"The next day Nidhi opens 'My Mastery' to review. And here's how the system got built."*

**TOP BAND — Analysis page:**

UI screenshot/mockup showing:
- Breadcrumb: `My Mastery / Bone Tissue / Bone Structure / Body Planes`
- **Transcript** section (full turn-by-turn replay)
- **Summary** section: locked Q, locked answer, status, score (10%), tier (`needs_review`), `Demonstrated` line, `Needs work` line
- **Analysis chat** — scoped to this session: *"How did this session go?"* → tutor analyzes the specific transcript

Code refs:
- `backend/api/sessions.py` — `/sessions/{thread_id}/transcript`, `/sessions/{thread_id}/analyze`
- `frontend/src/routes/SessionAnalysis.tsx`

**MIDDLE BAND — Architecture journey (timeline):**

```
2026 ─── Started on Anthropic direct API
     │   - Single provider, simple keys
     │
2026 ─── Tried proposition indexing
     │   ✗ Removed — claims drifted from source, retrieval noise up
     │
2026 ─── Moved to AWS Bedrock
     │   ✓ Production reliability, multi-region, IAM-managed access
     │   ✓ Native prompt-cache support
     │
2026 ─── V1 → V2 conversation flow rewrite
     │   ✓ Verifier quartet + retry orchestrator
     │   ✓ Master prompt + vocabulary registry (single source of truth)
     │   ✓ System events + per-turn snapshots in history
     │
NOW  ─── Demo-ready
```

**BOTTOM BAND — What's next (5 honest items):**

- 🔲 **Physics textbook chunking** — deferred. Architecture refactor (V1→V2 + Bedrock migration + verifier rework) consumed the sprint. Pipeline mirror of `openstax_anatomy` is documented and ready to run.
- 🔲 **Parallelize independent API calls** — today preflight, reach_gate, and Dean.plan run sequentially in places where they don't share dependencies. Audit needed to identify the parallelizable boundaries.
- 🔲 **Cache analysis** — currently caching only static system prompts (master prompt + vocabulary registry). Need to measure cache hit rates, identify warm-up patterns, and assess whether dynamic content (recent history, locked context) can be cached at session-level.
- 🔲 **More dynamic prompt sections cacheable** — investigate splitting prompts so the stable-per-session parts (locked Q, retrieved chunks at lock time) cache separately from the volatile parts (latest student message).
- 🔲 **Tier 2 verifier compound-probability tail** — 4/11 stress scenarios still flake on synthetic worst-case (3+ consecutive flagrant violations). Real conversations don't hit this; addressing it would require consensus voting on verifier output.

**Code refs:**
- `conversation/llm_client.py` — Bedrock vs Anthropic switching, `make_anthropic_client`
- `conversation/master_prompt.py` — what's currently cached (Tier 1)
- `conversation/teacher_v2.py` — 2-tier cache structure (Tier 1 master, Tier 2 body)

---

# Visual style guide

Match the existing app aesthetic for visual continuity:

- **Theme:** dark (matches `localhost:5173/chat` screenshots)
- **Background:** near-black, subtle grid or noise
- **Accent color:** the existing accent green (matches the "TUTORING" pill in screenshots)
- **Warning color:** amber for ⚠ verifier rejections (matches the new ActivityFeed)
- **Text:** light gray for body, white for emphasis, muted gray for tooltips
- **Typography:** sans for headings, mono for code blocks
- **Card style:** matches the rounded-card/border-border classes from the app — subtle border, no shadow

# Interactive elements catalog

Each slide should have at least 2 interactive moments. Suggestions per slide:

| Slide | Interaction |
|---|---|
| 1 | Hover badges → tooltip; click question → reveals locked answer |
| 2 | Click each phase node → side panel with details |
| 3 | Click timeline step → expand prompt template / SQL queries |
| 4 | Hover counter pills → example messages; click pipeline stage → prompt details; toggle preflight verdicts |
| 5 | Toggle "what's reused" pills; play/pause clinical loop animation |
| 6 | Switch mem0/SQLite tabs; trigger close-reason scenarios |
| 7 | Run example RAG query → see chunks light up; toggle injection points |
| 8 | Click timeline events; check off "what's next" items |

# Code reference master index

Single source for all code locations. Project root: `/Users/arun-ghontale/UB/NLP/sokratic`.

| Concern | Read this |
|---|---|
| Graph wiring | `conversation/graph.py:79-138` |
| Phase routing | `conversation/lifecycle_v2.py:924-998` (`after_rapport`, `after_dean`, `after_assessment`) |
| Rapport (memory load + greeting) | `conversation/lifecycle_v2.py:48-283` |
| Topic lock (anchor gen) | `conversation/topic_lock_v2.py:173-end` |
| Tutoring orchestrator | `conversation/nodes_v2.py:77-end` |
| Preflight (8 verdicts) | `conversation/preflight.py:287-end`, `conversation/preflight_classifier.py:177-end` |
| Dean planning | `conversation/dean_v2.py` |
| TurnPlan dataclass | `conversation/turn_plan.py` |
| Retry orchestrator | `conversation/retry_orchestrator.py:198-end` |
| Verifier quartet | `conversation/verifier_quartet.py` |
| Clinical loop | `conversation/assessment_v2.py:402-end` |
| Memory update / close | `conversation/lifecycle_v2.py:545-end` |
| Mem0 injection points | `conversation/mem0_inject.py` |
| RAG retrieval | `retrieval/retriever.py`, `retrieval/topic_matcher.py` |
| SQLite schema + queries | `memory/sqlite_store.py` |
| BKT scorer | `memory/mastery_store.py` |
| Frontend analysis page | `frontend/src/routes/SessionAnalysis.tsx` |
| LLM client (Bedrock vs Anthropic) | `conversation/llm_client.py` |
| Master prompt + cache | `conversation/master_prompt.py`, `conversation/registry.py` |
| Activity log + WS protocol | `conversation/streaming.py`, `backend/api/chat.py` |

# Story / scenario assets

Real artifact for direct quoting:
- **Transcript:** `~/Downloads/sokratic_nidhi_2026-05-05T17-47-13-126Z-repeated.json`
- **Locked Q:** *"What is the key difference between a midsagittal plane and a parasagittal plane?"*
- **Locked answer:** *"Equal vs. unequal division of right and left sides"*
- **3 anchor variations** (Dean-generated, in the JSON's `dean._generate_anchor_variations` trace)
- **Counter progression** (in `per_turn_snapshots` array): turns 3-19 show low_effort and help_abuse climbing, then resetting on hint advance
- **System events** (in `system_events` array): `anchor_pick_shown` → `topic_locked` → `preflight_intervened` (3×) → `hint_advance` → `exit_modal_canceled`

The teammate can pull screenshots from running sessions for SLIDES 3, 4, 8.

# Open decisions for the teammate

1. **Static HTML or React/Vite?** — Static HTML is simpler to deploy as a 1-file deck. React allows richer interactions. Recommendation: static HTML + small `<script>` blocks for interactivity. No build step needed.
2. **Slide navigation:** keyboard arrows + visible dots, or scroll-based? Scroll-based reads more modern; keyboard is more presentation-friendly.
3. **Code blocks:** prismjs for syntax highlighting, or just `<pre><code>` with hand-styled colors?
4. **Animations:** CSS-only (transitions on click) or anime.js / framer-motion? CSS keeps it light.
5. **Accessibility:** include a "skip to slide N" jumper? Probably yes for usability.

---

*Plan author: prior agent session. Update as the deck evolves.*
