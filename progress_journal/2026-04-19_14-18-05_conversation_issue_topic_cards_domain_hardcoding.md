# Conversation Issue Log — Topic Card Misrouting + Domain Hardcoding + Weak Topic Persistence

Date: 2026-04-19

## Reported Symptoms
- Duplicate opening tutor cards/messages at session start.
- Greeting input like `hi` generated topic cards with prefixed options (`hi: ...`).
- Flow occasionally advanced too early toward clinical/assessment.
- User-facing copy still contained OT-specific language even after domain-generalization intent.
- User asked whether weak topics are currently being stored.

## Root Causes Identified
1. Domain defaults and many prompt strings were still OT-specific in config/code.
2. Topic low-effort detection missed punctuation variants (e.g., `hi.`), allowing greeting text to pass into topic-option generation.
3. Frontend bootstrap/session init could be invoked in edge cases more than once; no local dedupe for repeated tutor message content.
4. Weak-topic persistence is conditional on mem0/Qdrant availability at flush time.

## Changes Applied
### Backend / Conversation
- `conversation/dean.py`
  - Made domain defaults generic (`human anatomy` / `anatomy` / `student`).
  - Added `_apply_domain_vars()` and applied domain placeholder rendering inside cached prompt blocks.
  - Improved `_normalize_text()` to strip punctuation for robust low-effort checks.
  - Strengthened `_is_low_effort_topic_reply()` to catch greeting variants (`hi there`, `hello.` etc.).
  - Replaced OT-specific fallback clinical prompt strings with `{domain_short}` / `{student_descriptor}` placeholders.

- `conversation/teacher.py`
  - Made domain defaults generic.
  - Added `_apply_domain_vars()` and applied placeholder rendering for role base/wrapper/chunks/turn deltas before API call.

- `config.yaml`
  - Domain defaults switched from OT-specific to generic anatomy values:
    - `domain.name = "human anatomy"`
    - `domain.short = "anatomy"`
    - `domain.student_descriptor = "student"`
    - `domain.mem0_namespace = "anatomy"`
    - `domain.retrieval_domain = "anatomy"`
  - Replaced OT-specific tutor wording in prompts with domain-neutral/domain-placeholder wording.

### Frontend
- `frontend/src/stores/sessionStore.ts`
  - Added dedupe guard to `addTutorMessage()` to avoid appending identical consecutive tutor messages.

- `frontend/src/hooks/useSession.ts`
  - Reworked bootstrap guard to prevent duplicate session-start calls and make new-session bootstrap deterministic.

## Validation
- Python compile: `conversation/dean.py`, `conversation/teacher.py` passed.
- Frontend build: `npm run build` passed.

## Clarification: Weak Topics Persistence
- Weak topics are updated in-session from assessment outcomes.
- Cross-session persistence occurs only when `memory_manager.flush()` succeeds.
- If mem0/Qdrant is unavailable, flush is skipped and weak topics will not be persisted for next session.

## Remaining Follow-up
- Re-test end-to-end with intentionally disengaged first input (`hi`, `hey`, `ok`) to confirm no topic cards are generated from greeting text.
- Verify no OT-specific surface text remains in the React flow during a clean session run.
