# Conversation Flow Fixes — React UI + Topic Lock Guard

Date: 2026-04-19 13:33:13

## Reported issues
1. Duplicate opener/cards at session start.
2. Topic cards generated from low-signal input (e.g., `hi`) leading to nonsense options.
3. Premature transition to clinical after shallow topic exchange.
4. Remaining hardcoded OT phrasing in fallback topic options.

## Root causes found
- React dev `StrictMode` in `frontend/src/main.tsx` was triggering duplicate mount/effect behavior, creating duplicated start interactions.
- Dean topic gate accepted low-signal and overly-generic custom text too early, then treated it as a lock candidate.
- Generic terms (e.g., `bone`) could lock as custom topic, allowing trivial follow-up to be classified as core success.
- Teacher topic-option fallback still had literal `OT` in one option template.

## Changes applied
- Removed `React.StrictMode` wrapper in `frontend/src/main.tsx` to stop duplicate mount-driven startup interactions in dev.
- In `conversation/dean.py`:
  - Extended low-effort topic detection to include greetings (`hi`, `hello`, `hey`, etc.).
  - Added `_is_generic_topic_reply(...)` guard.
  - Added early reprompt path for low-signal first topic input (no cards shown for greetings).
  - Blocked generic custom topic lock; requires either card selection or sufficiently specific custom topic.
  - Improved retrieval ambiguity check to allow specific 1-2 word entity queries (e.g., `humerus`, `axillary nerve`).
- In `conversation/teacher.py`:
  - Replaced hardcoded `OT relevance` fallback with domain-aware `{domain_short}` phrasing.

## Validation
- Python compile check: PASS (`conversation/dean.py`, `conversation/teacher.py`).
- Frontend production build: PASS (`npm run build`).

## Expected behavior now
- No duplicated opening messages/cards on first load.
- `hi` no longer generates topic-option cards.
- Generic replies like `bone` no longer prematurely lock the topic.
- No immediate jump to clinical from shallow topic locking.
