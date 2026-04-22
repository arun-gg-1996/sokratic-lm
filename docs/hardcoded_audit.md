# Hardcoded Audit (Dean + Nodes)

| Name | File:line | Used at | Purpose | Disposition |
|---|---|---|---|---|
| `_BANNED_FILLER_PREFIXES` | `conversation/dean.py:46` | `dean._deterministic_tutoring_check` | deterministic QC banlist for generic lead-ins | KEEP (safety/quality guard) |
| `_STRONG_AFFIRM_PATTERNS` | `conversation/dean.py:50` | `_has_strong_affirmation` | prevent sycophantic affirmation in non-correct states | KEEP (safety/quality guard) |
| `_RETRIEVAL_LOW_SIGNAL` | `conversation/dean.py:56` | `_is_ambiguous_retrieval_query` | block weak/ambiguous retrieval calls | KEEP (retrieval hygiene) |
| `_RETRIEVAL_NOISE_PATTERNS` | `conversation/dean.py:73` | `_clean_retrieval_query` | normalize noisy student inputs before retrieval | KEEP (retrieval hygiene) |
| `_COMMON_NERVE_GUESSES` | `conversation/dean.py` (removed) | `_contains_explicit_wrong_nerve_guess` | domain-specific nerve lexicon | REPLACE (deleted; generic regex extraction) |
| `_UNCERTAINTY_STRONG_MARKERS` | `conversation/dean.py` (removed) | `_heuristic_confidence` | heuristic confidence penalty markers | REPLACE-LLM (deleted) |
| `_UNCERTAINTY_WEAK_MARKERS` | `conversation/dean.py` (removed) | `_heuristic_confidence` | heuristic confidence penalty markers | REPLACE-LLM (deleted) |
| `_CONFIDENCE_MARKERS` | `conversation/dean.py` (removed) | `_heuristic_confidence` | heuristic confidence boost markers | REPLACE-LLM (deleted) |
| `_heuristic_confidence` | `conversation/dean.py` (removed) | setup fallback + confidence blend | post-hoc confidence mixing | REPLACE-LLM (deleted; model score + deterministic fallback only) |
| blend `0.7/0.3` + `0.69` cap | `conversation/dean.py` (removed) | `_compute_student_confidence` | heuristic calibration blend | REPLACE-LLM (deleted) |
| repetition threshold `0.9` literal | `conversation/dean.py` | `_is_repetitive_question` | detect near-duplicate tutor questions | CONFIG (`thresholds.repetition_similarity`) |
| `_is_affirmative` / `_is_negative` | `conversation/nodes.py` (removed) | assessment opt-in branch | free-text yes/no regex parser | REPLACE-UI (button/card flow emits literal `yes`/`no`) |
| `hint_score_map`, weighted mastery rubric | `conversation/nodes.py` (removed) | `_compute_mastery_tiers` | rule-based tiering | REPLACE-LLM (batched `dean._close_session_call`) |
| `_tier_from_score` | `conversation/nodes.py` (removed) | `_compute_mastery_tiers` | score -> tier mapping | REPLACE-LLM |
| `_compute_mastery_tiers` | `conversation/nodes.py` (removed) | assessment closeout branches | deterministic mastery grading | REPLACE-LLM |

## Notes
- Defensive parsing (`_extract_json_object`) remains unchanged by design.
- Topic lock + retrieval freeze contract remains: retrieval fires once at topic lock and is then frozen.
