# Test Plan — Socratic-OT (Phase 2, No RAG)

Tests are organized by what they require. Run them in order — standalone module tests first,
then tests that require multiple modules working together.

---

## Prerequisites

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install -r requirements.txt
```

Qdrant is optional. If not running, memory tests degrade gracefully (empty weak_topics).

---

## Layer 0 — Import / Config sanity (no API, no LLM)

**What's needed:** Nothing. Just Python.

```bash
# All config keys accessible
python -c "
from config import cfg
assert hasattr(cfg.session, 'max_turns')
assert hasattr(cfg.session, 'summarizer_keep_recent')
assert hasattr(cfg.session, 'max_hints')
assert hasattr(cfg.dean, 'max_teacher_retries')
assert hasattr(cfg.dean, 'help_abuse_threshold')
assert hasattr(cfg.prompts, 'teacher_rapport')
assert hasattr(cfg.prompts, 'teacher_socratic')
assert hasattr(cfg.prompts, 'teacher_clinical')
assert hasattr(cfg.prompts, 'dean_setup')
assert hasattr(cfg.prompts, 'dean_quality_check')
assert hasattr(cfg.prompts, 'dean_assessment')
assert hasattr(cfg.prompts, 'dean_memory_summary')
assert hasattr(cfg.prompts, 'summarizer_system')
print('Config OK')
"

# State initializes correctly
python -c "
from conversation.state import initial_state
from config import cfg
s = initial_state('test_01', cfg)
assert s['assessment_turn'] == 0
assert s['debug']['api_calls'] == 0
assert s['debug']['turn_trace'] == []
assert 'guiding_question_chain' not in s
print('State OK')
"

# MockRetriever returns 5 chunks
python -c "
from retrieval.retriever import MockRetriever
r = MockRetriever()
chunks = r.retrieve('deltoid innervation')
assert len(chunks) == 5
assert all('text' in c for c in chunks)
print('MockRetriever OK')
"

# Tool schemas load
python -c "
from tools.mcp_tools import DEAN_TOOLS, SUBMIT_TURN_EVALUATION
assert any(t['name'] == 'submit_turn_evaluation' for t in DEAN_TOOLS)
assert 'student_state' in SUBMIT_TURN_EVALUATION['input_schema']['properties']
print('Tools OK')
"
```

---

## Layer 1 — Routing unit tests (no API, pure Python)

**What's needed:** Nothing.

```bash
pytest tests/test_conversation.py -v -k "not integration and not scenarios"
```

**What these test:**
- `after_dean` routes to `assessment_node` when `student_reached_answer=True`
- `after_dean` routes to `assessment_node` when `hint_level > max_hints`
- `after_dean` routes to `assessment_node` when `turn_count >= max_turns`
- `after_dean` returns `END` in normal tutoring flow
- `after_assessment` routes to `memory_update_node` when `assessment_turn == 2`
- `after_assessment` returns `END` when `assessment_turn == 1` (waiting for clinical answer)
- Help abuse counter logic: 3 consecutive low_effort → flag to advance hint
- Help abuse counter resets on real attempt

**Pass criteria:** All tests green, no LLM calls made.

---

## Layer 2 — Standalone module tests (require `ANTHROPIC_API_KEY`)

### 2a. Graph compiles

```bash
python -c "
from conversation.graph import build_graph
from retrieval.retriever import MockRetriever
from memory.memory_manager import MemoryManager
g = build_graph(MockRetriever(), MemoryManager())
print('Graph compiled OK:', type(g))
"
```

### 2b. Teacher — each wrapper independently

```bash
python -c "
from conversation.teacher import TeacherAgent
from conversation.state import initial_state
from config import cfg

t = TeacherAgent()

# draft_rapport: new student
greeting = t.draft_rapport([])
assert len(greeting) > 10
assert '?' in greeting
print('draft_rapport (new student):', greeting[:80])

# draft_rapport: student with history
weak = [{'topic': 'Deltoid Innervation', 'difficulty': 'moderate', 'failure_count': 3}]
greeting2 = t.draft_rapport(weak)
print('draft_rapport (with history):', greeting2[:80])
"

python -c "
from conversation.teacher import TeacherAgent
from conversation.state import initial_state
from retrieval.retriever import MockRetriever
from config import cfg

t = TeacherAgent()
state = initial_state('test', cfg)
state['retrieved_chunks'] = MockRetriever().retrieve('deltoid')
state['hint_level'] = 1
state['student_state'] = 'incorrect'
state['messages'] = [{'role': 'student', 'content': 'What innervates the deltoid?'}]

draft = t.draft_socratic(state)
assert '?' in draft, f'No question in draft: {draft}'
assert state['debug']['api_calls'] == 1
print('draft_socratic OK:', draft[:100])
"
```

### 2c. Dean `_setup_call` — locks answer and classifies student

```bash
python -c "
from conversation.dean import DeanAgent
from conversation.state import initial_state
from retrieval.retriever import MockRetriever
from memory.memory_manager import MemoryManager
from config import cfg

m = MemoryManager()
d = DeanAgent(MockRetriever(), m.persistent)
state = initial_state('test', cfg)
state['retrieved_chunks'] = MockRetriever().retrieve('deltoid')
state['messages'] = [{'role': 'student', 'content': 'What nerve innervates the deltoid?'}]

result = d._setup_call(state)
print('student_state:', result['student_state'])
print('locked_answer:', result['locked_answer'])
print('hint_level:', result['hint_level'])
assert result['student_state'] in ['correct','partial_correct','incorrect','question','irrelevant','low_effort']
assert result['locked_answer'] != ''
print('_setup_call OK')
"
```

### 2d. Dean `_quality_check_call` — PASS and FAIL paths

```bash
python -c "
from conversation.dean import DeanAgent
from conversation.state import initial_state
from retrieval.retriever import MockRetriever
from memory.memory_manager import MemoryManager
from config import cfg

m = MemoryManager()
d = DeanAgent(MockRetriever(), m.persistent)
state = initial_state('test', cfg)
state['locked_answer'] = 'axillary nerve'
state['messages'] = [{'role': 'student', 'content': 'What innervates the deltoid?'}]

# Should PASS — good Socratic question
good = 'What do you recall about which nerve roots contribute to shoulder movement?'
r = d._quality_check_call(state, good)
print('PASS result:', r)
assert r['pass'] == True

# Should FAIL — reveals answer
bad = 'The axillary nerve (C5-C6) innervates the deltoid. Does that help?'
r2 = d._quality_check_call(state, bad)
print('FAIL result:', r2)
assert r2['pass'] == False
assert r2['leak_detected'] == True
print('_quality_check_call OK')
"
```

### 2e. Memory (requires Qdrant running)

```bash
# Start Qdrant first (pinned Docker image): ./scripts/qdrant_up.sh

python -c "
from memory.memory_manager import MemoryManager

m = MemoryManager()

# flush a session summary
from conversation.state import initial_state
from config import cfg
state = initial_state('test_student_mem', cfg)
state['messages'] = [{'role': 'student', 'content': 'Deltoid innervation'}]
state['student_reached_answer'] = False
state['weak_topics'] = [{'topic': 'Deltoid Innervation', 'difficulty': 'moderate', 'failure_count': 1}]

m.flush('test_student_mem', state)
print('flush OK')

# load — should return weak topic
topics = m.load('test_student_mem')
print('load result:', topics)
"
```

If Qdrant is not running, both calls should silently return `[]` / no-op without crashing.

---

## Layer 3 — Integration tests (real LLM, full graph, requires `ANTHROPIC_API_KEY`)

```bash
pytest tests/test_conversation.py -v -m integration
```

**What these test:**
- Graph compiles and rapport node runs
- First student question → `locked_answer` is set, `student_state` is classified
- Every tutor response ends with `?`
- `state["debug"]["api_calls"]` increments each turn

**Pass criteria:** All 4 integration tests green.

---

## Layer 4 — Scenario tests (multi-turn, requires `ANTHROPIC_API_KEY`)

```bash
pytest tests/test_conversation.py -v -m scenarios
```

**What these test (in order of importance):**

| Test | Scenario | Pass condition |
|------|----------|---------------|
| `test_happy_path` | Correct answer on turn 2 | `student_reached_answer=True` or phase=assessment |
| `test_hints_exhausted` | 3 wrong answers | `hint_level > 1` or phase=assessment |
| `test_help_abuse_flow` | 3× "I don't know" | `hint_level >= 2` or phase=assessment |
| `test_hard_turn_rule` | First 2 turns | `locked_answer` never appears in tutor response |
| `test_turn_limit` | Force turn_count=24, max_turns=25 | phase=assessment after next invoke |
| `test_partial_correct_flow` | Partial answer | Session does NOT immediately end |
| `test_memory_flush` | assessment_turn=2 | phase=memory_update after invoke |

---

## Layer 5 — Manual UI verification (requires `ANTHROPIC_API_KEY`)

```bash
streamlit run ui/app.py
```

Go through this checklist manually:

- [ ] Rapport greeting appears on load (no student message needed)
- [ ] Student profile dropdown changes work
- [ ] Type a question → tutor responds with a question (never a direct answer)
- [ ] `locked_answer` appears in debug sidebar after first question
- [ ] Quick-response buttons work (Correct / Wrong / Don't know / Off-topic)
- [ ] `help_abuse_count` increments on "I don't know" button presses (debug panel)
- [ ] After 3× "I don't know" → hint level advances (debug panel shows `hint: 2/3`)
- [ ] Enable debug mode → two-column layout appears
- [ ] Dean stream (right column) shows wrappers fired, tool calls, PASS/FAIL
- [ ] Correct answer → assessment phase fires → clinical question sent
- [ ] After clinical answer → mastery summary sent
- [ ] Session ends → "Session complete!" message appears
- [ ] New Session button resets everything

---

## Layer 6 — Simulation smoke test (requires `ANTHROPIC_API_KEY`)

```bash
# Step 1: Run 10 conversations with weakest profile (S3 exercises full hint + reveal path)
python -m simulation.runner --profile S3 --n 10

# Step 2: Verify output
python -c "
from simulation.logger import load_conversations
convs = load_conversations()
print(f'{len(convs)} conversations logged')
assert len(convs) > 0
for c in convs:
    assert 'conv_id' in c
    assert 'turns' in c
    assert 'outcome' in c
    assert 'reached_answer' in c['outcome']
    assert 'turns_taken' in c['outcome']
print('JSONL structure OK')
"

# Step 3: Score with EULER
python -m evaluation.euler --all
```

**Pass criteria:**
- At least 8/10 conversations complete without exception
- Each has `turns`, `outcome.reached_answer`, `outcome.turns_taken` populated
- EULER average > 0.70

---

## Layer 7 — Full smoke test (50 convos)

```bash
python -m simulation.runner --profile S3 --n 50
python -m evaluation.euler --all
```

**Pass criteria:** 80%+ complete, EULER > 0.70

---

## What is NOT tested here (RAG — teammate's responsibility)

- `ingestion/` pipeline
- `retrieval/retriever.py` `Retriever` class (real hybrid search)
- `tests/test_rag.py`
- `tests/test_ingestion.py`
- `data/textbook_structure.json` parsing (covered by `_load_topics()` fallback until file exists)
