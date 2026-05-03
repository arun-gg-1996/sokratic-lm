"""
evaluation/quality/deterministic.py
-----------------------------------
Pure-Python computations from session trace fields. No API calls.

Produces a dict of named scalar metrics that are then combined with LLM
judge outputs in `dimensions.py`. Keep this module side-effect free and
strictly dependent on `SessionView` so it's trivially testable.

Naming convention: `det_{family}_{metric}` (e.g. `det_tlq_match_top_score`).
"""

from __future__ import annotations
import re
from typing import Any
from .schema import SessionView, TutorTurn


# =============================================================================
# Helpers
# =============================================================================

_FABRICATION_KEYWORDS_REGEX = re.compile(
    r"\b("
    r"you'?ve\s+(correctly\s+)?identified|"
    r"you\s+have\s+(correctly\s+)?identified|"
    r"you\s+(did|done)\s+(well|great)\s+identifying|"
    r"correctly\s+identified|"
    r"you'?ve\s+reached|"
    r"you\s+have\s+reached|"
    r"great\s+job,?\s+(the\s+answer\s+is|identifying)|"
    r"exactly\!?\s+the\s+\w+\s+is|"
    r"that'?s\s+(it|right|correct)\s*[—\-]?\s*the\s+\w+\s+is|"
    r"indeed,?\s+(that'?s|this\s+is)|"
    r"you\s+correctly\s+(named|stated|recognized)|"
    r"you'?ve\s+correctly\s+(named|stated|recognized)"
    r")\b",
    re.IGNORECASE,
)

_OFF_TOPIC_INDICATORS = (
    "vape", "vaping", "smoke", "smoking",
    "sex", "sexual", "porn",
    "weed", "marijuana", "alcohol",
    "shit", "fuck", "damn",  # profanity often signals frustration off-topic
)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _content_tokens(s: str, stopwords: set[str]) -> set[str]:
    norm = re.sub(r"[^a-z0-9\s]+", " ", _normalize(s))
    return {t for t in norm.split() if t and t not in stopwords and len(t) > 1}


_OVERLAP_STOPS = {
    "a", "an", "the", "of", "is", "are", "and", "or", "to",
    "in", "on", "at", "for", "by", "with", "from", "this",
    "that", "it", "its", "as", "be",
}


def _student_token_overlap_with_answer(student_msg: str, locked_answer: str, aliases: list[str]) -> bool:
    """Mirror of dean.reached_answer_gate Step A. Returns True if the
    student message contains all content-tokens of locked_answer or any alias."""
    msg_tokens = _content_tokens(student_msg, _OVERLAP_STOPS)
    if not msg_tokens:
        return False
    for cand in [locked_answer] + (aliases or []):
        cand_tokens = _content_tokens(cand, _OVERLAP_STOPS)
        if cand_tokens and cand_tokens.issubset(msg_tokens):
            return True
    return False


def _safe_div(num: float, den: float, default: float = 1.0) -> float:
    return num / den if den > 0 else default


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# =============================================================================
# Main entry point
# =============================================================================

def compute_all(view: SessionView) -> dict[str, Any]:
    """Run all deterministic sub-metrics. Returns a flat dict with keys
    organized by family. Higher-level dimension assembly happens later."""
    out: dict[str, Any] = {}

    # Pre-computed lists used by multiple families
    out["intermediate_turns"] = _intermediate_turns(view)
    out["fabrication_turns"] = _fabrication_turns(view)
    out["off_topic_turns"] = _off_topic_turns(view)

    # Family computations
    out.update(_compute_tlq(view))
    out.update(_compute_rrq(view))
    out.update(_compute_aq(view))
    out.update(_compute_trq(view))
    out.update(_compute_rgc(view))
    out.update(_compute_pp(view))
    out.update(_compute_arc(view, out))
    out.update(_compute_cc(view))
    out.update(_compute_ce(view))
    out.update(_compute_msc(view))
    return out


# =============================================================================
# Pre-computed signal lists
# =============================================================================

def _intermediate_turns(view: SessionView) -> list[int]:
    """Turn IDs where student_state == 'correct' but reached == False — the
    'mine goes lower' pattern. These are the at-risk turns for ARC."""
    out = []
    for t in view.turns:
        if (t.student_state == "correct"
                and not t.student_reached_answer
                and t.phase == "tutoring"):
            out.append(t.turn_id)
    return out


def _fabrication_turns(view: SessionView) -> list[dict]:
    """Turns where the tutor message contains a fabrication keyword AND
    the gate said reached=False. These are critical-penalty candidates.

    L39 #6 — uses session-level reach in addition to per-turn reach.
    The reach-path close turn (L65) legitimately confirms the textbook
    answer + Teacher's wrap prose can naturally include fabrication
    keywords ("the answer is…"). We only flag fabrication when the
    student NEVER reached the answer in the session AND the per-turn
    reach gate said False. Sessions that reached in any turn get a
    free pass on the very last turn.
    """
    out = []
    final_turn_id = view.turns[-1].turn_id if view.turns else 0
    session_reached = bool(view.final_student_reached_answer)
    for t in view.turns:
        if t.student_reached_answer:
            # Confirmation when student actually reached is fine.
            continue
        # L39 #6 — session-level pass on the final turn for sessions
        # where reach happened in any earlier turn (close-out prose).
        if session_reached and t.turn_id == final_turn_id:
            continue
        m = _FABRICATION_KEYWORDS_REGEX.search(t.tutor_msg or "")
        if m:
            t.fabrication_keyword_match = [m.group(0)]
            out.append({
                "turn_id": t.turn_id,
                "evidence": m.group(0),
                "tutor_msg": t.tutor_msg[:200],
            })
    return out


def _off_topic_turns(view: SessionView) -> list[int]:
    """Turns where the student message contains off-topic indicators."""
    out = []
    for t in view.turns:
        msg = (t.student_msg or "").lower()
        if any(k in msg for k in _OFF_TOPIC_INDICATORS):
            out.append(t.turn_id)
    return out


# =============================================================================
# TLQ — Topic Lock Quality
# =============================================================================

def _compute_tlq(view: SessionView) -> dict[str, Any]:
    """Reads:
      - locked_topic (presence + score)
      - all_turn_traces for dean.topic_match, dean.topic_vote, anchors_locked vs anchor_extraction_failed
      - debug.coverage_gap_events
    """
    topic_match_score = 0.0
    vote_consensus = 1.0
    anchor_success = 0.0
    repair_invoked = False
    coverage_gate_pass = 1.0

    # Walk traces
    found_anchors_locked = False
    found_anchor_extraction_failed = False
    for tr in view.all_turn_traces + [{"trace": []}]:
        for entry in (tr or {}).get("trace", []):
            if not isinstance(entry, dict):
                continue
            w = entry.get("wrapper", "")
            if w == "dean.topic_match":
                ts = entry.get("top_score")
                if ts is not None:
                    try:
                        topic_match_score = float(ts) / 100.0
                    except (TypeError, ValueError):
                        pass
            elif w == "dean.topic_vote":
                weights = entry.get("all_weights") or {}
                if isinstance(weights, dict) and weights:
                    total = sum(float(v or 0) for v in weights.values())
                    winner = float(entry.get("winner_weight") or 0)
                    vote_consensus = _safe_div(winner, total, 1.0)
            elif w == "dean.anchors_locked":
                found_anchors_locked = True
            elif w == "dean.anchor_extraction_failed":
                found_anchor_extraction_failed = True
            elif w == "dean._lock_anchors_repair_call":
                repair_invoked = True

    if found_anchors_locked and not found_anchor_extraction_failed:
        anchor_success = 1.0
    elif found_anchors_locked and found_anchor_extraction_failed:
        # Failed first, then succeeded after retry
        anchor_success = 0.7
    else:
        anchor_success = 0.0

    if found_anchors_locked and view.locked_question and view.locked_answer:
        # Even if traces are missing, the existence of full anchors is signal
        anchor_success = max(anchor_success, 1.0 if not repair_invoked else 0.7)
    elif view.locked_question and view.locked_answer:
        anchor_success = max(anchor_success, 0.7)

    # Coverage gate: 1.0 minus normalized event count
    if view.coverage_gap_events > 0:
        coverage_gate_pass = max(0.0, 1.0 - 0.25 * view.coverage_gap_events)

    repair_clean = 1.0 if not repair_invoked else 0.7

    return {
        "det_tlq_match_top_score": _clamp01(topic_match_score) if topic_match_score > 0 else None,
        "det_tlq_vote_consensus": _clamp01(vote_consensus),
        "det_tlq_coverage_gate_pass": _clamp01(coverage_gate_pass),
        "det_tlq_anchor_extraction_success": _clamp01(anchor_success),
        "det_tlq_repair_clean": _clamp01(repair_clean),
        "det_tlq_repair_invoked": repair_invoked,
    }


# =============================================================================
# RRQ — RAG Retrieval Quality
# =============================================================================

def _compute_rrq(view: SessionView) -> dict[str, Any]:
    chunks = view.retrieved_chunks or []
    chunk_count = len(chunks)
    chunk_count_adequacy = _clamp01(chunk_count / 5.0) if chunk_count else 0.0

    # mean top-3 score
    scores = []
    for c in chunks:
        s = (c or {}).get("score")
        if s is not None:
            try:
                scores.append(float(s))
            except (TypeError, ValueError):
                continue
    scores.sort(reverse=True)
    mean_top3 = sum(scores[:3]) / max(1, len(scores[:3])) if scores else 0.0
    # If scores look like 0–100, normalize. If 0–1, leave alone.
    if mean_top3 > 1.5:
        mean_top3 = mean_top3 / 100.0
    mean_top3 = _clamp01(mean_top3)

    # section match rate
    locked_subsection = (view.locked_topic or {}).get("subsection") or ""
    if chunks and locked_subsection:
        n_match = sum(
            1 for c in chunks
            if str((c or {}).get("subsection_title") or "").strip() == locked_subsection.strip()
        )
        section_match_rate = n_match / chunk_count
    else:
        section_match_rate = 1.0 if chunk_count else 0.0

    # retrieval calls efficiency
    rcalls = view.retrieval_calls
    retrieval_calls_eff = 1.0 if rcalls <= 1 else _safe_div(1.0, rcalls, 1.0)

    # coverage gap frequency
    gap_freq = 1.0 - min(1.0, 0.25 * view.coverage_gap_events)

    return {
        "det_rrq_chunk_count_adequacy": chunk_count_adequacy,
        "det_rrq_mean_top3_score": mean_top3,
        "det_rrq_section_match_rate": _clamp01(section_match_rate),
        "det_rrq_retrieval_calls_efficiency": _clamp01(retrieval_calls_eff),
        "det_rrq_coverage_gap_frequency": _clamp01(gap_freq),
        "det_rrq_chunk_count": chunk_count,
        "det_rrq_retrieval_calls": rcalls,
    }


# =============================================================================
# AQ — Anchor Quality
# =============================================================================

def _compute_aq(view: SessionView) -> dict[str, Any]:
    locked_answer = view.locked_answer or ""
    word_count = len([w for w in locked_answer.split() if w.strip()])

    if 2 <= word_count <= 5:
        answer_brevity = 1.0
    elif 6 <= word_count <= 10:
        answer_brevity = 0.7
    elif word_count == 1:
        answer_brevity = 0.5  # might be too terse; e.g. "ATP"
    else:
        answer_brevity = 0.3

    # noun phrase heuristic — no verbs, no commas with multiple ideas
    answer_is_noun_phrase = 1.0
    if locked_answer:
        # Sentence-like markers
        if re.search(r"\b(is|are|was|were|has|have|spreads?|innervates?|arises?|branches?|supplies?|allows?|enables?)\b",
                     locked_answer, re.IGNORECASE):
            answer_is_noun_phrase = 0.0
        # Multiple commas suggest a list, not a phrase
        if locked_answer.count(",") >= 2:
            answer_is_noun_phrase = min(answer_is_noun_phrase, 0.3)

    aliases_count = len(view.locked_answer_aliases or [])
    aliases_count_adequacy = _clamp01(aliases_count / 4.0)

    # Sanitize action — read from traces if present
    sanitize_action = None
    for tr in view.all_turn_traces:
        for entry in (tr or {}).get("trace", []):
            if isinstance(entry, dict) and entry.get("wrapper") == "dean.sanitize_locked_answer":
                # Take the LAST one (final action after possible repair)
                sanitize_action = entry.get("action")

    if sanitize_action == "kept":
        groundedness = 1.0
    elif sanitize_action and sanitize_action.startswith("wiped"):
        # Wiped means the LLM produced something that didn't pass — but we
        # might still have a final answer from repair. Use the existence
        # of a non-empty locked_answer as the secondary signal.
        groundedness = 0.5 if locked_answer else 0.0
    else:
        # No trace info available — fall back on the answer existing at all
        groundedness = 1.0 if locked_answer else 0.0

    return {
        "det_aq_answer_brevity": answer_brevity,
        "det_aq_answer_is_noun_phrase": answer_is_noun_phrase,
        "det_aq_aliases_count_adequacy": aliases_count_adequacy,
        "det_aq_aliases_count": aliases_count,
        "det_aq_groundedness": groundedness,
        "det_aq_sanitize_action": sanitize_action,
        "det_aq_locked_answer_word_count": word_count,
    }


# =============================================================================
# TRQ — Tutor Response Quality (deterministic part; EULER comes from LLM)
# =============================================================================

def _compute_trq(view: SessionView) -> dict[str, Any]:
    tutoring_turns = [t for t in view.turns if t.phase == "tutoring"]
    n = len(tutoring_turns)
    if n == 0:
        return {
            "det_trq_qc_pass_rate": None,
            "det_trq_revised_draft_rate": None,
            "det_trq_intervention_rate": None,
            "det_trq_reason_codes_clean_rate": None,
            "det_trq_n_tutoring_turns": 0,
        }

    qc_pass_count = sum(1 for t in tutoring_turns if t.qc_pass is True)
    qc_known = sum(1 for t in tutoring_turns if t.qc_pass is not None)
    qc_pass_rate = _safe_div(qc_pass_count, qc_known, 1.0)

    revised_count = sum(1 for t in tutoring_turns if t.revised_draft_applied)
    revised_draft_rate = 1.0 - _safe_div(revised_count, n, 0.0)

    interventions = view.interventions or sum(
        1 for t in tutoring_turns if t.intervention_used
    )
    intervention_rate = 1.0 - _safe_div(interventions, n, 0.0)

    # reason_codes_clean: % of turns with empty reason_codes
    clean_count = sum(1 for t in tutoring_turns if not t.qc_reason_codes)
    reason_codes_clean = _safe_div(clean_count, n, 1.0)

    return {
        "det_trq_qc_pass_rate": _clamp01(qc_pass_rate),
        "det_trq_revised_draft_rate": _clamp01(revised_draft_rate),
        "det_trq_intervention_rate": _clamp01(intervention_rate),
        "det_trq_reason_codes_clean_rate": _clamp01(reason_codes_clean),
        "det_trq_n_tutoring_turns": n,
    }


# =============================================================================
# RGC — Reached-Gate Correctness
# =============================================================================

def _compute_rgc(view: SessionView) -> dict[str, Any]:
    """For each turn with a gate decision recorded, classify:
      - true positive: reached=True ∧ student msg overlaps locked_answer/aliases OR has a verbatim quote.
      - false positive: reached=True ∧ no overlap ∧ no quote evidence.
      - true negative: reached=False ∧ no overlap.
      - false negative: reached=False ∧ overlap (likely missed positive).
    """
    aliases = view.locked_answer_aliases or []
    locked = view.locked_answer or ""
    fp = 0
    fn = 0
    tp = 0
    tn = 0
    n_with_decision = 0
    paths: dict[str, int] = {}

    for t in view.turns:
        if t.gate_path is None:
            # No gate ran (rapport, ack turn, etc.)
            continue
        n_with_decision += 1
        paths[t.gate_path] = paths.get(t.gate_path, 0) + 1

        msg_overlap = _student_token_overlap_with_answer(t.student_msg, locked, aliases)
        # evidence_in_msg: was the gate's quoted evidence actually a substring of student_msg?
        ev = (t.gate_evidence or "").strip().lower()
        evidence_in_msg = bool(ev) and ev in (t.student_msg or "").lower()

        if t.student_reached_answer:
            if msg_overlap or evidence_in_msg:
                tp += 1
            else:
                fp += 1
        else:
            if msg_overlap and not _hedge_detected(t.student_msg):
                fn += 1
            else:
                tn += 1

    total_pos_decisions = tp + fp
    total_neg_decisions = tn + fn

    fp_rate = _safe_div(fp, total_pos_decisions, 0.0)
    fn_rate = _safe_div(fn, total_neg_decisions, 0.0)

    # evidence_quote_validity: of paraphrase-path turns, what % had a valid quote?
    paraphrase_total = 0
    paraphrase_valid = 0
    for t in view.turns:
        if t.gate_path == "paraphrase" and t.student_reached_answer:
            paraphrase_total += 1
            ev = (t.gate_evidence or "").strip().lower()
            if ev and ev in (t.student_msg or "").lower():
                paraphrase_valid += 1
    evidence_quote_validity = _safe_div(paraphrase_valid, paraphrase_total, 1.0)

    return {
        "det_rgc_false_positive_rate": fp_rate,
        "det_rgc_false_negative_rate": fn_rate,
        "det_rgc_evidence_quote_validity": evidence_quote_validity,
        "det_rgc_path_distribution": paths,
        "det_rgc_n_decisions": n_with_decision,
        "det_rgc_tp": tp, "det_rgc_fp": fp, "det_rgc_tn": tn, "det_rgc_fn": fn,
    }


_HEDGE_RE = re.compile(
    r"\b(i\s+don'?t\s+know|no\s+idea|not\s+sure|i'?m\s+lost|idk|"
    r"i\s+forget|no\s+clue|i\s+can'?t\s+remember)\b",
    re.IGNORECASE,
)


def _hedge_detected(msg: str) -> bool:
    return bool(_HEDGE_RE.search(msg or ""))


# =============================================================================
# PP — Pedagogical Progression
# =============================================================================

def _compute_pp(view: SessionView) -> dict[str, Any]:
    tutoring_turns = [t for t in view.turns if t.phase == "tutoring"]
    n = len(tutoring_turns)
    if n == 0:
        return {
            "det_pp_hint_progression_monotonic": None,
            "det_pp_hint_utilization": None,
            "det_pp_student_engagement_rate": None,
            "det_pp_state_trajectory_score": None,
        }

    # Hint progression monotonicity (across tutoring turns)
    levels = [t.hint_level for t in tutoring_turns if t.hint_level is not None]
    monotonic = 1.0
    if len(levels) >= 2:
        # 1.0 if every step is non-decreasing; lower if any decrease
        decreases = sum(1 for a, b in zip(levels, levels[1:]) if b < a)
        monotonic = 1.0 - (decreases / max(1, len(levels) - 1))

    # Hint utilization
    final_hint = view.final_hint_level
    if view.final_student_reached_answer:
        # Lower hint usage = better when reaching the answer
        hint_utilization = max(0.0, 1.0 - (final_hint / max(1, view.max_hints)))
    else:
        # Used all hints + still didn't reach: utilization measures effort, neutral
        hint_utilization = _clamp01(final_hint / max(1, view.max_hints))

    # Engagement: % of turns NOT low_effort or irrelevant
    bad_states = sum(
        1 for t in tutoring_turns
        if t.student_state in {"low_effort", "irrelevant"}
    )
    engagement = 1.0 - _safe_div(bad_states, n, 0.0)

    # State trajectory: map states to numeric, fit slope
    state_map = {"incorrect": 0.0, "low_effort": 0.0, "irrelevant": 0.0,
                 "question": 0.3, "partial_correct": 0.5, "correct": 1.0}
    nums = [state_map.get(t.student_state or "", 0.0) for t in tutoring_turns]
    trajectory_score = _slope_score(nums) if nums else 0.5

    return {
        "det_pp_hint_progression_monotonic": _clamp01(monotonic),
        "det_pp_hint_utilization": _clamp01(hint_utilization),
        "det_pp_student_engagement_rate": _clamp01(engagement),
        "det_pp_state_trajectory_score": _clamp01(trajectory_score),
    }


def _slope_score(values: list[float]) -> float:
    """Simple least-squares slope, normalized to [0, 1]. Positive slope = good."""
    n = len(values)
    if n < 2:
        return 0.5
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.5
    slope = num / den
    # Slope of 1/n means full traversal across the session — score 1.0
    target = 1.0 / max(1, n - 1)
    return _clamp01(0.5 + (slope / max(target, 0.01)) * 0.5)


# =============================================================================
# ARC — Answer-Reach vs Step-Correctness (deterministic part)
# =============================================================================

def _compute_arc(view: SessionView, prior: dict[str, Any]) -> dict[str, Any]:
    """Deterministic ARC pieces. The mastery_attribution_grounding sub-score
    requires LLM judgment and is added later from llm_judges output."""
    intermediate = prior.get("intermediate_turns") or []
    fabrications = prior.get("fabrication_turns") or []

    # step_vs_reach disambiguation: how many intermediate turns (correct ∧ ¬reached)
    # were FOLLOWED by a tutor message that contained a fabrication keyword?
    if not intermediate:
        det_step_vs_reach = 1.0
    else:
        bad = 0
        fab_turn_ids = {f["turn_id"] for f in fabrications}
        for tid in intermediate:
            # Check the SAME turn's tutor msg for fabrication
            if tid in fab_turn_ids:
                bad += 1
        det_step_vs_reach = 1.0 - _safe_div(bad, len(intermediate), 0.0)

    # intermediate_credit_avoidance: of intermediate turns, % where tutor msg has no fabrication keyword
    if not intermediate:
        det_intermediate_credit = 1.0
    else:
        fab_turn_ids = {f["turn_id"] for f in fabrications}
        ok = sum(1 for tid in intermediate if tid not in fab_turn_ids)
        det_intermediate_credit = _safe_div(ok, len(intermediate), 1.0)

    return {
        "det_arc_step_vs_reach_disambiguation": _clamp01(det_step_vs_reach),
        "det_arc_intermediate_credit_avoidance": _clamp01(det_intermediate_credit),
        "det_arc_n_intermediate_turns": len(intermediate),
        "det_arc_n_fabrication_turns": len(fabrications),
    }


# =============================================================================
# CC — Conversation Continuity
# =============================================================================

def _compute_cc(view: SessionView) -> dict[str, Any]:
    # phase transitions legality
    legal_sequence = ["rapport", "tutoring", "assessment", "memory_update"]
    seen_phases = []
    for t in view.turns:
        if t.phase and (not seen_phases or seen_phases[-1] != t.phase):
            seen_phases.append(t.phase)
    legality = 1.0
    last_idx = -1
    for ph in seen_phases:
        if ph not in legal_sequence:
            continue
        idx = legal_sequence.index(ph)
        if idx < last_idx:
            legality = 0.0
            break
        last_idx = idx

    # Topic stability: locked_topic.subsection must be unchanged across turns.
    # We don't have per-turn snapshots, so use locked_topic_snapshot in debug
    # if available. For now, infer from invariant_violations.
    topic_stability = 1.0
    for v in view.invariant_violations:
        if isinstance(v, dict) and "topic" in str(v.get("kind", "")).lower():
            topic_stability = 0.0
            break

    # Anchor stability (proxy: locked_question and locked_answer are non-empty
    # and unchanged — we don't snapshot per-turn, so this is asserted by absence
    # of "anchors_locked" appearing more than once after the first lock).
    anchors_locked_count = 0
    for tr in view.all_turn_traces:
        for e in (tr or {}).get("trace", []):
            if isinstance(e, dict) and e.get("wrapper") == "dean.anchors_locked":
                anchors_locked_count += 1
    anchor_stability = 1.0 if anchors_locked_count <= 1 else 0.5

    # Invariant violations
    iv_score = 1.0 - min(1.0, 0.5 * len(view.invariant_violations))

    return {
        "det_cc_phase_transitions_legal": _clamp01(legality),
        "det_cc_topic_stability": _clamp01(topic_stability),
        "det_cc_anchor_stability": _clamp01(anchor_stability),
        "det_cc_invariant_violations_count": len(view.invariant_violations),
        "det_cc_invariant_score": _clamp01(iv_score),
    }


# =============================================================================
# CE — Cost & Efficiency
# =============================================================================

def _compute_ce(view: SessionView) -> dict[str, Any]:
    useful_turns = max(1, len([t for t in view.turns if t.phase == "tutoring"]))
    cost_per_turn = view.cost_usd / useful_turns

    # Score: budget target $0.20 per useful turn
    target = 0.20
    cost_score = _clamp01(1.0 - min(1.0, cost_per_turn / target))

    # Cache hit rate (proxy from totals)
    cache_total = view.cache_read_tokens + view.cache_write_tokens + view.input_tokens
    cache_hit_rate = _safe_div(view.cache_read_tokens, cache_total, 0.0)

    # Latency
    if view.elapsed_s_per_turn:
        mean_latency = sum(view.elapsed_s_per_turn) / len(view.elapsed_s_per_turn)
    else:
        mean_latency = None
    latency_score = (
        _clamp01(1.0 - min(1.0, mean_latency / 15.0)) if mean_latency is not None else None
    )

    return {
        "det_ce_cost_per_useful_turn": cost_per_turn,
        "det_ce_cost_score": cost_score,
        "det_ce_cache_hit_rate": _clamp01(cache_hit_rate),
        "det_ce_mean_turn_latency": mean_latency,
        "det_ce_latency_score": latency_score,
    }


# =============================================================================
# MSC — Mastery Scoring Calibration (deterministic part)
# =============================================================================

def _compute_msc(view: SessionView) -> dict[str, Any]:
    """The grounding sub-metric is LLM-judged elsewhere. Here we compute
    confidence appropriateness and EWMA movement."""
    mastery = view.mastery_score
    confidence = view.mastery_confidence
    reached = view.final_student_reached_answer

    # confidence_appropriateness: penalize mismatch between reached and confidence
    if mastery is None or confidence is None:
        confidence_appropriateness = None
    else:
        if reached and confidence < 0.4:
            # Underclaim — reached but low confidence
            confidence_appropriateness = 0.5
        elif (not reached) and confidence > 0.7:
            # Overclaim — didn't reach but high confidence
            confidence_appropriateness = 0.0
        else:
            confidence_appropriateness = 1.0

    # EWMA movement appropriate (heuristic: nominal range 0.0–1.0)
    if view.student_mastery_confidence_first is not None and view.student_mastery_confidence_last is not None:
        delta = view.student_mastery_confidence_last - view.student_mastery_confidence_first
        # Positive delta + reached = aligned. Negative delta + ¬reached = aligned.
        if reached and delta >= 0:
            ewma_movement_score = 1.0
        elif (not reached) and delta <= 0:
            ewma_movement_score = 1.0
        else:
            ewma_movement_score = 0.5
    else:
        ewma_movement_score = None

    return {
        "det_msc_confidence_appropriateness": confidence_appropriateness,
        "det_msc_ewma_movement_score": ewma_movement_score,
        "det_msc_has_mastery": mastery is not None,
    }
