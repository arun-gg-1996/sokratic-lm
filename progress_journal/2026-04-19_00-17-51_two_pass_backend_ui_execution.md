# Two-Pass Execution Log (No PR Split)

Date: 2026-04-19
Scope: Execute requested plan in two passes within one code path.

## Pass 1 (Backend / Orchestration / Domain plumbing)

### 1) Hardcoded audit added
- Added: `docs/hardcoded_audit.md`
- Captures key constants and dispositions (KEEP / REPLACE-LLM / REPLACE-UI / CONFIG).

### 2) Removed heuristic confidence layer
- File: `conversation/dean.py`
- Removed:
  - `_UNCERTAINTY_STRONG_MARKERS`
  - `_UNCERTAINTY_WEAK_MARKERS`
  - `_CONFIDENCE_MARKERS`
  - `_heuristic_confidence`
  - blend logic `(0.7 * model) + (0.3 * heuristic)` and uncertainty cap behavior
- New behavior:
  - Uses model confidence directly when present.
  - Deterministic state-based fallback only when confidence missing/invalid.

### 3) Removed domain-specific guess list
- File: `conversation/dean.py`
- Removed `_COMMON_NERVE_GUESSES`.
- `_contains_explicit_wrong_nerve_guess` now uses generic regex extraction of `<...> nerve` phrases.

### 4) Moved repetition threshold to config
- File: `config.yaml`
  - Added `thresholds.repetition_similarity: 0.90`
- File: `conversation/dean.py`
  - `_is_repetitive_question` call now reads threshold from config.

### 5) Batched close-session path added
- File: `conversation/dean.py`
  - Added `_close_session_call(state)` returning JSON payload:
    - `core_mastery_tier`
    - `clinical_mastery_tier`
    - `mastery_tier`
    - `grading_rationale`
    - `student_facing_message`
    - `memory_summary`
  - Added deterministic fallback `_close_session_fallback_payload(...)`.
- File: `conversation/nodes.py`
  - Added `_close_session_with_dean(...)` helper.
  - Rewired all assessment closeout branches to use this single batched path.
  - Removed rule-based tier calculators from nodes (`_tier_from_score`, `_compute_mastery_tiers`).
  - `memory_update_node` now flushes `state["session_memory_summary"]` (from batched closeout).

### 6) Domain plumbing added
- File: `config.yaml`
  - Added `domain` section:
    - `name`, `short`, `student_descriptor`, `kb_collection`, `memory_collection`, `mem0_namespace`, `retrieval_domain`
- File: `config.py`
  - Added `Config.domain` loading and reload support.
- File: `retrieval/retriever.py`
  - Uses `cfg.domain.kb_collection` when present.
  - `retrieve()` defaults to `cfg.domain.retrieval_domain`.
- File: `memory/persistent_memory.py`
  - Uses namespaced user IDs: `<mem0_namespace>:<student_id>`.
  - Uses `cfg.domain.memory_collection` when present.

### 7) Prompt-domain format compatibility
- Files: `conversation/teacher.py`, `conversation/dean.py`
- Added `_domain_prompt_vars()` helper and passed domain vars into prompt `.format(...)` calls.

## Pass 2 (UI deterministic choice flow)

### 1) Added deterministic choice state channel
- File: `conversation/state.py`
  - Added fields:
    - `pending_user_choice`
    - `grading_rationale`
    - `session_memory_summary`

### 2) Topic selection card flow wired
- File: `conversation/dean.py`
  - Topic engagement returns now set:
    - `pending_user_choice = {"kind": "topic", "options": [...]}`
  - Clears pending choice after topic lock.
- File: `ui/app.py`
  - `_render_topic_option_cards` now prefers `pending_user_choice` options.
  - Button labels now render clean option text.
  - During active topic-choice moment, free-text input is suppressed.

### 3) Clinical opt-in button flow made literal
- File: `conversation/nodes.py`
  - At assessment turn 0, sets:
    - `pending_user_choice = {"kind": "opt_in", "options": ["yes", "no"]}`
  - At assessment turn 1, parser now expects literal `"yes"` / `"no"`.
- File: `ui/app.py`
  - Opt-in buttons now send exactly `yes` or `no`.
  - During opt-in choice, free-text input is suppressed.
  - Clinical choice card width aligned closer to message lane (narrow card column).

## Verification executed
- Ran syntax compile check successfully:
  - `conversation/dean.py`
  - `conversation/nodes.py`
  - `conversation/state.py`
  - `conversation/teacher.py`
  - `config.py`
  - `retrieval/retriever.py`
  - `memory/persistent_memory.py`
  - `ui/app.py`

## Known follow-ups
- Old prompt keys still exist in config for compatibility; close-session path now uses new `dean_close_session_*` keys.
- If UI later needs “type custom topic” during topic-card mode, re-enable composer for that specific case while keeping card flow primary.
