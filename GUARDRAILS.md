# Socratic-OT: Guardrails

> This document lists every guardrail in the system — what it catches, how it works, and where it runs.

---

## What is a Guardrail Here?

A guardrail is any check that sits between an LLM output and the student, or between the system and a wrong/unsafe state. Some are simple string checks. Some are embedding comparisons. Some are GPT-4o reasoning checks. All of them run before the student ever sees a response.

---

## Guardrail 1 — Answer Leak: Exact Match

**What it catches:** Teacher directly writes the locked answer word in its response.
Example: *"Think about the axillary nerve..."*

**How it works:**
```python
locked_answer.lower() in response.lower()
```

**Speed:** < 1ms
**Runs in:** `tools/mcp_tools.py → flag_answer_leak()` — Level 1
**On trigger:** Response sent back to Teacher with critique. Counted as one retry.

---

## Guardrail 2 — Answer Leak: Semantic Match

**What it catches:** Teacher uses a synonym or paraphrase of the locked answer.
Example: *"Consider the nerve named after the armpit region..."* (when answer is "axillary nerve")

**How it works:**
- Embed Teacher's response with `text-embedding-3-small`
- Embed `locked_answer` with `text-embedding-3-small`
- Compute cosine similarity
- If similarity > 0.85 → flagged

**Speed:** ~50ms (two embedding calls)
**Runs in:** `tools/mcp_tools.py → flag_answer_leak()` — Level 2
**On trigger:** Response sent back to Teacher with critique. Counted as one retry.

---

## Guardrail 3 — Answer Leak: Entailment Check

**What it catches:** Teacher's response logically implies the answer even though no similar words are used.
Example: *"Think about the nerve that exits the posterior cord, travels through the quadrilateral space, and wraps around the surgical neck of the humerus..."* — no synonym, but any student following this description would immediately arrive at "axillary nerve."

**How it works:**
Dean's GPT-4o evaluation prompt includes:
> *"Could a student who read only this response — with no other context — immediately identify that the answer is '{locked_answer}'? Answer yes or no."*

This is embedded inside Dean's normal evaluation call — not a separate API call.

**Speed:** Part of Dean's evaluation (~500ms total, no added cost)
**Runs in:** `conversation/dean.py → run_turn()` — Level 3 of LeakGuard
**On trigger:** Response sent back to Teacher with critique. Counted as one retry.

---

## Guardrail 4 — Hard Turn Rule (No Answer Before Turn 3)

**What it catches:** Any response — even a passing LeakGuard — that goes to the student in turns 1 or 2.

The course requirement states: *"The bot is strictly forbidden from providing a direct definition or answer in the first two turns."*

**How it works:**
```python
if state["turn_count"] < 3:
    return FAIL  # always, regardless of LeakGuard result
```

This is a hardcoded check, not a configurable threshold. It runs after LeakGuard and after quality checks, and overrides a PASS if turn_count < 3.

**Speed:** < 1ms
**Runs in:** `conversation/dean.py → run_turn()`
**On trigger:** Response sent back to Teacher. Turn count does not increment until a response actually reaches the student.

---

## Guardrail 5 — Sycophancy Guard

**What it catches:** Teacher validating a wrong student claim.
Example: Student says "It's the radial nerve, right?" — Teacher responds "Yes, exactly!" — but the locked answer is "axillary nerve."

This is dangerous because it reinforces incorrect learning.

**How it works:**
- Dean calls `check_student_answer(student_claim, locked_answer)`
- Returns `{correct: bool, similarity: float}`
- If `correct = False` AND Teacher's draft contains affirming language ("yes", "correct", "exactly", "that's right", "good job") → flagged
- Dean's GPT-4o evaluation also reasons about this explicitly:
  > *"Does this response validate or agree with the student's claim? The student's claim is incorrect. Flag if yes."*

**Speed:** One embedding comparison (~50ms) + part of Dean's GPT-4o call
**Runs in:** `tools/mcp_tools.py → check_student_answer()` + `conversation/dean.py`
**On trigger:** Response sent back to Teacher with critique: "The student's claim is wrong. Do not validate it."

---

## Guardrail 6 — Out-of-Scope Guardrail

**What it catches:** Student asks a question that has no relevant answer in the knowledge base.
Example: "What is the capital of France?" or a question about a topic not in OpenStax A&P.

**How it works:**
- After cross-encoder reranking, check the max reranker score
- If `max_score < out_of_scope_threshold (0.3)` → no chunks returned
- When retrieved_chunks is empty, Dean responds with a polite redirect:
  > *"That topic isn't covered in your OT textbook. Let's stick to the anatomy we're studying."*

**Speed:** Part of retrieval pipeline (~100ms total)
**Runs in:** `retrieval/retriever.py → retrieve()`
**On trigger:** Dean produces the out-of-scope response directly. Teacher is not called.

---

## Guardrail 7 — No Question Present

**What it catches:** Teacher produces a statement instead of a question.
Example: *"The deltoid is innervated by the axillary nerve."* — this is a fact delivery, not Socratic tutoring.

**How it works:**
Dean checks Teacher's draft:
```python
"?" not in response
```
Also checked by Dean's GPT-4o evaluation:
> *"Does this response end with or contain a question? Answer yes or no."*

Simple string check runs first. GPT-4o check catches cases like implicit questions without a "?".

**Speed:** < 1ms (string check) + part of Dean's GPT-4o call
**Runs in:** `conversation/dean.py → run_turn()`
**On trigger:** Response sent back to Teacher with critique: "You must end your response with a question."

---

## Guardrail 8 — Dean Retry Limit (Dean as Final Fallback)

**What it catches:** Teacher repeatedly failing all guardrails (any combination of the above).

**How it works:**
- `dean_retry_count` tracks how many times Teacher has been sent back this turn
- If `dean_retry_count >= max_teacher_retries (2)` → Dean writes the student response directly
- Dean already has all the context (retrieved_chunks, locked_answer, conversation history)
- Dean's response is still Socratic and still goes through LeakGuard levels 1–3 and the hard turn rule before reaching the student

**Speed:** One GPT-4o call for Dean's response
**Runs in:** `conversation/dean.py → run_turn()`
**On trigger:** Dean bypasses Teacher entirely for this turn. dean_retry_count resets to 0 on next student message.

---

## Guardrail 9 — Multimodal Non-Anatomy Rejection

**What it catches:** Student uploads an image that is not an anatomical diagram.
Example: a photo of food, a landscape, a screenshot.

**How it works:**
- GPT-4o Vision attempts to identify anatomical structures
- If no structures are identified with confidence > 0.5 → `image_structures` is empty
- Retrieval returns no results (no query to run)
- Out-of-scope guardrail fires (Guardrail 6)

**Speed:** One GPT-4o Vision call (~1–2s)
**Runs in:** `multimodal/image_processor.py`
**On trigger:** Dean produces: *"I can only help with anatomical diagrams from your OT coursework."*

---

## Summary Table

| # | Guardrail | Method | Speed | Handles |
|---|-----------|--------|-------|---------|
| 1 | Answer leak — exact | String match | < 1ms | Direct word use |
| 2 | Answer leak — semantic | Cosine similarity (embeddings) | ~50ms | Synonyms, paraphrases |
| 3 | Answer leak — entailment | GPT-4o reasoning (in Dean's call) | No extra cost | Implicit logical reveals |
| 4 | Hard turn rule | Hardcoded turn_count check | < 1ms | Answer before turn 3 |
| 5 | Sycophancy | Embedding similarity + GPT-4o | ~50ms | Validating wrong claims |
| 6 | Out-of-scope | Cross-encoder score threshold | Part of retrieval | Off-topic questions |
| 7 | No question | String check + GPT-4o | < 1ms + no extra cost | Statement instead of question |
| 8 | Dean retry limit | Counter check | < 1ms | Repeated Teacher failures |
| 9 | Non-anatomy image | GPT-4o Vision + empty retrieval | ~1–2s | Irrelevant image uploads |

---

## What is NOT a Guardrail Here

- **EULER evaluation** — this is an offline measurement tool, not a live guardrail. It scores conversation quality after the fact.
- **RAGAS faithfulness** — also offline. Measures whether hints are grounded in retrieved chunks.
- **Conversation summarizer** — manages context window length, not safety.
- **Memory manager** — tracks weak topics, not a safety check.
