# Socratic-OT: Full Architecture & Implementation Plan

> Written in plain English so anyone on the team can follow along.

---

## What We're Building

A tutoring AI for OT (Occupational Therapy) students studying Gross Anatomy. The system follows one rule: **never give the student the answer directly**. Instead, it asks Socratic questions that guide the student to figure it out themselves. It works by reading the OpenStax A&P textbook and using that as its only source of knowledge.

**Key constraints right now:**
- No fine-tuned or local models. Everything uses GPT-4o via API.
- No SFT or DPO. That comes later.
- Fully domain-agnostic: swap the textbook → same system tutors Physics.

---

## The Two Agents: Dean and Teacher

**Teacher** — writes the Socratic responses to the student. Receives the conversation history, the relevant textbook passages, and the current hint level. Does NOT know what the correct answer is — but that's fine. Teacher doesn't need to know the answer; it just needs to ask questions that guide the student toward the content in the retrieved chunks. The chunks contain the answer, and Teacher forms questions around the concepts in those chunks.

**Dean** — the supervisor. Dean retrieves the textbook passages, extracts and privately stores the correct answer, and checks every Teacher response before it goes to the student. If Teacher leaks the answer, validates a wrong student claim, or asks no question — Dean sends it back with a critique. If Teacher fails twice, Dean writes the response to the student itself.

This means it is **structurally impossible** for Teacher to leak the answer, because Teacher never has it.

**Dean's live checks vs. EULER:**
- Dean's checks during a live session = simple pass/fail quality gate (fast, runs every turn)
- EULER = formal offline evaluation with nuanced 0–1 scores (run on saved conversation logs, not live)
- They're separate: Dean checks are for correcting Teacher in real time. EULER is for measuring system quality after the fact.

---

## Project Structure

```
sokratic/
├── ARCHITECTURE.md                 # This file
├── config.yaml                     # ALL configs, prompts, thresholds live here
├── config.py                       # Loads config.yaml, exposes as Python objects
├── ingestion/
│   ├── extract.py                  # Pull text + tables from PDF
│   ├── chunk.py                    # Split into semantic chunks
│   ├── propositions.py             # Break chunks into single facts (GPT-4o)
│   ├── index.py                    # Build FAISS + BM25 indexes
│   └── build_structure.py          # Build textbook_structure.json from TOC
├── retrieval/
│   └── retriever.py                # Hybrid search: FAISS + BM25 → RRF → cross-encoder → top-5
├── memory/
│   ├── session_memory.py           # In-session memory (LangGraph MemorySaver)
│   ├── persistent_memory.py        # Cross-session memory (mem0 + Qdrant)
│   └── memory_manager.py           # Load at start of session, flush at end
├── tools/
│   └── mcp_tools.py                # MCP tool definitions for Dean
├── conversation/
│   ├── state.py                    # TutorState TypedDict (shared between all nodes)
│   ├── dean.py                     # Dean agent logic
│   ├── teacher.py                  # Teacher agent logic
│   ├── summarizer.py               # Rolls up old turns when session gets long
│   ├── nodes.py                    # One function per LangGraph node
│   ├── edges.py                    # Routing logic between nodes
│   └── graph.py                    # Wires everything into the LangGraph graph
├── multimodal/
│   └── image_processor.py          # GPT-4o Vision → structure IDs → retrieval
├── evaluation/
│   ├── euler.py                    # Offline EULER scoring on saved conversations
│   ├── generate_rag_qa.py          # One-time: generate 150 Q&A pairs for RAG validation
│   └── blind_test.py               # Multimodal: identify structures in unlabeled diagrams
├── tests/
│   ├── test_ingestion.py           # Gate 1: chunk/proposition counts, metadata, linkage
│   └── test_rag.py                 # Gate 2: Hit@5, MRR, latency, dedup, out-of-scope
├── simulation/
│   ├── profiles.py                 # 6 student personas
│   ├── student_simulator.py        # Generates student responses from profile
│   ├── runner.py                   # Runs many conversations concurrently
│   ├── logger.py                   # Saves conversations to JSONL
│   └── seed_demo_memory.py         # Runs 5 simulated convos per profile to seed mem0
├── ui/
│   └── app.py                      # Streamlit demo interface
└── data/
    ├── raw/                        # Source PDFs
    ├── processed/
    │   ├── chunks.jsonl            # All chunks saved locally before indexing
    │   └── propositions.jsonl      # All propositions saved locally before indexing
    ├── diagrams/                   # Anatomy images + metadata JSON
    ├── indexes/
    │   ├── ot/                     # FAISS + BM25 indexes for OT domain
    │   └── physics/                # FAISS + BM25 indexes for Physics domain
    ├── eval/
│   ├── rag_qa.jsonl            # 150 Q&A pairs for RAG validation (generated once)
│   └── blind_test/
│       ├── images/             # unlabeled held-out diagrams (~10)
│       └── ground_truth.json   # [{filename, structures: [str]}]
    ├── simulations/                # JSONL output from simulation runner
    ├── textbook_structure.json     # Full topic hierarchy (human + machine readable)
    └── artifacts/                  # Everything needed for human evaluation (see Phase 5)
        ├── tool_definitions.json
        ├── prompts/
        ├── conversations/
        ├── euler_scores/
        ├── dean_interventions/
        └── session_prompts/
```

---

## config.yaml — Central Config File

Everything configurable lives here. No hardcoded values in the codebase. All prompts live here too so they're easy to inspect and tweak.

```yaml
models:
  teacher: "claude-sonnet-4-5"         # Anthropic
  dean: "claude-sonnet-4-5"            # Anthropic
  vision: "claude-sonnet-4-5"          # Anthropic (native vision, same model)
  propositions: "claude-sonnet-4-5"    # Anthropic
  summarizer: "claude-sonnet-4-5"      # Anthropic
  embeddings: "text-embedding-3-large" # OpenAI — RAG only
  cross_encoder: "cross-encoder/ms-marco-MiniLM-L-6-v2"  # local

retrieval:
  qdrant_top_k: 10
  bm25_top_k: 10
  rrf_k: 60
  top_chunks_final: 5
  out_of_scope_threshold: 0.3
  cross_encoder_input: "chunk"      # cross-encoder scores (query, chunk) pairs, NOT (query, proposition)

session:
  max_turns: 30                     # when exceeded, old turns get summarized (not exposed in UI)
  summarizer_keep_recent: 10        # always keep the last N turns intact; summarize everything before
  max_hints: 3                      # hint levels (1 = broad, 2 = medium, 3 = narrow)
  # one topic per session — no proactive_revisit_every_n needed

dean:
  max_teacher_retries: 2            # after 2 failed retries, Dean writes the student response directly
  help_abuse_threshold: 3           # consecutive low-effort turns before hint_level advances anyway

thresholds:
  student_answer_correct: 0.85      # cosine similarity floor to count student answer as correct
  answer_leak_semantic: 0.85        # cosine similarity floor to flag Teacher response as leaking
  image_structure_confidence: 0.5   # GPT-4o Vision confidence floor for structure identification

memory:
  qdrant_host: "localhost"
  qdrant_port: 6333
  kb_collection: "sokratic_kb"          # proposition embeddings (RAG knowledge base)
  memory_collection: "sokratic_memory"  # student episodic memory (mem0)

simulation:
  n_conversations: 1500
  max_concurrent: 10
  with_dean: true

prompts:
  teacher_system: |
    You are a Socratic tutor. Your only job is to ask questions that guide the student
    toward the answer — never state the answer directly.
    You will receive textbook passages and the conversation history.
    The passages contain the answer somewhere in them. Use them to form your questions.
    Always end your response with exactly one question.
    Keep your response to 3-4 sentences maximum.
    -- TEXTBOOK PASSAGES --
    {retrieved_chunks}
    -- CONVERSATION HISTORY --
    {conversation_history}
    -- CURRENT HINT LEVEL: {hint_level} of {max_hints} --
    Hint level 1: ask a broad question about the topic.
    Hint level 2: ask a more specific question pointing toward the key concept.
    Hint level 3: ask a very direct question that gives the student one final push.
    {dean_critique}

  dean_system: |
    You are the Dean — a supervisor overseeing a Socratic tutoring session.
    The CORRECT ANSWER to the current question is: {locked_answer}
    The Teacher does not know this answer. Your job is to:
    1. Check every Teacher response before it reaches the student.
    2. Use your tools to retrieve textbook content and check student answers.
    3. Only call search_textbook if you do not already have relevant context for this turn.
    Reject a Teacher response if it:
    - Contains the answer or a synonym of: {locked_answer}
    - Validates an incorrect student claim
    - Contains no question
    - Is longer than 4 sentences
    If Teacher fails twice, write the student response yourself.
    -- RETRIEVED CHUNKS (already fetched) --
    {retrieved_chunks}
    -- CONVERSATION HISTORY --
    {conversation_history}

  # All other prompts (rapport, assessment, memory_update, summarizer) follow the same pattern.
  # Each prompt is assembled with === STATIC === / === DYNAMIC === delimiters and logged to
  # data/artifacts/session_prompts/ for human inspection.
```

**Prompt structure for KV caching:** The static part (system prompt + retrieved chunks) comes first. The dynamic part (conversation history) comes last. OpenAI automatically caches the static prefix, so repeated Dean calls on the same context get faster and cheaper with each turn.

**Logging full prompts:** `dean.get_full_prompt()` and `teacher.get_full_prompt()` output the complete assembled prompt with `=== STATIC ===` / `=== DYNAMIC ===` delimiters so you can read exactly what GPT-4o sees at any point.

---

## Data Schemas (`schemas.py`)

All objects flowing through ingestion and retrieval have canonical schemas defined in `schemas.py`. Every field is documented with why it exists. Use `validate_chunk()`, `validate_proposition()`, `validate_diagram()` before indexing.

### Chunk (produced by `chunk.py`)

| Field | Type | Why it exists |
|---|---|---|
| `chunk_id` | str (UUID) | Stable ID — propositions reference this |
| `text` | str | Full chunk text — cross-encoder input |
| `chapter_num` | int | Enables chapter-level Qdrant filtering |
| `chapter_title` | str | Human readable, stored in Qdrant payload |
| `section_num` | str | e.g. "11.2" |
| `section_title` | str | Key for anatomy topic matching |
| `subsection_title` | str | Most specific level — critical for anatomy queries |
| `page` | int | Human verification |
| `element_type` | paragraph\|table\|figure_caption | Table chunks are high-value for anatomy |
| `domain` | ot\|physics | Qdrant filter — prevents cross-domain retrieval |

### Proposition (produced by `propositions.py`, indexed into Qdrant)

All chunk fields inherited, plus:

| Field | Type | Why it exists |
|---|---|---|
| `proposition_id` | str (UUID) | Unique ID per proposition |
| `text` | str | Atomic fact, no pronouns — what gets embedded and matched against queries |
| `parent_chunk_id` | str | Links back to source chunk |
| `parent_chunk_text` | str | **Full parent chunk stored in Qdrant payload** — cross-encoder uses this, no extra lookup needed |
| `image_filename` | str | Empty for textbook; diagram filename for diagram propositions — UI uses to display image |

### Diagram JSON (`data/diagrams/*.json`)

| Field | Type | Why it exists |
|---|---|---|
| `diagram_id` | str | Unique slug |
| `filename` | str | Image file in `data/diagrams/` |
| `title` | str | Human label |
| `source` | str | AnatomyTOOL\|MedPix\|OpenStax |
| `chapter_num`, `chapter_title`, `section_num`, `section_title` | — | Inherited into propositions for filtering |
| `labels_visible` | bool | False = blind test set |
| `structures` | list | Each entry → one proposition in `sokratic_kb` |

Each structure entry:

| Field | Why |
|---|---|
| `name` | Structure name — embedded in proposition text |
| `structure_type` | muscle\|nerve\|bone\|vessel\|joint\|ligament — enables type-based queries |
| `origin`, `insertion` | Key anatomy facts for muscles |
| `action` | What the structure does |
| `innervation` | Critical for OT/clinical questions |
| `clinical_note` | Patient scenario context — what OT students need |

### Qdrant Collection Spec (`sokratic_kb`)

| Setting | Value | Reason |
|---|---|---|
| `vector_size` | 3072 | `text-embedding-3-large` output dim |
| `distance` | Cosine | Semantic similarity |
| `on_disk` | False | Keep in RAM — required for < 200ms latency |

Payload = full PropositionSchema. Nothing dropped. Retriever gets `parent_chunk_text` and `image_filename` directly from Qdrant — no file lookups.

---

## Phase 1: Ingestion Pipeline

### What it produces
- `data/processed/chunks.jsonl` — every chunk, saved for human inspection **before** indexing
- `data/processed/propositions.jsonl` — every proposition, saved for inspection **before** indexing
- `data/indexes/ot/` — FAISS + BM25 indexes (built only after chunks/propositions are verified)
- `data/textbook_structure.json` — full topic hierarchy with difficulty levels

### Step 1 — Extract text from PDF
- **PyMuPDF** for paragraphs and figure captions
- **pdfplumber** for tables
- Tables are converted to plain English sentences by GPT-4o (one-time cost, ~$1)
- Every piece of text gets tagged with:
  - `chapter_num`, `chapter_title`
  - `section_num`, `section_title`
  - `subsection_title` (if it exists)
  - `page`
  - `element_type`: `paragraph` | `table` | `figure_caption`

### Step 2 — Build textbook_structure.json
- Parsed from the PDF's Table of Contents during extraction
- Format: Chapter → Section → Subsection (deepest level available in TOC)
- Each node gets a `difficulty` label: `easy` | `moderate` | `hard`
  - **Set automatically by GPT-4o during ingestion:** for each topic node, we ask "How difficult is this anatomy topic for a first-year OT student? Reply with only: easy, moderate, or hard."
  - Run once, stored permanently in the JSON. No manual labeling needed.
- Example:
```json
{
  "Chapter 11: Muscle Tissue": {
    "difficulty": "moderate",
    "sections": {
      "11.1 Interactions of Skeletal Muscles": {
        "difficulty": "easy",
        "subsections": {
          "Origin, Insertion, and Action": { "difficulty": "easy" },
          "Lever Systems": { "difficulty": "moderate" }
        }
      },
      "11.2 Naming Skeletal Muscles": {
        "difficulty": "easy"
      }
    }
  }
}
```

### Step 3 — Semantic chunking
- **LlamaIndex SemanticSplitterNodeParser**
- Joins sentences that are semantically close; splits when the topic changes (cosine similarity drops below 75th percentile)
- Target: 1,500–2,000 chunks of 600–900 characters each
- Each chunk inherits full metadata from its source text
- **Saved to `data/processed/chunks.jsonl` — inspect before proceeding to Step 4**

### Step 4 — Proposition extraction
- GPT-4o breaks each chunk into atomic single-fact sentences
- Prompt: "Rewrite this passage as a list of standalone factual statements. Each statement must be self-contained and testable."
- Each proposition stores `{text, parent_chunk_id, chapter_title, section_title, subsection_title}`
- Target: ~5,000–8,000 propositions (~$2 total one-time cost)
- **Saved to `data/processed/propositions.jsonl` — inspect before proceeding to Step 5**

### Step 5 — Build indexes

Two sources are indexed together into `sokratic_kb`:

**Source 1 — Textbook propositions** (from `propositions_ot.jsonl`)
- `element_type`: `paragraph` | `table` | `figure_caption`

**Source 2 — Diagram metadata** (from `data/diagrams/*.json`)
- `index_diagrams()` converts each structure entry in a diagram JSON into one proposition sentence
- e.g. `"The Supraspinatus initiates abduction and is innervated by the suprascapular nerve."`
- `element_type: "diagram"`, `image_filename` stored in payload so UI can display the image
- Indexed alongside textbook propositions — retriever doesn't distinguish the source

Both sources → embedded with `text-embedding-3-large` (dim=3072) → upserted to Qdrant `sokratic_kb` + BM25 pkl.

Physics textbook runs through the identical pipeline → saved to `data/indexes/physics/`.

> Steps 3–5 are separate scripts so you can inspect quality before committing to indexing.

---

## Phase 2: Retrieval Pipeline

### Key design detail — what the cross-encoder scores
- Propositions are used for initial recall (short, precise, match student queries well)
- Each matched proposition expands back to its parent chunk (full context)
- **The cross-encoder scores `(query, parent_chunk)` pairs** — not `(query, proposition)` pairs — because it needs full context to judge relevance
- Multiple propositions can point to the same parent chunk → **deduplicate by `parent_chunk_id`** before running the cross-encoder, keeping the highest-ranked proposition per chunk

### Full retrieval flow
```
Student query
  → embed with text-embedding-3-small
  → FAISS top-10 propositions  ─┐
  → BM25 top-10 propositions   ─┴→ RRF merge (k=60) → deduplicate
  → expand propositions → parent chunks
  → deduplicate parent chunks (keep highest-ranked proposition per chunk)
  → cross-encoder scores (query, chunk) for each unique chunk
  → if max score < 0.3 → out-of-scope guardrail fires
  → return top-5 chunks with metadata
```

**Total latency target: < 200ms**

---

## Phase 3: Qdrant — Two Collections

Qdrant is used for two completely separate purposes. They never interact.

| Collection | Purpose | What is stored |
|-----------|---------|---------------|
| `sokratic_kb` | RAG knowledge base | Proposition embeddings + parent chunk text. Used by FAISS for vector search. |
| `sokratic_memory` | Student episodic memory | mem0 stores past session summaries, weak topics, mastered topics per student. Searched semantically at session start. |

FAISS handles all RAG retrieval. `sokratic_kb` in Qdrant is available if we later want persistent vector storage instead of local FAISS files.

---

## Phase 4: Two-Tier Memory

### What mem0 on Qdrant is (plain English)
mem0 is a library that stores memories as vector embeddings. Think of it as a searchable notebook. You write: `"Student failed deltoid innervation 3 times"` → mem0 embeds that sentence and stores it in Qdrant. Later, searching `"shoulder muscles"` returns that memory because it is semantically close. Self-hosted on Qdrant, free to run.

### Tier 1 — Session Memory (LangGraph MemorySaver)
- Lives for the duration of the current conversation only
- Stores: current phase, weak topics added this session, turn count, locked answer (Dean's private copy), hint level, concepts covered count
- **Conversation summarizer:** when `turn_count > max_turns`, GPT-4o summarizes the oldest turns into one paragraph. Always keep the last `summarizer_keep_recent` turns (default: 10) intact. Summarize everything before that. This rolls forward as the session continues.
- **Proactive revisit:** after every 3 new concepts covered this session, Dean adds a nudge in its next evaluation: "Before we move on, you've struggled with X before — worth revisiting?"
- **At session end:** LangGraph state is cleared. The important outcomes were already flushed to Tier 2.

### Tier 2 — Persistent Memory (mem0 + Qdrant)
- Survives across sessions
- Stores per student: mastered topics (with session number), weak topics (with failure count and difficulty), full session summaries
- **On session start:** `memory_manager.load(student_id)` → pulls relevant memories → seeds `weak_topics` in Tier 1 state
- **On session end:** `memory_manager.flush(student_id, session_state)` → writes summary and updated counts to Qdrant

### How mem0 tools are called
- Dean has two MCP tools: `get_student_memory(student_id)` and `update_student_memory(student_id, text)`
- Tool definitions are passed to GPT-4o in the `tools` array when Dean is initialized
- The Dean system prompt briefly describes each tool and when to use it
- When Dean calls `update_student_memory`, it passes natural language: "Student mastered deltoid origin/insertion. Struggled with axillary nerve innervation (failed 3 times)."
- mem0 handles embedding and storage automatically

### Weak topic difficulty integration
- When a topic is marked weak, we look up its `difficulty` in `textbook_structure.json`
- Stored as: `{topic: "Chapter 11 > Deltoid Innervation", difficulty: "moderate", failure_count: 3}`
- During Rapport: select from weak topics starting with the most-failed (highest `failure_count`), optionally filtered by difficulty based on student profile

---

## Phase 5: Human Evaluation Artifacts Store

Everything needed for human analysis lives in `data/artifacts/`. Every module that needs to log something for human review writes here. This is the complete audit trail for the system.

```
data/artifacts/
├── tool_definitions.json           # Complete MCP tool schemas (name, description, parameters, examples)
├── prompts/
│   ├── teacher_system.txt          # Full assembled Teacher system prompt (latest version)
│   ├── dean_system.txt             # Full assembled Dean system prompt (latest version)
│   └── [timestamp]_*.txt           # Versioned snapshots saved whenever a prompt changes
├── conversations/
│   └── [conv_id].json              # Full conversation: all turns, phases, Dean critiques, tool calls
├── euler_scores/
│   └── [run_id].json               # Per-turn EULER scores and per-conversation averages
├── dean_interventions/
│   └── [conv_id]_interventions.json  # All cases where Dean blocked or rewrote Teacher
└── session_prompts/
    └── [conv_id]_turn_[n].txt      # Exact prompt GPT-4o received, delimited:
                                    # === STATIC === ... === DYNAMIC === ...
```

---

## Phase 6: MCP Tools (Dean Only)

Teacher does NOT have any tools. Teacher only receives what Dean passes to it in the prompt.

| Tool | What it does |
|------|-------------|
| `search_textbook(query)` | Runs hybrid retrieval, returns top-5 chunks. Dean calls this only when it does not already have relevant context for the current student message. |
| `check_student_answer(student_claim, locked_answer)` | Compares the student's claim to the locked answer using embedding cosine similarity. Returns `{correct: bool, similarity: float}`. |
| `get_student_memory(student_id)` | Fetches past weak topics and mastered topics from mem0/Qdrant. |
| `update_student_memory(student_id, memory_text)` | Writes the session outcome to mem0. Called once at session end. |

> `flag_answer_leak` is **not** a Dean tool. LeakGuard levels 1 (exact match) and 2 (semantic similarity) run automatically in Python in `dean.py` before Dean's LLM call. Level 3 (entailment) is embedded in Dean's evaluation prompt. Dean never calls it explicitly.

---

## Phase 7: LangGraph Conversation Manager

### Shared State Object
```python
class TutorState(TypedDict):
    student_id: str
    phase: str                          # "rapport" | "tutoring" | "assessment" | "memory_update"
    messages: list[BaseMessage]         # conversation history (oldest turns summarized when long)
    retrieved_chunks: list[dict]        # {text, chapter_title, section_title, subsection_title, page}
    locked_answer: str                  # extracted by Dean, never shared with Teacher
    hint_level: int                     # current hint level (1 to max_hints)
    max_hints: int                      # from config.yaml
    turn_count: int
    max_turns: int                      # from config.yaml
    concepts_covered_count: int
    weak_topics: list[str]              # seeded from mem0, updated this session
    student_reached_answer: bool
    dean_retry_count: int               # resets to 0 on each new student turn
    is_multimodal: bool
    image_structures: list[str]         # structure names from GPT-4o Vision if image uploaded
```

### Phase 1 — Rapport

```
Session starts
    ↓
Dean calls get_student_memory(student_id)
    ↓
If weak_topics is non-empty:
    → Dean suggests revisiting the most-failed weak topic (highest failure_count)
    → Streamlit renders this as an interactive choice:
        [ Revisit: "Deltoid Innervation (failed 3x)" ]  [ Pick something else ]
    → Student can accept OR click "Pick something else"
    → "Pick something else": Streamlit shows topic selector from textbook_structure.json
      (grouped by chapter, filterable by difficulty)
If no history:
    → Dean greets student
    → Streamlit shows topic selector immediately
    ↓
Student picks a topic via UI
    ↓
Dean calls search_textbook(topic) → retrieve overview chunks for orientation
    → Does NOT lock the answer yet
    → locked_answer is set only when the student asks their first specific question in Phase 2
Move to Phase 2
```

**Why not lock the answer at topic selection:** The topic is broad (e.g. "Shoulder Muscles"). There are many possible questions within it. We lock the answer when the student's first specific question gives us a precise retrieval target.

**Streamlit + LangGraph integration:** Streamlit calls `graph.invoke(state)` or `graph.stream(state)`, receives the updated state, and renders the new messages. MCP tool calls happen entirely inside the graph — Streamlit never calls tools directly. The topic selector in Rapport is a `st.selectbox` that populates the student's first message to the graph.

### Phase 2 — Socratic Tutoring (the Dean-Teacher loop)

```
Student message arrives
    ↓
Dean checks: do I already have relevant chunks for this question?
    → No: call search_textbook(student_message) → update retrieved_chunks in state
    → Yes: skip the tool call, use existing chunks
    ↓
If locked_answer not yet set:
    → Dean extracts the answer from top retrieved chunk → stores as locked_answer (immutable for this topic)
    ↓
Dean calls check_student_answer(student_message, locked_answer)
    → Correct → set student_reached_answer = True → move to Phase 3
    → Incorrect → continue tutoring
    ↓
Dean passes to Teacher:
    - retrieved_chunks (raw text — the answer is somewhere in there)
    - conversation history
    - hint_level and max_hints
    - dean_critique (empty on first attempt)
    ↓
Teacher drafts a Socratic response, ends with one question
    ↓
Dean checks Teacher's response:
    a. flag_answer_leak(response, locked_answer) → leaked?
    b. No question in response?
    c. Validates a wrong student claim (sycophancy check)?
    ↓
PASS → send to student, increment turn_count
FAIL (dean_retry_count < 2):
    → store Dean's critique in state
    → loop Teacher back for a retry
    → increment dean_retry_count
FAIL (dean_retry_count >= 2):
    → Dean writes the student response directly (Dean IS the fallback — no separate GPT-4o call needed)
    ↓
After every 3 new concepts covered this session:
    → Dean adds a weak topic revisit nudge in its next response
    ↓
If hint_level has reached max_hints and student still hasn't answered:
    → Move to Phase 3
```

### Phase 3 — Assessment

- **Student reached the answer:** Teacher (still supervised by Dean) asks a clinical application question.
  - Example: "Now that you know the deltoid is innervated by the axillary nerve — if a patient dislocated their shoulder and damaged that nerve, what movement would they lose?"
- **Student did not reach the answer:** Dean reveals the answer with textbook context. Topic is added to `weak_topics` with incremented `failure_count`.

### Phase 4 — Memory Update

- Dean calls `update_student_memory(student_id, summary)`
- Summary: "Session {n}. Covered: {topics}. Mastered: {mastered}. Struggled with: {weak} (failure count updated)."
- LangGraph state cleared. Ready for the next session.

---

## Phase 8: Conversation Summarizer

When `turn_count > max_turns`:
- Keep the last `summarizer_keep_recent` turns intact (default: 10)
- GPT-4o summarizes all earlier turns into one summary paragraph
- The summary replaces the old turns in `messages`
- Rolling: if the session keeps going and hits the limit again, the next oldest batch gets summarized the same way

---

## Phase 9: KV Caching

OpenAI automatically caches repeated prompt prefixes. We order prompts to maximize cache hits:

- **Static part (first):** system prompt + tool definitions + retrieved chunks
- **Dynamic part (last):** conversation history

Retrieved chunks often stay the same for several turns within one topic, so the long static prefix gets cached. Each turn only pays for the small dynamic portion. No extra code needed — just careful prompt ordering.

---

## Phase 10: Multimodal Pipeline

### How it works

**All image pre-processing happens in Streamlit before `graph.invoke()` — the graph itself never changes.**

```
Student uploads image (Streamlit)
        ↓
Streamlit calls process_image(image_bytes, retriever)
        ↓
Claude Vision (claude-sonnet-4-5) identifies structures
→ [{name: "Deltoid", confidence: 0.92}, {name: "Supraspinatus", confidence: 0.87}]
        ↓
Filter structures with confidence ≥ cfg.thresholds.image_structure_confidence (0.5)
        ↓
For each passing structure: retriever.retrieve(structure_name)
→ merge all results → dedup by chunk_id → top-5 by cross-encoder score
        ↓
Streamlit sets in state:
  is_multimodal = True
  image_structures = ["Deltoid", "Supraspinatus"]
  retrieved_chunks = [top 5 merged chunks]
        ↓
graph.invoke(state)  ← same graph, same nodes, nothing special
```

### How Claude knows it's a multimodal session

The Teacher system prompt contains a `MULTIMODAL MODE` block that reads `{is_multimodal}` and `{image_structures}` from state at runtime:

- **`is_multimodal=False`** → Claude ignores the block entirely, normal text tutoring
- **`is_multimodal=True`, `locked_answer` empty (first turn)** → Claude asks a focusing question: *"I can see this shows the Deltoid — would you like to explore its origin and insertion, its innervation, or its action?"* Does NOT tutor yet.
- **`is_multimodal=True`, `locked_answer` set (subsequent turns)** → Normal Socratic tutoring. Claude may refer to the diagram in its questions.

No separate graph path. The prompt signal is the only difference.

### Edge cases
- **Non-anatomy image** → all cross-encoder scores below `out_of_scope_threshold` → empty chunks → out-of-scope guardrail fires
- **Low confidence** → `low_confidence=True` returned → `image_structures=[]` → Teacher asks student to describe what they see
- **Unlabeled diagram** → identified structures used as starting hints regardless of labels

### Blind test evaluation
A held-out set of ~10 unlabeled diagrams in `data/eval/blind_test/` is used to evaluate vision accuracy. Run `evaluation/blind_test.py` → target mean recall ≥ 0.75.

---

## Phase 11: Stage Gate Testing

Every major stage has a hard test gate. **Do not proceed to the next stage until the current gate passes.**

### Gate 1 — Ingestion (`tests/test_ingestion.py`)

Run immediately after ingestion. Checks:

| Test | What it catches |
|------|----------------|
| Chunk count in range [1000–3000] | Extraction failure or over-chunking |
| Proposition count in range [3000–12000] | GPT-4o proposition step failed silently |
| All chunks have required metadata fields | Missing chapter/section metadata |
| No chunks shorter than 100 chars | PDF extraction artifacts, page headers |
| All proposition `parent_chunk_id` values exist in chunks | Broken linkage |
| `textbook_structure.json` exists with difficulty labels | Build structure step failed |

```bash
python -m pytest tests/test_ingestion.py -v
```

### Gate 2 — RAG Validation (`tests/test_rag.py`) — HARD REQUIREMENT

**Must pass before building Dean/Teacher agents.**

#### Step 1 — Generate the eval dataset (one-time, run after ingestion)
```bash
python -m evaluation.generate_rag_qa
```

This calls GPT-4o on 150 sampled textbook chunks (stratified across chapters) and generates one student-style question + expected answer per chunk. Output saved to `data/eval/rag_qa.jsonl`.

Each record:
```json
{
  "question":        "What nerve innervates the deltoid muscle?",
  "expected_answer": "The deltoid is innervated by the axillary nerve (C5–C6).",
  "source_chunk_id": "chunk_0482",
  "chapter_title":   "Chapter 11: Muscle Tissue",
  "section_title":   "11.2 Naming Skeletal Muscles"
}
```

Cost: ~$0.50–1.00 (one-time). Dataset is saved permanently — never regenerate unless the textbook changes.

#### Step 2 — Run retrieval against the eval dataset
```bash
python -m pytest tests/test_rag.py -v
```

| Metric | Threshold | What failure means |
|--------|----------|--------------------|
| Hit@5 | ≥ 0.70 | Retrieval not surfacing the right chunks |
| MRR | ≥ 0.40 | Right chunk found but ranked too low — cross-encoder issue |
| Latency | < 200ms per query | Qdrant not running, or cross-encoder bottleneck |
| Out-of-scope | Empty list | `out_of_scope_threshold` too low |
| Duplicates | None | Deduplication bug in RRF merge |

---

## Phase 12: EULER Evaluation (Offline Only)

Run on saved conversation logs after the fact. **Not run live.**

| Criterion | What it checks |
|-----------|---------------|
| Question Present | Did the tutor response contain a question? |
| Relevance | Is the response relevant to the student's last message? |
| Helpful | Does it advance understanding without giving the answer? |
| No Reveal | Is the locked answer absent from the response? |

GPT-4o scores each criterion 0–1. Average per conversation. **Target: > 0.75.**

Results saved to `data/artifacts/euler_scores/`.

---

## Phase 13: Six Student Profiles

Six profiles cover the full spectrum of real student behavior while keeping the simulation manageable.

| ID | Profile | Behavior | What it tests |
|----|---------|----------|--------------|
| S1 | Strong | Gets answer by turn 2–3 with no hints | Happy path end-to-end |
| S2 | Moderate | Gets answer with 1–2 hints, partial answers | Normal tutoring flow |
| S3 | Weak | Needs all hints, often fails to answer | Full hint progression + assessment phase |
| S4 | Overconfident/Wrong | States wrong answers confidently | Sycophancy guard (primary test) |
| S5 | Disengaged | Vague replies, lots of "I don't know" | Edge case: no engagement |
| S6 | Anxious/Correct | Right reasoning but heavily hedged: "maybe...?" | Over-validation guard |

Each profile is a Python dataclass:
```python
@dataclass
class StudentProfile:
    profile_id: str
    name: str
    response_strategy: Callable[[str, int, str], str]  # (topic, hint_level, target_answer) -> response
    correct_answer_prob: float      # probability of being correct at each hint level
    error_patterns: list[str]       # common wrong answers (used for sycophancy testing)
    engagement_level: float         # 0.0 to 1.0, affects response verbosity
```

### Demo Memory Seeding (`seed_demo_memory.py`)
Instead of crafting fake history manually, we run 5 real simulated conversations per profile through the actual system. The outcomes (weak topics, mastered topics, failure counts) get stored in mem0 naturally. This gives us realistic memory state for the demo without any hardcoding.

---

## Phase 14: Simulation Runner

**Why async?** 1,500 conversations one-by-one would take many hours. With `asyncio`, up to 10 conversations run in parallel — each is independent so this is safe. GPT-4o API supports concurrent requests.

### Distribution
- 6 profiles × ~15 anatomy topics from OpenStax chapters
- ~17 conversations per profile-topic combination = ~1,500 total

### Output per conversation (JSONL)
```json
{
  "conv_id": "uuid",
  "student_profile": "S3",
  "topic": "Chapter 11 > 11.2 Naming Skeletal Muscles > Deltoid Innervation",
  "topic_difficulty": "moderate",
  "turns": [
    {"role": "tutor", "content": "...", "phase": "tutoring", "turn": 1},
    {"role": "student", "content": "...", "turn": 2}
  ],
  "outcome": {
    "reached_answer": false,
    "turns_taken": 9,
    "hints_used": 3,
    "weak_topics_added": ["Chapter 11 > Deltoid Innervation"],
    "euler_scores": {
      "question": 0.9,
      "relevance": 0.85,
      "helpful": 0.7,
      "no_reveal": 1.0
    }
  },
  "dean_interventions": 2
}
```

---

## Phase 15: Streamlit UI

### Sidebar
- Domain selector: OT ↔ Physics
- Student profile selector (demo mode)
- Show Dean intervention log (debug toggle)
- Current turn count (read-only)
- Hint level tracker (visual progress bar)

### Main chat area
- Chat window (tutor ↔ student) — renders `state["messages"]` only; `state["retrieved_chunks"]` is never shown to the student
- Image upload button (multimodal path)
- Topic selector (shown during Rapport phase)
- Optional collapsed debug expander showing `retrieved_chunks` for development inspection

### Info panel
- Weak topics added this session
- Weak topics from past sessions (loaded from mem0)
- Concepts covered this session
- Post-session EULER score (shown after session ends, not live)

---

## Dependencies

```
langchain
langgraph
langchain-openai
faiss-cpu
rank_bm25
sentence-transformers
llama-index
llama-index-core
pymupdf
pdfplumber
mem0ai
qdrant-client
streamlit
ragas
openai
torch
transformers        # for cross-encoder only, no local LLM
tqdm
asyncio
aiohttp
pyyaml
```

---

## Module Testing Plan

Every module has specific tests before moving on. Tests live in `tests/`. Run with `pytest tests/<file> -v`.

---

### Module 1 — Ingestion (`tests/test_ingestion.py`)

**What to run:** `pytest tests/test_ingestion.py -v`

| Test | How | Pass condition |
|------|-----|---------------|
| Chunk count | `len(chunks)` from `chunks_ot.jsonl` | 1000–3000 |
| Proposition count | `len(propositions)` from `propositions_ot.jsonl` | 3000–12000 |
| Chunk metadata complete | Check every chunk has: `chunk_id`, `text`, `chapter_title`, `section_title`, `page`, `element_type` | 0 missing |
| Proposition metadata complete | Check every prop has: `proposition_id`, `text`, `parent_chunk_id`, `chapter_title`, `section_title` | 0 missing |
| No short chunks | `len(chunk["text"]) >= 100` for all chunks | 0 violations |
| Parent chunk linkage | Every `proposition["parent_chunk_id"]` exists in chunk set | 0 orphans |
| Textbook structure exists | `textbook_structure.json` exists, non-empty, every chapter has `difficulty` label | Pass |
| Table chunks detected (hard) | `element_type == "table"` count across all chunks | ≥ 30 total |
| Figure captions detected (hard) | `element_type == "figure_caption"` count | > 0 |
| Per-chapter table report (informational) | Prints table count per chapter, flags chapters with 0 | Warning only — some chapters have no tables |
| Element type distribution (informational) | Prints % breakdown of paragraph/table/figure_caption | No `unknown` element types |

**Manual inspection (before running tests):**
- Open `chunks_ot.jsonl`, read 10 random chunks — do they look like clean anatomy text?
- Check a few propositions — are they atomic single facts or did GPT-4o merge things?
- Open `textbook_structure.json` — does the chapter hierarchy match the actual textbook?

---

### Module 2 — Retrieval (`tests/test_rag.py`) ⛔ HARD GATE

**Prerequisite:** Run `python -m evaluation.generate_rag_qa` first to build the eval dataset.

**What `generate_rag_qa.py` does:**
- Loads all chunks from `chunks_ot.jsonl`
- Stratified sample of 150 chunks across all chapters (so every chapter is represented)
- For each chunk, Claude generates one student-style question + short expected answer grounded in that chunk
- Saves to `data/eval/rag_qa.jsonl` — generated once, kept permanently

**What to run:** `pytest tests/test_rag.py -v`

| Test | How | Pass condition |
|------|-----|---------------|
| Hit@5 | For each Q in `rag_qa.jsonl`: run `retriever.retrieve(question)`, check if `source_chunk_id` is in top-5 | ≥ 0.70 |
| MRR | Mean reciprocal rank of `source_chunk_id` across all queries | ≥ 0.40 |
| Latency | Time each retrieval call | All < 200ms |
| Out-of-scope | 3 off-topic queries (e.g. "capital of France") | Returns empty list |
| No duplicates | No repeated `chunk_id` in any result set | 0 duplicates |
| Result count | Every in-scope query returns 1–5 chunks | Pass |

**Interpreting failures:**
- Hit@5 low → proposition extraction too lossy, or embedding quality poor — re-run propositions step
- MRR low → right chunk retrieved but ranked poorly → tune cross-encoder threshold
- Latency failing → Qdrant not running, or cross-encoder slow on CPU → check Docker + batch size
- Out-of-scope fails → lower `out_of_scope_threshold` in `config.yaml`

---

### Module 3 — MCP Tools (`tests/test_tools.py`) — to be created

**What to run:** `pytest tests/test_tools.py -v`

| Test | How | Pass condition |
|------|-----|---------------|
| `search_textbook` | Call with "deltoid innervation" → mock retriever | Returns list of dicts with correct fields |
| `check_student_answer` correct | student_claim="axillary nerve", locked="axillary nerve" | `correct=True`, similarity ≥ 0.85 |
| `check_student_answer` incorrect | student_claim="radial nerve", locked="axillary nerve" | `correct=False` |
| `flag_answer_leak` level 1 | response_text contains locked_answer verbatim | `leaked=True`, `level=1` |
| `flag_answer_leak` level 2 | response_text contains near-synonym | `leaked=True`, `level=2` |
| `flag_answer_leak` clean | response text has no answer | `leaked=False` |
| `get_student_memory` | Call with known student_id in Qdrant | Returns list of memory strings |
| `update_student_memory` | Write then read back | Written record retrievable |

---

### Module 4 — Memory (`tests/test_memory.py`) — to be created

| Test | How | Pass condition |
|------|-----|---------------|
| Session start load | `memory_manager.load(student_id)` for seeded student | Returns non-empty `weak_topics` list |
| Session end flush | Run full session → `memory_manager.flush()` | Qdrant `sokratic_memory` has new record |
| Cross-session persistence | Flush session 1 → load session 2 | Weak topics from session 1 appear in session 2 |
| New student | `load()` for unknown `student_id` | Returns empty list, no crash |
| Weak topic sort | Multiple weak topics with different `failure_count` | Returned sorted by `failure_count` descending |

---

### Module 5 — Dean + Teacher + Graph (`tests/test_conversation.py`) — to be created

These are integration tests — run the actual graph with mock student inputs.

| Test | How | Pass condition |
|------|-----|---------------|
| Hard turn rule | Inject answer at turn 1 | Dean blocks, `student_reached_answer` stays False |
| LeakGuard level 1 | Force Teacher to return text containing `locked_answer` verbatim | Dean rejects, `dean_retry_count` increments |
| LeakGuard level 2 | Force Teacher to return near-synonym of answer | Dean rejects |
| Sycophancy guard | Send wrong answer confidently (S4 pattern) | Dean critique fires, Teacher does not validate |
| Help abuse counter | Send "I don't know" 3 times in a row | Counter hits threshold, hint_level advances |
| Help abuse reset | Send real attempt after 2 low-effort turns | Counter resets to 0 |
| `locked_answer` not set at topic selection | Invoke rapport node, check state | `locked_answer == ""` after rapport |
| `locked_answer` set on first specific question | Send first question in tutoring phase | `locked_answer != ""` |
| Hint progression | Progress through all 3 hints | `hint_level` increments correctly |
| Assessment trigger | Exhaust hints | Phase transitions to "assessment" |
| Student reaches answer | Send correct answer | `student_reached_answer=True`, assessment triggered |
| Dean fallback | Force 2 Teacher failures in a row | Dean writes response directly |

---

### Module 6 — Conversation Summarizer (`tests/test_summarizer.py`) — to be created

| Test | How | Pass condition |
|------|-----|---------------|
| Trigger condition | Set `turn_count > max_turns` | Summarizer fires |
| Recent turns preserved | 40-turn conversation → summarize | Last 10 turns intact, verbatim |
| Old turns replaced | Check `messages` after summarization | Messages before last 10 are a single summary paragraph |
| Answer not in summary | Summary paragraph | `locked_answer` string absent from summary |
| Rolling | Continue after summarization, hit limit again | Second oldest batch gets summarized |

---

### Module 7 — Multimodal (`tests/test_multimodal.py`) — to be created

| Test | How | Pass condition |
|------|-----|---------------|
| Structure identification | Send labeled shoulder diagram | Deltoid, Supraspinatus identified with confidence > 0.5 |
| Low confidence path | Send blurry/ambiguous image | `low_confidence=True` returned |
| Non-anatomy image | Send photo of a car | All scores below threshold, empty chunks |
| Multi-structure merge | Image with 3 structures | Chunks from all 3 structures in results, no duplicates |
| State population | After `process_image()` | `is_multimodal=True`, `image_structures` non-empty, `retrieved_chunks` non-empty |
| Blind test | Run `evaluation/blind_test.py` on held-out set | Mean recall ≥ 0.75 |

---

### Module 8 — EULER Evaluation

Not a pytest test — run as a script on saved conversation logs.

```bash
python -m evaluation.euler --input data/artifacts/conversations/ --output data/artifacts/euler_scores/
```

| Criterion | What Claude judges | Pass condition |
|---|---|---|
| Question present | Does the tutor response end with a question? | Score > 0.9 (almost always) |
| Relevance | Is the response relevant to the student's last message? | Average > 0.75 |
| Helpful | Does it advance understanding without giving the answer? | Average > 0.75 |
| No reveal | Is the locked answer absent from the response? | Score > 0.95 |

Run on 5 manually crafted conversations first. Target: overall average > 0.75.

---

### Module 9 — Simulation

| Test | How | Pass condition |
|------|-----|---------------|
| Smoke test | 1 profile × 5 topics = 50 convos | All 50 complete, valid JSONL, all fields present |
| Profile distribution | Full 1500-convo run | S1 reaches answer most often, S3 least |
| No crashes | Full run | 0 uncaught exceptions in logs |
| Dean interventions logged | Check artifacts | `dean_interventions/` has files for convos where Dean intervened |

---

## Development Order — Build, Test, Move On

| Step | What gets built | Gate before moving on |
|------|----------------|-----------------------|
| **1** | `config.yaml` + project scaffold + directory structure | Config loads, all imports work |
| **2** | Ingestion: extract → chunk → propositions → `textbook_structure.json` | Manually inspect `chunks.jsonl` and `propositions.jsonl`. Verify structure hierarchy and difficulty labels. Run indexing only after inspection. |
| **2a** ⛔ | **Gate: `pytest tests/test_ingestion.py`** | Chunk count, proposition count, metadata fields, parent_chunk linkage all pass. |
| **3** | RAG Q&A eval dataset: `python -m evaluation.generate_rag_qa` | `data/eval/rag_qa.jsonl` has ≥ 100 records covering all chapters. Spot-check 10 manually. |
| **4** | Retrieval pipeline | — |
| **4a** ⛔ | **Gate: `pytest tests/test_rag.py`** — HARD REQUIREMENT | Hit@5 ≥ 0.70, MRR ≥ 0.40, latency < 200ms, no duplicates, out-of-scope returns empty. **Do not build agents until this passes.** |
| **5** | Human eval artifacts store | `tool_definitions.json` written. Prompt logs appear in `artifacts/session_prompts/`. |
| **6** | MCP tools | Unit test each tool with mock inputs. |
| **7** | Memory system (both tiers) | Full session → end → Qdrant record updated. New session → weak topics load. |
| **8** | Dean + Teacher agents + LangGraph 4-phase graph | 5 manual conversations. Dean blocks leaks. Sycophancy guard fires. `locked_answer` set on first question. Help abuse counter resets on real attempt. |
| **9** | Conversation summarizer | 40-turn conversation → oldest turns summarized, last 10 intact. |
| **10** | Multimodal pipeline | 3 shoulder diagrams → structures identified → Socratic loop starts. Run `evaluation/blind_test.py` → mean recall ≥ 0.75. |
| **11** | EULER evaluation | 5 manual transcripts scored → average > 0.75. |
| **12** | Student profiles + demo seed | Profiles generate realistic responses. `seed_demo_memory.py` populates mem0 (5 convos per profile). |
| **13** | Simulation smoke test | 1 profile × 5 topics (50 convos) → clean JSONL, all fields, EULER scores present. |
| **14** | Full simulation | 1,500 convos → distribution sensible per profile. |
| **15** | Streamlit UI | Domain toggle, topic selector, weak topics panel, multimodal upload all work. |

---

## End-to-End Platform Testing

> **High-level only — details to be worked out when implementation is complete.**

Once the full platform is built, we need a way to test the complete system as a human user would experience it — not unit tests, but real flows where someone (or a script) types messages and we verify the system behaves correctly end-to-end.

### What needs to be tested

**Flow 1 — Happy path (text)**
A student picks a topic, asks a question, gets Socratic hints, reaches the answer.
What to verify: hint progression, locked_answer never revealed, assessment triggers, memory updated.

**Flow 2 — Student never answers (hints exhausted)**
Student says "I don't know" every turn until hints run out.
What to verify: help abuse counter fires, hint_level advances at threshold, Dean reveals answer at assessment, topic added to weak_topics.

**Flow 3 — Sycophancy attempt**
Student confidently states a wrong answer.
What to verify: Dean critique fires, Teacher does not validate the wrong answer.

**Flow 4 — Answer leak attempt**
Force Teacher to return the answer verbatim (can be done by injecting into mock).
What to verify: LeakGuard blocks it, dean_retry_count increments, Dean writes fallback.

**Flow 5 — Multimodal path**
Upload a shoulder diagram, go through the full vision → focusing question → tutoring flow.
What to verify: structures identified, focusing question asked first, tutoring starts after student picks direction, diagram shown in UI.

**Flow 6 — New session with memory**
Complete a session where student fails a topic → start a new session → verify weak topic suggested in rapport.

**Flow 7 — Domain swap**
Switch from OT to Physics in the sidebar → verify Physics indexes loaded, tutoring works on a Physics topic.

**Flow 8 — Out-of-scope query**
Student asks something completely unrelated to anatomy.
What to verify: out-of-scope guardrail fires, no chunks retrieved, graceful redirect.

### How to run these tests

- **Manual:** A team member plays the student role and follows a test script, checking behaviour at each step.
- **Scripted:** Use the student simulator (profiles.py + student_simulator.py) to replay predefined conversation scripts against the live graph and assert on state fields at each turn.
- **Details to be decided:** exact tooling, assertion format, how to inject edge cases (e.g. forced Teacher leak) in the live system.

---

## Verification Checklist

- [ ] Retrieval: 20 anatomy queries return relevant chunks (RAGAS faithfulness > 0.85)
- [ ] Deduplication: multiple propositions from the same parent chunk → only 1 cross-encoder input
- [ ] Dean blocks a manually injected answer-leaking Teacher response
- [ ] `locked_answer` is NOT set at topic selection — only set on first specific student question
- [ ] Sycophancy: S4 sends wrong answer → Dean critique fires → Teacher retry does not validate
- [ ] Hint level escalates correctly; after `max_hints` → Assessment phase
- [ ] Summarizer fires at `turn_count > max_turns`; last 10 turns intact
- [ ] Session end: LangGraph state cleared; Qdrant record updated
- [ ] New session: weak topics loaded from mem0
- [ ] Multimodal: shoulder image → structures identified → Socratic loop starts
- [ ] EULER > 0.75 on 5 manual transcripts
- [ ] Simulation smoke test: 50 convos → valid JSONL with all fields
- [ ] Full simulation: 1,500 convos → distribution sensible per profile
- [ ] Physics domain swap: toggle → Physics indexes loaded → tutoring works
