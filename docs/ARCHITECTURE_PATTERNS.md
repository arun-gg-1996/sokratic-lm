# Sokratic — Architectural Patterns

Reference for understanding what the Sokratic codebase IS in industry
terms. Useful for interviews, onboarding, architectural trade-offs.
Last updated: 2026-05-06.

---

## TL;DR

**LangGraph state machine where every turn runs a Planner-Writer-Verifier
loop with tiered model routing and a hybrid two-layer memory.**

## 30-second pitch

> Sokratic is a Socratic anatomy tutor built as a LangGraph state
> machine. Each turn runs a Planner-Writer-Verifier loop: a Dean LLM
> emits a typed TurnPlan, a Teacher LLM writes constrained by that
> plan, four parallel Haiku verifiers (leak/sycophancy/shape/pedagogy)
> judge the output — failures trigger Dean replanning. Memory is split
> L1/L2: atomic mem0+Qdrant observations for cross-session context,
> SQLite aggregates for EWMA mastery scoring. Same family as Khanmigo's
> moderator+tutor model and Replit Agent's planner-executor, but with
> a deterministic pedagogical state machine instead of a free-form
> ReAct loop — explicit phase transitions are how we guarantee
> Socratic discipline.

---

## The 5 core patterns

### 1. Planner-Executor (Dean ↔ Teacher)

Dean (Sonnet) emits a typed TurnPlan; Teacher (Sonnet) renders the
message constrained by that plan.

**Industry term:** Plan-and-Solve, Hierarchical agents, Director-Actor.
**Files:** `conversation/dean.py`, `dean_v2.py`, `teacher_v2.py`, `turn_plan.py`.

### 2. Verifier-Generator with retry

Four parallel Haiku checks (leak / sycophancy / shape / pedagogy)
judge Teacher's draft; failure → Dean replans → retry up to N → fall
through to ErrorCard (no fake tutor text).

**Industry term:** LLM-as-Judge, Self-Refine, Reflexion-lite,
Constitutional AI at inference time.
**Files:** `conversation/verifier_quartet.py`, `retry_orchestrator.py`.

### 3. State-machine agent (NOT ReAct)

Explicit phases: rapport → tutoring → assessment (opt-in → clinical) →
memory_update. LangGraph nodes + conditional edges. Phase routing is
deterministic Python, not LLM-decided.

**Industry term:** Stateful agent graph, deterministic agent orchestration.
**Files:** `conversation/graph.py`, `lifecycle_v2.py:after_dean / after_assessment`, `state.py`.

### 4. Tiered model routing

Sonnet for generation/planning. Haiku for verifiers + classifiers.
4 Haiku verifiers parallel ~1.5s + $0.001/turn vs naive all-Sonnet
~6s + $0.05/turn (50× cheaper, 4× faster).

**Industry term:** Model cascade, speculative routing, tiered inference.
**Files:** `config/base.yaml:models.*`, `preflight_classifier.py`, `verifier_quartet.py`.

### 5. Hybrid memory (L1/L2 split)

| Layer | Backend | Granularity | Use |
|---|---|---|---|
| L1 atomic | mem0 + Qdrant (vector) | per-claim | similarity recall |
| L2 aggregate | SQLite (relational) | per-session row + EWMA | mastery trajectory |

**Industry term:** Episodic + semantic memory (CoALA framework).
**Files:** `memory/memory_manager.py`, `observation_extractor.py`, `sqlite_store.py`, `conversation/mem0_inject.py`.

---

## Bonus patterns

- **Preflight classifier** — single Haiku call up-front classifies
  intent into 7 categories. **"Input guardrails"** in OpenAI/Anthropic
  vocabulary.
- **TurnPlan as typed contract** — strict dataclass with `__post_init__`
  validation. **"Structured agent communication"** / **"typed handoffs"**.
- **Activity log + chips** — `fire_activity` emits human-readable
  events streamed to UI. **"Streaming UX with provenance"** /
  **"observable agents"**.
- **Retry-then-error-card** (no templated fallbacks) — fail loud, never
  fake tutor text. **"Fail-loud agent"**.
- **Tiered counter visibility** — consecutive vs total counters
  surfaced separately. **"Telemetry observability"**.

---

## How Sokratic differs from the big-lab SDKs

### Anthropic Claude Agent SDK

| Dimension | Anthropic | Sokratic |
|---|---|---|
| Loop | ReAct, LLM-decided | State machine, Python-decided |
| Verification | Hooks (opt-in, async) | Verifier quartet (mandatory) |
| Memory | Filesystem | Hybrid mem0 + SQLite |
| Sub-agents | First-class | Not used — Dean/Teacher are call-sites |
| Tools | First-class | Retrieval is a function call |

Anthropic ships **primitives**; you compose. We baked the pedagogical
loop INTO the graph because Socratic discipline can't be opt-in.

### OpenAI Agents SDK

| Dimension | OpenAI | Sokratic |
|---|---|---|
| Agent transfer | Handoffs (LLM-decided) | TurnPlan (fixed contract) |
| Guardrails | Sync filters (block/pass) | Verifier → Dean replan loop (REWRITE) |
| Tracing | First-class | Custom debug + activity log |

OpenAI guardrails are **filters**. Our quartet is a **rewriting loop**.

### Khanmigo (Khan Academy)

Public talks describe a moderator + tutor dual-model setup with
classifier filters. Closest cousin to Sokratic. They lean on prompt
engineering + classifier filters; we lean on state machine + post-hoc
verifier loop. Both end up at similar guarantees, different mechanisms.

---

## Companies using related patterns

| Pattern | Who |
|---|---|
| Planner-Executor + LangGraph | Replit Agent, Cursor Composer, Cognition (Devin), Vercel v0 |
| Verifier-Generator with retry | Imbue, Adept, Anthropic artifacts pipeline (rumored) |
| Tiered routing | Perplexity, NotebookLM, Anthropic batch |
| State-machine pedagogy | Khanmigo, Duolingo Max (lighter), Quizlet Q-Chat |
| Hybrid memory | mem0, Letta (formerly MemGPT), OpenAI beta memory |

---

## Interview gotchas

**"Why not one big prompt?"** Latency + leak guarantees. Single calls
drift on classification questions where naming categories IS the answer.

**"Why state machine instead of ReAct?"** ReAct drifts; explicit phases
force discipline (no answer reveal in tutoring; clinical close DOES
reveal target — different one-shot).

**"Why two memory stores?"** Vector for similarity ("what mistakes
does this student make?"), relational for aggregates ("EWMA over N
sessions"). Forcing one store hurts queries or schema.

**"Why Haiku + Sonnet?"** 4 Sonnet verifiers = ~6s + $0.05/turn. 4
parallel Haiku = ~1.5s + $0.001/turn. Verification is classification;
generation needs Sonnet.

**"Verifier disagreement?"** Each evaluates a different axis. Failures
aggregate into Dean's replan critique. Fourth attempt fails →
ErrorCard (M-FB rule).

**"How measure quality?"** Three layers: Tier 1/2 unit tests, smoke
harness (18 static checks), browser-sim manual checklist.

**"Why not fine-tune?"** Verifier-and-rewriter at inference time gives
correctness with zero training data + one-day iteration. Fine-tuning
locks in behavior; verifiers can be added in a PR.

---

## Glossary — job-posting → Sokratic mapping

| Term in postings | Maps to |
|---|---|
| Agentic systems / orchestration | LangGraph state machine + Dean/Teacher/verifier loop |
| Multi-agent systems | Dean + Teacher + 4 verifiers as separate coordinated calls |
| LLM evaluation / LLM-as-judge | Verifier quartet (Haiku judges) |
| Guardrails | Preflight classifier + verifier quartet |
| Prompt engineering | `config/base.yaml` (50+ prompts, factored static + delta) |
| RAG | `retrieval/topic_matcher.py` + chunks-grounded socratic mode |
| Vector DBs | Qdrant via mem0 for L1 atomic observations |
| Memory architectures | L1 (mem0+Qdrant) + L2 (SQLite EWMA) split |
| Inference cost optimization | Tiered routing |
| Production ML / MLOps | Activity log, smoke harness, debug payloads |
| Evals / regression testing | Tier 1/2/3 pyramid + smoke harness |
| Streaming UI | WebSocket + activity chips + streaming markdown |

---

## File index

| Pattern | Files |
|---|---|
| State machine | `graph.py`, `state.py`, `lifecycle_v2.py` |
| Planner | `dean.py`, `dean_v2.py` |
| Writer | `teacher_v2.py` |
| Verifier quartet | `verifier_quartet.py` |
| Retry orchestrator | `retry_orchestrator.py` |
| Preflight | `preflight.py`, `preflight_classifier.py` |
| TurnPlan | `turn_plan.py` |
| L1 memory | `observation_extractor.py`, `mem0_inject.py`, `safe_mem0.py` |
| L2 memory | `sqlite_store.py` |
| RAG | `topic_matcher.py`, `retriever.py` |
| Architecture-block centralization | `config/base.yaml:architecture_block` (M-T2) |
| Test pyramid | `test_memory_flush_paths.py`, `test_sqlite_session_paths.py`, `test_mem0_inject_paths.py`, `scripts/smoke_post_demo_fixes.py` |
| LangGraph nodes | `nodes_v2.py`, `assessment_v2.py`, `topic_lock_v2.py` |

---

## ReAct vs Sokratic — the concrete difference

### ReAct (Reasoning + Acting, Yao et al. 2022)

The LLM is INSIDE the control flow. Each step looks like:

```
Thought: The student asked about the SA node. I should retrieve chunks.
Action: search_chunks("SA node")
Observation: [chunks returned]
Thought: Good, now I should check if the student's answer matched.
Action: check_answer("autorhythmicity")
Observation: leak=false
Thought: Now I'll write a Socratic response.
Action: write_response(...)
Observation: [draft]
Thought: I'm done.
Final Answer: <draft>
```

The LLM picks the action. Loops until it emits "Final Answer". You
give it tools; it figures out which to call.

**Examples:** AutoGPT, original LangChain agents, Anthropic Computer
Use, ChatGPT-with-tools, Devin (partly).

### Sokratic — orchestrated workflow with verifier loop

The LLM is INSIDE discrete nodes. The control flow is OUTSIDE the LLM,
in LangGraph Python edges:

```python
# Pseudocode of our turn
preflight_result = haiku_classify(student_msg)        # Haiku call #1
if preflight_result.fired:
    return teacher.draft(redirect_plan)               # Sonnet call, exit
chunks = retrieve(locked_subsection)                  # not an LLM call
plan = dean.plan(state, chunks)                       # Sonnet call #2
draft = teacher.draft(plan)                           # Sonnet call #3
checks = run_quartet_parallel(draft, plan)            # 4 Haiku calls #4-7
if any_check_failed(checks):
    plan = dean.replan(state, plan, checks)           # Sonnet call #8
    draft = teacher.draft(plan)                       # Sonnet call #9
return draft
```

The LLM picks **content** but never picks **what runs next**. Python
knows.

### Side-by-side

| Dimension | ReAct | Sokratic |
|---|---|---|
| Who decides next step? | LLM (via Thought/Action) | Python (LangGraph edge) |
| Number of LLM calls per turn | Variable (1 to ∞) | Deterministic (3-9) |
| What if LLM picks wrong tool? | Wrong outcome, no recovery unless prompt catches it | N/A — code picks |
| What if LLM drifts? | Drifts | Drifts on content only; structure stays put |
| Failure mode | Infinite loop, wrong tool, hallucinated tool | Verifier rejects → retry → ErrorCard |
| Debug story | Read Thought/Action trace | Read graph trace + per-call inputs/outputs |
| Cost predictability | Bad (variable calls) | Good (bounded calls) |
| Prompt cache hit rate | Low (full context every step) | High (each call is narrow + stable prefix) |
| Best for | Open-ended tasks (research, code exploration) | Bounded tasks with guarantees (tutoring, support, ops) |

---

## Is Sokratic's pattern "standard"?

**Yes — it's the dominant pattern for production LLM systems in
2025/2026.** It has a name in the literature now.

### The naming convention

Anthropic's December 2024 essay "Building Effective Agents" (Schluntz
& Zhang) split the world into two categories:

- **Workflows** — *"systems where LLMs and tools are orchestrated
  through predefined code paths"*
- **Agents** — *"systems where LLMs dynamically direct their own
  processes and tool usage"*

By that taxonomy, **Sokratic is a Workflow, not an Agent.** The essay
explicitly recommends workflows over agents for most production
problems. The pattern we built has even more specific names depending
on which axis you focus on:

- *"Prompt chaining + routing + parallelization + evaluator-optimizer"*
  (Anthropic's terms — we use all four)
- *"Director-Actor with critic loop"*
- *"State-machine agent"* (the LangGraph community's term)
- *"Verifier-augmented generation"*

### Companies on this exact pattern

- **Cursor Composer** — planner + executor + verifier
- **Replit Agent** — LangGraph + planner-executor
- **Cognition (Devin)** — planner-executor with verifier checks (some
  ReAct elements too)
- **Vercel v0** — workflow with verifier
- **Khanmigo** — moderator + tutor (closest analog)
- **Anthropic's own internal Claude.ai pipeline for artifacts**
  (rumored, similar shape)

So calling Sokratic *"a workflow with verifier loop"* in interviews is
**precise, current, and common vocabulary**. Not exotic.

---

## Could we have done it differently? 6 alternatives

### A. Pure ReAct with tools

One Sonnet, give it tools (`retrieve`, `check_answer`, `lock_topic`,
`score_mastery`).

- **Pro:** Simpler code, more flexible.
- **Con:** Cannot guarantee no-answer-leak. The model decides when to
  call the leak check; sometimes it skips it. Cost unpredictable. We'd
  be one prompt-injection away from disaster.
- **Verdict:** Wrong choice for tutoring. Right for code exploration /
  research agents.

### B. Single Sonnet call per turn with chain-of-thought

One big prompt with everything: chunks + state + Socratic rules + leak
prevention.

- **Pro:** Cheapest, fastest (~3s/turn vs our 5-7s).
- **Con:** Higher leak rate (one prompt can't reliably self-police).
  No retry. No cross-turn memory injection point. Prompt becomes 8000
  tokens of nested rules.
- **Verdict:** What v1 of many tutoring products look like. Quality
  plateaus quickly.

### C. Multi-agent debate

Multiple agents argue, vote, decide ("Society of Mind" / "MetaGPT"
pattern).

- **Pro:** Robust to single-model errors.
- **Con:** 5-10× cost, harder to debug, slower. No clear pedagogical
  benefit over verifier-quartet.
- **Verdict:** Overkill for a tutor. Useful for high-stakes synthesis
  (legal review, medical decisions).

### D. Linear chain with no state machine (LangChain LCEL)

`preflight | retrieve | plan | draft | verify`. No branching, no loops.

- **Pro:** Simplest to reason about.
- **Con:** Can't loop (no retry). Can't branch (no phase transitions,
  no opt-in). Can't carry stateful counters across turns. Would need
  to re-architect to add features.
- **Verdict:** What v0 looks like. Hits a ceiling fast.

### E. Fine-tuned Socratic tutor model

Train a model on tutoring transcripts, no orchestration needed.

- **Pro:** Could be cheaper at inference. "Feels" smarter.
- **Con:** 3-6 months and tens of thousands of dollars. Locked-in
  behavior — every guardrail change is a retraining cycle. Can't
  easily add new safety rules. Memory architecture still needed
  externally.
- **Verdict:** What Duolingo Max likely does for some interactions.
  Right when you have unlimited training data and stable requirements.

### F. DSPy-style compiled pipeline

Use DSPy to declare "what" and let the framework optimize prompts.

- **Pro:** Auto-tunes prompts, could outperform hand-written.
- **Con:** Less interpretable, immature ecosystem, harder to express
  the verifier-quartet structure cleanly. Requires labeled training
  data for the optimizer.
- **Verdict:** Promising research, not production-ready for our use
  case yet.

---

## So why did we pick what we picked?

The trade-off space pushes hard toward what we built once you require:

1. **Hard pedagogical guarantees** (no answer leak, ever) → kills ReAct
2. **Bounded cost per turn** → kills ReAct
3. **Sub-day iteration on safety rules** → kills fine-tuning
4. **Cross-turn stateful memory** → kills linear chains
5. **Observable failures** → kills single-prompt
6. **Production-grade quality** → kills naive multi-agent debate

What's left after those constraints is: **orchestrated workflow with
verifier loop and tiered routing** — exactly Sokratic.

This isn't a happy accident. The big production-LLM teams converged on
this pattern in 2024-2025 because the same constraints push everyone
the same way. Cursor, Replit, Khanmigo, Devin — they all look broadly
like Sokratic if you squint. They differ in WHAT the verifiers check
(code-correctness, factual accuracy, safety, pedagogy) but the SHAPE
is the same.

---

## Interview talk-track — when asked "ReAct or workflow?"

> "I built it as an orchestrated workflow rather than a ReAct agent.
> The decision was driven by hard pedagogical guarantees — no answer
> leakage, no sycophancy, deterministic phase transitions — which
> ReAct can't promise because the LLM owns the control flow. We use
> the pattern Anthropic's 'Building Effective Agents' essay calls
> 'evaluator-optimizer' (verifier-rewriter loop) layered on top of
> 'prompt chaining' (Dean → Teacher) and 'parallelization' (the four
> verifiers running concurrently). It's the same shape as Cursor's
> Composer or Replit's Agent — workflow + verifier — just specialized
> for a tutoring domain. ReAct would've been simpler to write but
> impossible to ship with our guarantees."

That answer signals: you know the literature, you know the trade-offs,
you've made the choice deliberately, and you can argue the alternatives.
