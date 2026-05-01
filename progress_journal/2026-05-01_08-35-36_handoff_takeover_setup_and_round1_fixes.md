# 2026-05-01 — Nidhi takeover: clean-clone setup, Bedrock runtime, round-1 tutoring fixes

Owner: Nidhi
Branch: `nidhi/reach-gate-and-override-analysis`
Spans: 2026-04-30 evening → 2026-05-01 morning
Companion to: `docs/HANDOFF_NIDHI.md`, `docs/SETUP_NOTES_NIDHI_2026-04-30.md`,
`docs/SESSION_JOURNAL_2026-04-30.md`.

This entry covers the takeover handoff, six clean-clone setup bugs found and
fixed, the Anthropic-Direct → AWS Bedrock runtime swap, and the first round
of conversation-quality fixes (anchor hardening, hint-3 leak prevention,
sample-related cards, WebSocket zombie reconnect, mastery scope, edges
revert, memory wipe).

---

## Part 1 — Clean-clone setup (six bugs)

Full details in `docs/SETUP_NOTES_NIDHI_2026-04-30.md`. Summary:

| # | File | Symptom | Fix |
|---|------|---------|-----|
| 1 | `requirements.txt` | `pip install` ResolutionImpossible: `llama-index 0.12` ↔ `llama-index-core>=0.14.21` conflict; cascading `openai==1.76.0` blocked `llama-index-llms-openai>=0.7` (needs `openai>=1.108.1`); pip 26.1 `resolution-too-deep` | Bumped `llama-index>=0.14.21`, `openai>=1.108.1`. Downgrade pip to 24.3.1 in venv bootstrap. |
| 2 | `config/base.yaml` | `FileNotFoundError: bm25_openstax_anatomy.pkl` — HF dataset publishes `bm25_chunks_openstax_anatomy.pkl` post chunks-only flip | Updated `paths.bm25_openstax_anatomy` to `bm25_chunks_openstax_anatomy.pkl`. |
| 3 | `backend/dependencies.py` | **(silent)** Backend boots, retrieval returns 0 chunks every turn (legacy `Retriever` reads non-existent `parent_chunk_id` payload field on chunks-only Qdrant collection). Eval would log every session as ungrounded. | `Retriever()` → `ChunkRetriever()`. |
| 4 | `data/indexes/qdrant_sokratic_kb_chunks.snapshot` | Snapshot restore fails (RocksDB version mismatch, snapshot generated on Qdrant > 1.13.4 pinned in `qdrant_up.sh`) | Path-B rebuild: `python scripts/reindex_chunks.py --collection sokratic_kb_chunks --fresh`. ~10 min, ~$0.10 OpenAI (text-embedding-3-large × 7,574 chunks). |
| 5 | `backend/main.py` | **(silent)** Backend boots, `/health` ok, frontend connects, first `graph.invoke()` throws `Could not resolve authentication method`. uvicorn doesn't auto-load `.env`; FastAPI entrypoint never called `load_dotenv`. UI shows "Unable to start session." | `load_dotenv(Path(__file__).parent.parent / ".env", override=True)` before any module imports that construct LLM clients. **`override=True` is required** — empty `ANTHROPIC_API_KEY=""` exported from shell profiles silently wins over `.env` otherwise. |
| 6 | `README.md` smoke-test | `curl http://localhost:8000/api/health` 404 (route is `/health`, no `/api` prefix) | Doc-only fix. |

Plus: `scripts/validate_topic_index.py` was using legacy `Retriever()` and
not loading `.env` — both fixed (load `.env` early, swap to `ChunkRetriever`).
Once green, ran the validator to backfill the `teachable` flag and discovered
all 363 entries in `data/topic_index.json` had `chapter_num: null`. Backfilled
from the `Chapter X:` prefix of the section path. 355/363 remain teachable
(below `chunk_count` floor on 8).

**Verification command** (post-fix, end-to-end smoke):

```bash
.venv/bin/python -c "
from retrieval.retriever import ChunkRetriever
r = ChunkRetriever()
hits = r.retrieve('What is the rotator cuff?', domain='openstax_anatomy')
print(f'{len(hits)} chunks, top: {hits[0][\"chapter_title\"]} / {hits[0][\"section_title\"]}')
"
# 11 chunks, top: Joints / Anatomy of Selected Synovial Joints
```

---

## Part 2 — Bedrock + Sonnet 4.6 runtime swap

### Why

- Anthropic-Direct credits exhausted on the takeover account.
- AWS account provisioned with $100 in Sonnet 4.6 Bedrock credits.
- Per-session math (uncached, Sonnet 4.6 across all roles): ~$1.80/session.
  $100 budget = ~55 sessions. Below the 50-conversation eval (Task 2) +
  10 production sessions + 40-test edge-case driver target.
- Bedrock prompt caching can take that to ~$0.80/session (~55% off) — enabled
  separately, not in this round.

### Implementation

New `conversation/llm_client.py` — single source of truth:

- `make_anthropic_client()` returns `AnthropicBedrock` when
  `SOKRATIC_USE_BEDROCK=1`, else `Anthropic` (Direct). Async variant
  (`make_async_anthropic_client`) for ingestion paths.
- `resolve_model(name)` rewrites short names (`claude-sonnet-4-6`) to
  Bedrock cross-region inference profile IDs (`us.anthropic.claude-sonnet-4-6`).
  Pass-through for already-prefixed IDs and on Direct.
- `beta_headers()` returns `{}` on Bedrock (Bedrock 400s on
  `anthropic-beta: prompt-caching-2024-07-31`), the explicit caching
  header on Direct.
- `_BEDROCK_MODEL_MAP` covers Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.5,
  Opus 4.6, Opus 4.7. Verified 2026-05-01 via `boto3 list_foundation_models`
  + direct `messages.create()` invocations in `us-east-1`.

Wired through every module that instantiates an Anthropic client:

- `conversation/dean.py` — `DeanAgent.__init__`, `_timed_create()` calls
  `beta_headers()`.
- `conversation/teacher.py` — same, both `messages.create` sites.
- `conversation/nodes.py` — mastery scoring path.
- `conversation/summarizer.py` — old-turn summarizer.
- `evaluation/quality/llm_judges.py` — eval framework.
- `retrieval/retriever.py` — HyDE rewrite.

`config/base.yaml` — every `models.<role>` set to `claude-sonnet-4-6`.

`.env` — `SOKRATIC_USE_BEDROCK=1` + AWS access keys (not committed).

### What this changes vs Direct

- **Architecture: nothing.** Same LangGraph flow, same LLM I/O contract,
  same temp=0 determinism.
- **Quality: nothing.** Same model weights served via Bedrock as via Direct.
- **Cost: ~5–10% higher per token** without caching, but unlocks the
  $100 AWS credit pool. Caching round (next session) drops the per-session
  number well below Direct.

---

## Part 3 — Round-1 conversation-quality fixes

Diagnosed from a series of real session transcripts (cardiac, antibody,
CNS / brain, cerebrum). Five clusters identified; this round shipped
fixes against Clusters 1–4 plus several UX bugs. The fifth (Sonnet
sycophancy patterns + multi-anchor reach) is queued for the next round.

### Cluster 1 — Anchor hardening (`_is_distinctive_anchor`)

**Symptom.** Multi-component locked answers (`"left and right coronary
arteries"`) broke the reach gate's token-overlap step (Step A) because
the gate required all anchor tokens overlap with student input. Single
strong matches like "LCA" or "left coronary" were rejected. Earlier
attempt to fix by `len(locked.split()) >= 2` filter inadvertently
excluded ALL single-word anchors — so legitimate single-word answers
(`"nucleus"`, `"pepsin"`) lost their guard rails too.

**Fix.** New module-level helper `_is_distinctive_anchor(token)` in
`conversation/dean.py`:

- ≥5 chars OR multi-word
- Not in `_COMMON_ANCHOR_FALSE_POSITIVES` (muscle, nerve, vein, bone,
  artery, organ, tissue, system, …) — these are common nouns the
  teacher uses naturally during scaffolding and would false-positive
  every leak check.

Used in three places: `_lock_anchors_call` post-validator, `_build_forbidden_tokens`,
and `_deterministic_assessment_check`.

`_lock_anchors_call` repair prompt also tightened with an explicit
**umbrella + per-component aliases** worked example
("two coronary arteries" → `aliases: ["LCA", "left coronary artery",
"RCA", "right coronary artery"]`) and a deterministic split-on-`and`
fallback for when the LLM repair returns the same broken structure.

### Cluster 2 — Forbidden tokens upstream + deterministic post-filter

**Symptom.** Teacher would draft a Hint-3 message that named the answer
("the SA node, also called the sinoatrial node…"); Dean's QC would
catch it and rewrite, but the user saw both versions because the teacher
streams BEFORE the QC fires. Observed dean override rate ~90% in some
sessions.

**Fix (3-layer).**

1. **Upstream (teacher prompt).** New `_build_forbidden_tokens(state)` in
   `conversation/teacher.py` — combines `locked_answer` + aliases + top
   distinctive nouns from `full_answer` into a comma-separated list
   injected into both `draft_socratic` and `draft_clinical` prompts.
   Teacher is now told explicitly what NOT to say upstream rather than
   relying on the dean's post-hoc scrub.
2. **Deterministic post-filter (assessment phase).** New
   `DeanAgent._deterministic_assessment_check(state, draft)` — runs
   regex against `draft` for: forbidden tokens, letter-hint patterns,
   chunk citations. Returns `{"verdict": "ship" | "reject", "reason": ...}`.
   `conversation/nodes.py` clinical-followup path now runs this check
   on `feedback_message` BEFORE shipping; rejects fall back to
   `_assessment_clinical_followup_fallback`.
3. **Tighter LLM QC.** `dean_quality_check_assessment_*` prompts got
   "no leak", "no chunk citation", scope rules. `dean_clinical_turn_*`
   got the same.

### Cluster 3 — Hint-3 letter-hint / morphology / etymology / blank-completion

**Symptom.** Hint-3 framed as "be more direct without naming the answer"
created strong loophole. Real observations:
- 2026-05-01 CNS: "The textbook uses a word that starts with 'n'"
- 2026-05-01 Cerebrum: "what suffix completes 'comm-____'?"
- 2026-05-01 Cerebrum: "comm-_______?"
- 2026-05-01 Cerebrum: "from a Latin root meaning together"

All are covert reveals — they hand the student the term's morphology
without naming it.

**Fix.** New `_has_letter_hint(text)` in `conversation/dean.py` — extensive
regex array covering: starts-with-letter, ends-with-letter, blank-completion
(`comm-____?`), suffix/prefix prompting, "Latin/Greek root meaning X",
"common English word for X", "everyday word for X", MCQ patterns
(`A) X B) Y`), single-letter quiz hints. Added to dean QC pipeline.

**Root cause discovery.** The `dean_hint_plan_*` prompts that govern
Hint-1/2/3 framing only existed in a dead **root-level `config.yaml`**
that's never loaded — the live `config/base.yaml` had no hint-plan
prompts. Hint-3 had been running on accidentally-empty guidance.
Migrated `dean_hint_plan_static`, `_delta`, `_dynamic` from the dead
file with explicit forbidden patterns + a positive example showing how
Hint-3 should still scaffold without revealing.

`teacher_socratic_static` and `_delta` Hint-3 rule expanded with the
enumerated forbidden patterns above.

### Cluster 3a — Edges revert (hint-exhaustion routing)

**Earlier attempted change.** In `conversation/edges.py`, removed the
`hint_level > max_hints → assessment` route, hoping tutoring would
continue past hint-3.

**Why it failed.** `DeanAgent` early-exits at hint exhaustion (`dean.py`
~line 1792) and skips Teacher draft entirely. With the route removed,
the graph looped back to dean_node with no message generated → infinite
loop with no UX update.

**Fix.** REVERTED. Hint exhaustion routes to `assessment_node` again.
The architecture assumes hint-exhaustion = session-end trigger; honour
that. Doc-comment in `edges.py` now records the reasoning so future
attempts don't fall into the same trap.

### Cluster 4 — `sample_related` (semantic cards)

**Symptom.** Student types "brain" → topic-card surfacing returned
random teachable topics (`sample_diverse(3)`) — DNA Replication,
Compensation Mechanisms — instead of brain-related ones.

**Fix.** New `TopicMatcher.sample_related(retriever, query, n, …)` in
`retrieval/topic_matcher.py`:

- Uses the live retriever to fetch top-12 chunks for the query.
- Votes chunks onto `(chapter_num, section_title, subsection_title)`,
  weighted `1/(rank+1)`.
- Walks the index for teachable subsections matching top-voted keys.
- Tops up from `sample_diverse` if vote pool is sparse — guarantees N
  cards.

Required `chapter_num` on every entry — driven the `topic_index.json`
backfill noted above.

`_coverage_gate(state, retriever=None)` updated to accept the retriever
and call `sample_related` for the card-pick path.

### Cluster 5 — Closure rules (anchor reveal at session-end)

**Symptom.** When student exhausts hints with 4× IDK, tutor refused to
state the answer at close — defeats the pedagogical purpose. The
over-blocking came from `_close_session_call` running the same leak
filter as mid-session.

**Fix.** Closure ALLOWS anchor mentions — proper pedagogy at session-end
is to state the answer + the reasoning the student missed. Closure
still BLOCKS chunk citations. `dean_close_session_static` got an
explicit CLOSURE RULES block.

`mastery_scorer_static` got a SCOPE RULE: scorer only judges the
student against `locked_answer` + aliases + the in-scope `full_answer`,
not against tutor-introduced sub-branches (P0-G fix). `mastery_store.py`
now passes `locked_answer_aliases`, `full_answer`, and a redacted
`clinical_history` block into the scorer prompt.

### UX bug — Frontend WebSocket zombie reconnect

**Symptom.** Click "+ New Chat" → backend opens a fresh thread, but the
old WebSocket's `onclose` still fires `setTimeout(() => connect(), 1200)`
with a stale-closure threadId. Result: two WebSockets, the new one
connected to the new session, the old one reconnected to the dead
thread now in `phase=memory_update`. Student messages route to the
zombie → frontend shows immediate "Session complete." Memory then never
saves because the live session never reaches `memory_update_node`.

**Fix.** `frontend/src/hooks/useWebSocket.ts`:

- `disposedRef` flag set true in cleanup, false on each fresh effect run.
- ws-instance identity check in `onclose` — `if (wsRef.current !== ws) return;`
  — using ws identity (not just the boolean) avoids the race where the
  fresh effect run resets the flag before the old `onclose` fires.
- Null out `onclose`, `onmessage`, `onerror` BEFORE calling `old.close()`
  in cleanup. Belt-and-suspenders against any handler firing post-cleanup.

### Memory wipe

Earlier session's mem0 fact-extraction had corrupted the `nidhi` user's
`mem0` state with fragmented anchor pieces ("left", "anterior", …) —
caused subsequent sessions to fast-close on rapport because mem0
matched current locks against fragmentary memories. Wiped mem0 user
state. Root cause (mem0 fragmentation + WS race) both fixed.

---

## What's NOT in this round (queued for next session)

| # | Item | Notes |
|---|------|-------|
| 1 | Letter-hint regex extensions | Two-letter abbreviations ("Fc"), "X stands for Y", "first letters of …" patterns. Adds 4–6 patterns to `_has_letter_hint`. |
| 2 | `_is_distinctive_anchor` for short ALL-CAPS abbreviations | Current ≥5-char rule misses `Fc`, `SA`, `RCA`, `LCA`, `ATP`. Add ALL-CAPS-with-≥2-chars branch. |
| 3 | `sample_related` cold-start failure for "coronary circulation" | Returns random despite query matching the corpus. Investigate: voting key mismatch? min_chunk_count too high? |
| 4 | Sonnet-specific sycophancy patterns | "On an interesting track", "partly right", "both key concepts in hand" — Haiku didn't produce these; Sonnet does. Extend dean QC's sycophancy regex. |
| 5 | Multi-anchor reach (K of N) | Student gets LCA correct but not RCA → currently full reach=true → mastery overstated. Needs K-of-N reach scoring at gate level. Schema work — multi-anchor was deferred from the original handoff (P1-A). |
| 6 | Bedrock prompt caching enablement | `cachePoint` markers on system prompts + retrieved-chunks blocks. ~55% per-session savings. ~2 hr implementation. |
| 7 | 40-case end-to-end test driver | Visible in UI as `student_id="test_<n>"`. Runs against Anthropic-Direct + Haiku to preserve AWS credits. |
| 8 | Dimensional hints (Change 6 from prior journal) | Still deferred — invasive, not on the critical path before paper deadline. |

---

## Cost ledger update

| Phase | Spend |
|---|---|
| Prior journal — through Phase 1 | ~$1.50 |
| Setup walkthrough (Anthropic Direct) | ~$0.40 (mostly debug calls) |
| Qdrant rebuild via OpenAI embeddings | ~$0.10 |
| Round-1 fixes (debug sessions on Direct + Haiku) | ~$1.20 |
| **Cumulative on Anthropic Direct** | **~$3.20** |
| AWS Bedrock + Sonnet (post-swap, ad-hoc test sessions) | ~$2.00 |
| **AWS budget remaining** | **~$98 / $100** |

---

## Files touched this round

```
backend/dependencies.py                  +1 −1   (Retriever → ChunkRetriever)
backend/main.py                          +5      (load_dotenv early, override=True)
config/base.yaml                         +413 −83 (hint-plan migration, leak rules,
                                                  closure rules, mastery scope, all
                                                  models → claude-sonnet-4-6)
conversation/dean.py                     +650 −90 (anchor helpers, letter-hint regex,
                                                  deterministic assessment check,
                                                  Bedrock client, lock-anchor repair)
conversation/edges.py                    +13 −3  (hint-exhaustion routing — REVERT)
conversation/llm_client.py               +97     (NEW — Bedrock factory)
conversation/nodes.py                    +25 −4  (assessment leak filter, Bedrock)
conversation/summarizer.py               +5 −1   (Bedrock client)
conversation/teacher.py                  +60 −4  (forbidden_tokens, Bedrock)
data/topic_index.json                    +1452 −0 (chapter_num backfill)
docs/SETUP_NOTES_NIDHI_2026-04-30.md     +297    (NEW — clean-clone bugbook)
evaluation/quality/llm_judges.py         +18 −0  (Bedrock)
frontend/package-lock.json               −11     (incidental)
frontend/src/hooks/useWebSocket.ts       +44 −3  (zombie reconnect fix)
memory/mastery_store.py                  +26     (scope variables to scorer)
requirements.txt                         +2 −2   (llama-index, openai bumps)
retrieval/retriever.py                   +4 −1   (HyDE → Bedrock)
retrieval/topic_matcher.py               +83     (sample_related)
scripts/validate_topic_index.py          +6 −2   (.env load, ChunkRetriever)
test.md                                  +540    (NEW — edge-case test catalogue,
                                                  driver pending)
```
