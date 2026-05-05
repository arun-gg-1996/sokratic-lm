"""
conversation/reach_gate.py
──────────────────────────
Reach-detection gate: decides whether the student has STATED the locked
answer in their most recent message.

Ported from V1 conversation/dean.py during D1 (V1 → V2 consolidation).
The gate had three steps in V1, all preserved here:

  Step A.1 — deterministic full token-overlap (free, fast)
  Step A.2 — K-of-N partial reach (multi-component answers)
  Step B   — LLM paraphrase fallback (quote-or-reject)

Diff vs V1: Step B uses a direct anthropic client.messages.create call
rather than the V1 `_timed_create` telemetry wrapper (telemetry is
captured manually into state["debug"]["turn_trace"]). Verdict logic
is identical — same prompts, same quote-or-reject post-validation.

Public API:
    reached_answer_gate(state, student_msg, client, model) -> dict

Returns dict with keys:
    reached:    bool          (true on full or partial reach)
    coverage:   float in [0,1] (1.0 on full, K/N on partial, 0.0 otherwise)
    evidence:   str           (matched span or LLM quote)
    path:       str           (overlap | partial_overlap | paraphrase |
                              hedge_block | no_overlap_no_paraphrase |
                              no_lock | llm_no_quote | llm_parse_fail |
                              llm_error | prompt_missing)
    n_matched:  int           (components matched, multi-component only)
    n_total:    int           (total components, multi-component only)
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

from config import cfg


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# Stop words dropped when computing content-token overlap. Conservative —
# only true filler that doesn't carry meaning in answer phrases. We want
# "the skeletal muscle pump" to match locked_answer="skeletal muscle pump",
# but NOT match locked_answer="muscle" alone.
_OVERLAP_STOPWORDS = frozenset({
    "a", "an", "the", "of", "is", "are", "and", "or", "to",
    "in", "on", "at", "for", "by", "with", "from",
})

# Phrases that signal hedging / asking / denying rather than asserting.
# When any appear, Step A is skipped and we fall through to the LLM
# paraphrase check, which can read intent more reliably.
_HEDGE_MARKERS = (
    "i don't know", "i dont know", "i do not know",
    "no idea", "not sure", "not certain",
    "i'm lost", "im lost", "idk", "i forget", "no clue", "not really",
    "i can't remember", "i cant remember", "can't remember",
)


# ─────────────────────────────────────────────────────────────────────
# Tokenization helpers
# ─────────────────────────────────────────────────────────────────────


def _normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _content_tokens(s: str) -> list[str]:
    """Lowercased, punct-stripped tokens minus filler stopwords."""
    norm = _normalize_text(s)
    return [t for t in norm.split() if t and t not in _OVERLAP_STOPWORDS]


def _has_hedge(msg_lower_raw: str) -> bool:
    return any(h in msg_lower_raw for h in _HEDGE_MARKERS)


def _split_locked_answer(answer: str) -> list[str]:
    """Split a multi-component locked_answer into its top-level noun-phrase
    components. Single-component answers return a 1-element list.

    Splits on " and " / " or " (with surrounding spaces — won't split
    "android"), and on "," / ";" with optional whitespace.

    Examples:
      "skeletal muscle pump"
        → ["skeletal muscle pump"]                            (single)
      "left and right coronary arteries"
        → ["left", "right coronary arteries"]                 (2-component)
      "ingestion, propulsion, mechanical digestion, chemical digestion"
        → ["ingestion", "propulsion", "mechanical digestion",
           "chemical digestion"]                              (4-component)
    """
    if not answer or not answer.strip():
        return []
    parts = re.split(r"\s+and\s+|\s+or\s+|[,;]\s*", answer.lower())
    parts = [p.strip() for p in parts if p.strip()]
    return parts


# ─────────────────────────────────────────────────────────────────────
# JSON parsing (ported verbatim from V1 dean._extract_json_object)
# ─────────────────────────────────────────────────────────────────────


def _extract_json_object(text: str) -> dict | None:
    """Robustly extract a JSON object from model text. Handles raw JSON,
    fenced blocks, leading/trailing prose, python-dict style fallbacks."""
    if not text:
        return None

    def _normalize_structured(s: str) -> str:
        s = (s or "").strip()
        s = s.replace("“", "\"").replace("”", "\"")
        s = s.replace("‘", "'").replace("’", "'")
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
        return s.strip()

    def _remove_trailing_commas(s: str) -> str:
        return re.sub(r",(\s*[}\]])", r"\1", s)

    def _try_parse_dict(s: str) -> dict | None:
        if not s:
            return None
        for cand in (s, _remove_trailing_commas(s)):
            try:
                parsed = json.loads(cand)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        # Python-literal fallback for single-quoted dict outputs
        pyish = _remove_trailing_commas(s)
        pyish = re.sub(r"\btrue\b", "True", pyish)
        pyish = re.sub(r"\bfalse\b", "False", pyish)
        pyish = re.sub(r"\bnull\b", "None", pyish)
        try:
            parsed = ast.literal_eval(pyish)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    def _extract_braced_candidates(s: str) -> list[str]:
        out: list[str] = []
        start = -1
        depth = 0
        in_string = False
        quote_char = ""
        escape = False
        for i, ch in enumerate(s):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote_char:
                    in_string = False
                continue
            if ch in ("\"", "'"):
                in_string = True
                quote_char = ch
                continue
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        out.append(s[start:i + 1])
                        start = -1
        return out

    candidate = _normalize_structured(text)
    parsed = _try_parse_dict(candidate)
    if parsed is not None:
        return parsed
    for snippet in _extract_braced_candidates(candidate):
        parsed = _try_parse_dict(snippet)
        if parsed is not None:
            return parsed
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return _try_parse_dict(candidate[start:end + 1])


# ─────────────────────────────────────────────────────────────────────
# History rendering (simple — V2 history_render is for richer flows)
# ─────────────────────────────────────────────────────────────────────


def _format_history_simple(messages: list[dict]) -> str:
    """Plain-text history rendering for the reach-check LLM context.

    Last 8 messages = ~4 tutor/student exchanges. Enough context for the
    LLM to see what was being asked without prompt bloat.
    """
    lines = []
    for m in (messages or [])[-8:]:
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# LLM paraphrase fallback (Step B)
# ─────────────────────────────────────────────────────────────────────


def _trace(state: dict, entry: dict) -> None:
    """Append to state["debug"]["turn_trace"], creating dicts as needed."""
    state.setdefault("debug", {}).setdefault("turn_trace", []).append(entry)


def _reached_check_llm(
    state: dict,
    student_msg: str,
    locked_answer: str,
    aliases: list[str],
    msg_norm: str,
    client: Any,
    model: str,
) -> dict:
    """LLM step of reached_answer_gate. Quote-or-reject post-validation."""
    aliases_str = ", ".join(aliases) if aliases else "(none)"
    static_block = getattr(cfg.prompts, "dean_reached_check_static", "")
    dynamic_template = getattr(cfg.prompts, "dean_reached_check_dynamic", "")
    if not static_block or not dynamic_template:
        _trace(state, {
            "wrapper": "reach_gate.reached_answer_gate",
            "result": "prompt_missing",
        })
        return {"reached": False, "evidence": "", "path": "prompt_missing"}

    recent_history = _format_history_simple(state.get("messages", []))
    dynamic_prompt = dynamic_template.format(
        locked_answer=locked_answer,
        aliases=aliases_str,
        recent_history=recent_history,
        student_msg=student_msg,
    )

    base = getattr(cfg.prompts, "dean_base", "")
    system_str = "\n\n".join(s for s in (base, static_block, dynamic_prompt) if s)

    try:
        resp = client.messages.create(
            model=model,
            temperature=0,
            max_tokens=160,
            system=system_str,
            messages=[{
                "role": "user",
                "content": "Decide and return strict JSON.",
            }],
        )
    except Exception as exc:
        _trace(state, {
            "wrapper": "reach_gate.reached_answer_gate",
            "result": "llm_error",
            "error": str(exc)[:200],
        })
        return {"reached": False, "evidence": "", "path": "llm_error"}

    text = (resp.content[0].text or "").strip()
    parsed = _extract_json_object(text)
    if parsed is None:
        return {"reached": False, "evidence": "", "path": "llm_parse_fail"}

    reached_claim = bool(parsed.get("reached", False))
    evidence = str(parsed.get("evidence", "") or "").strip()

    # Quote-or-reject: if the LLM claims reached, evidence must be a
    # verbatim substring of the student message. Hallucinated quotes
    # force reached=False.
    if reached_claim:
        ev_norm = _normalize_text(evidence)
        quote_present = (
            bool(ev_norm) and ev_norm in msg_norm
        ) or (
            bool(evidence) and evidence.lower() in (student_msg or "").lower()
        )
        if not quote_present:
            _trace(state, {
                "wrapper": "reach_gate.reached_answer_gate",
                "result": "llm_no_quote",
                "claimed_evidence": evidence[:120],
            })
            return {
                "reached": False,
                "evidence": evidence,
                "path": "llm_no_quote",
            }

    path = "paraphrase" if reached_claim else "no_overlap_no_paraphrase"
    return {
        "reached": reached_claim,
        "evidence": evidence if reached_claim else "",
        "path": path,
    }


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def reached_answer_gate(
    state: dict,
    student_msg: str,
    client: Any,
    model: str,
) -> dict:
    """Strict gate: did the student STATE the locked answer in their msg?

    Three-step strategy:
      Step A.1 — deterministic full token-overlap (free, fast)
      Step A.2 — K-of-N partial reach (multi-component answers)
      Step B   — LLM paraphrase fallback (quote-or-reject)

    Bias: reached=False on any ambiguity. False positives fabricate
    confirmations the student didn't earn — much worse than running
    one extra hint turn.

    Args:
        state:        TutorState dict (provides locked_answer,
                      locked_answer_aliases, messages, debug).
        student_msg:  Latest student message text.
        client:       Anthropic client instance for the LLM fallback.
        model:        Model id string.

    Returns:
        dict with keys: reached, coverage, evidence, path, plus
        n_matched / n_total for multi-component answers.
    """
    locked_answer = (state.get("locked_answer") or "").strip()
    if not locked_answer:
        return {"reached": False, "coverage": 0.0, "evidence": "", "path": "no_lock"}

    aliases = list(state.get("locked_answer_aliases") or [])
    msg_norm = _normalize_text(student_msg)
    msg_norm_tokens = set(msg_norm.split()) if msg_norm else set()
    msg_lower_raw = (student_msg or "").lower()

    # ---- Step A.1: deterministic full token overlap (skip on hedge) ----
    if not _has_hedge(msg_lower_raw):
        components = _split_locked_answer(locked_answer)
        multi = len(components) >= 2

        # Always check full match against locked_answer itself.
        locked_tokens = _content_tokens(locked_answer)
        if locked_tokens and all(tok in msg_norm_tokens for tok in locked_tokens):
            return {
                "reached": True,
                "coverage": 1.0,
                "evidence": locked_answer,
                "path": "overlap",
            }

        # Aliases — single-component answers only. For multi-component,
        # aliases are per-component identifiers (e.g. ["LCA",
        # "left coronary artery", ...] for "left and right coronary
        # arteries"). A single alias match would cheat the K-of-N partial
        # reach below — student says "LCA" and it'd count as full reach.
        if not multi:
            for cand in aliases:
                cand_tokens = _content_tokens(cand)
                if not cand_tokens:
                    continue
                if all(tok in msg_norm_tokens for tok in cand_tokens):
                    return {
                        "reached": True,
                        "coverage": 1.0,
                        "evidence": cand,
                        "path": "overlap",
                    }

        # ---- Step A.2: K-of-N partial reach for multi-component ----
        if multi:
            # Track distinct matches by sorted-content-token-tuple to
            # avoid double-counting "LCA" + "left coronary artery"
            # (two surface forms of the same component) as 2 hits.
            matched_phrases: list[str] = []
            seen_token_keys: set[tuple] = set()
            for phrase in components + aliases:
                phrase_tokens = _content_tokens(phrase)
                if not phrase_tokens:
                    continue
                if not all(tok in msg_norm_tokens for tok in phrase_tokens):
                    continue
                key = tuple(sorted(phrase_tokens))
                if key in seen_token_keys:
                    continue
                # Subset/superset dedup: skip "left coronary" if "left
                # coronary artery" already counted (or vice versa).
                skip = False
                for prior_key in seen_token_keys:
                    prior_set = set(prior_key)
                    cur_set = set(phrase_tokens)
                    if cur_set.issubset(prior_set) or prior_set.issubset(cur_set):
                        skip = True
                        break
                if skip:
                    continue
                seen_token_keys.add(key)
                matched_phrases.append(phrase)

            n_matched = len(matched_phrases)
            n_total = len(components)
            threshold = max(1, (n_total + 1) // 2)  # ceil(n/2)
            if n_matched >= threshold:
                is_full = (n_matched >= n_total)
                return {
                    "reached": True,
                    "coverage": min(1.0, n_matched / n_total) if n_total else 0.0,
                    "evidence": ", ".join(matched_phrases[:3]),
                    "path": "overlap" if is_full else "partial_overlap",
                    "n_matched": n_matched,
                    "n_total": n_total,
                }

    # ---- Step B: LLM paraphrase fallback ----
    result = _reached_check_llm(
        state, student_msg, locked_answer, aliases, msg_norm, client, model,
    )
    # Ensure coverage key is present even on LLM path (binary 1.0 / 0.0).
    if "coverage" not in result:
        result["coverage"] = 1.0 if result.get("reached") else 0.0
    return result
