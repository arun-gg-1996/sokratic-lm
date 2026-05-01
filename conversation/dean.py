"""
conversation/dean.py
---------------------
Dean agent — the supervisor of the tutoring session.

Dean's responsibilities each turn:
  1. _setup_call(): retrieve context if needed, lock answer, classify student_state,
     and decide whether student reached answer (structured output via submit_turn_evaluation).
  2. If student reached answer, return early (assessment_node handles clinical flow).
  3. Help-abuse gating in Python: low-effort streak can still advance hint level.
  4. teacher.draft_socratic(): generate a candidate tutor response.
  5. _quality_check_call(): enforce EULER-like quality + LeakGuard entailment.
  6. If quality fails, apply Dean's revised_teacher_draft in one pass.
     If no valid revision is returned, Dean writes fallback directly.
  7. Append approved response to state["messages"], update debug trace/metrics.

All Anthropic calls are timed and logged in state["debug"]["turn_trace"] with:
  - full system prompt blocks,
  - sent messages,
  - tool inputs,
  - raw response text,
  - token/cost/cache metrics.
"""

import ast
import json
import re
import time
import hashlib
from difflib import SequenceMatcher
from pathlib import Path
import anthropic
from conversation.state import TutorState
from conversation.rendering import render_history
from tools.mcp_tools import search_textbook, save_tool_definitions
from config import cfg
from retrieval.topic_matcher import get_topic_matcher, TopicMatch, MatchResult

# Sonnet pricing (per million tokens)
_PRICE_IN = 3.0
_PRICE_OUT = 15.0
# 2026-05-01: _BANNED_FILLER_PREFIXES + _STRONG_AFFIRM_PATTERNS were
# regex-based sycophancy / banned-opener detectors. Per the user's
# "LLM-only QC" directive, both were replaced by
# conversation/classifiers.haiku_sycophancy_check, which reads the draft
# + student_state + reach_fired and returns a verdict. Validated 100%
# accuracy on 27 hand-curated cases (15 sycophantic + 12 clean) — see
# data/artifacts/classifiers/2026-05-01T21-03-51/report.md. The Haiku
# classifier catches Sonnet 4.6's empathic soft-affirmation patterns
# ("on an interesting track", "in the right neighborhood", "you've
# touched on the answer") that the regex required ad-hoc maintenance to
# track. Single source of truth = LLM judgment.

_RETRIEVAL_NOISE_PATTERNS = (
    r"^\s*\d+\s*[\)\.\-:]\s*",
    r"^\s*(i think|i guess|maybe|honestly)\s+",
    r"\s+",  # collapsed at end
)


def _domain_prompt_vars() -> dict:
    domain = getattr(cfg, "domain", object())
    return {
        "domain_name": getattr(domain, "name", "the subject"),
        "domain_short": getattr(domain, "short", "the subject"),
        "student_descriptor": getattr(domain, "student_descriptor", "student"),
        "domain_example_topic_specific": getattr(domain, "example_topic_specific", "a specific concept"),
        "domain_example_topic_broad": getattr(domain, "example_topic_broad", "a broad topic area"),
        "domain_example_question": getattr(domain, "example_question", "What is the key concept here?"),
        "assessment_dimension": getattr(domain, "assessment_dimension", "real-world application"),
        "assessment_dimension_examples": getattr(domain, "assessment_dimension_examples", "examples, problems, or context"),
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _apply_domain_vars(text: str) -> str:
    rendered = text or ""
    for key, val in _domain_prompt_vars().items():
        rendered = rendered.replace(f"{{{key}}}", str(val))
    return rendered


def _cached_system(
    role_base: str,
    wrapper_delta: str,
    chunks: str,
    history: str,
    turn_deltas: str,
) -> list:
    """
    Multi-block cache layout (post-2026-04-29 rewrite).

    Why this exists
    ---------------
    The previous implementation joined `role_base + wrapper_delta + chunks +
    history` into a single cached block. Because `history` grows turn-over-
    turn, the block's bytes changed every turn → cache prefix never matched
    → cache hit rate was 0% in production (verified via cache_smoke_test
    on 2026-04-29). Caching telemetry showed the previous code WAS writing
    cache entries (e.g. one call cache_write=4922) but never reading them
    on subsequent turns because the prefix had changed.

    New layout
    ----------
      Block 1 [CACHED-if-large-enough]:  role_base + wrapper_delta + chunks
        Stable across all turns of a session, so cache_read on Block 1
        fires from turn 2 onward.
      Block 2 [CACHED-if-large-enough]:  history
        On turn N, the history is `messages[0..N-1]` rendered. Anthropic's
        cache lookup matches the full prefix up to each cache_control marker:
        for Block 2 to hit, the EXACT history bytes have to have been seen
        before. So Block 2 caches only fire on retries within the same turn
        (same history value) — not across turns. This is fine: the win on
        cross-turn caching comes from Block 1 alone, since Block 1's prefix
        is stable.
      Block 3 UNCACHED: turn_deltas
        Per-turn variable content (current student message, hints, etc.).

    Why split history into its own block instead of leaving it uncached
    -------------------------------------------------------------------
    The Anthropic cache key for a breakpoint is the prefix UP TO AND
    INCLUDING the marker. If history were in the SAME block as
    role/wrapper/chunks, history's growth would invalidate the marker for
    Block 1 too. Pulling history into a separate block means Block 1's
    marker hashes only over (role + wrapper + chunks), which is stable.

    Caching threshold notes
    -----------------------
    Haiku 4.5 caches blocks ≥ 4096 tokens; Sonnet 4.5 ≥ 1024. Our estimate
    is len/4 and overshoots, so we use 4000 as the gate to be conservative
    on Haiku. Sub-threshold blocks are sent without cache_control (Anthropic
    silently ignores cache markers below the threshold anyway).
    """
    blocks: list[dict] = []
    # Per Anthropic 2026 docs: minimum cacheable prompt is 1024 tokens for
    # Sonnet 4-5 and 2048 for Haiku 4-5. We use 1500 — covers Sonnet
    # cleanly, slightly over-aggressive for Haiku. Sub-threshold
    # cache_control markers are silently ignored by the API (no error,
    # no charge), so a too-low threshold costs us nothing; a too-high
    # threshold costs us cache hits we should be getting. The previous
    # value 4000 was based on a misread — it ruled out caching on most of
    # our calls (verified empirically 2026-04-29).
    cache_min_tokens = 1500
    role_base = _apply_domain_vars(role_base)
    wrapper_delta = _apply_domain_vars(wrapper_delta)
    chunks = _apply_domain_vars(chunks)
    history_rendered = _apply_domain_vars(history or "")
    turn_deltas = _apply_domain_vars(turn_deltas)

    # Block 1: stable session content. role + wrapper + chunks. Caches
    # from turn 2 onward provided it clears the threshold.
    stable = "\n\n".join(part for part in [role_base, wrapper_delta, chunks] if part)

    # Optional per-call cache-block diagnostic (toggleable via env var).
    # Used to verify the fix on 2026-04-29 against scripts/cache_smoke_test.py;
    # leaving in as a debugging hook for future cache regressions.
    import os as _os
    _DEBUG = bool(_os.environ.get("SOKRATIC_CACHE_DEBUG"))

    if stable:
        b1: dict = {"type": "text", "text": stable}
        est_stable = _estimate_tokens(stable)
        if est_stable >= cache_min_tokens:
            b1["cache_control"] = {"type": "ephemeral"}
        if _DEBUG:
            print(
                f"  [_cached_system] Block 1 stable: est_tokens={est_stable} "
                f"(role={_estimate_tokens(role_base)}, "
                f"wrapper={_estimate_tokens(wrapper_delta)}, "
                f"chunks={_estimate_tokens(chunks)}) "
                f"cached={'cache_control' in b1}",
                flush=True,
            )
        blocks.append(b1)

    if history_rendered:
        b2: dict = {"type": "text", "text": history_rendered}
        est_hist = _estimate_tokens(history_rendered)
        if est_hist >= cache_min_tokens:
            b2["cache_control"] = {"type": "ephemeral"}
        if _DEBUG:
            print(
                f"  [_cached_system] Block 2 history: est_tokens={est_hist} "
                f"cached={'cache_control' in b2}",
                flush=True,
            )
        blocks.append(b2)

    if turn_deltas:
        blocks.append({"type": "text", "text": turn_deltas})

    return blocks


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate for debug visibility (~4 chars/token)."""
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def _timed_create(client, state: dict, wrapper_name: str, **kwargs):
    """Wrapper around client.messages.create that records timing, cost, and full
    prompt/response into turn_trace for debug UI."""
    # Extract full prompt for debug before sending
    system_blocks = kwargs.get("system", [])
    system_text = ""
    if isinstance(system_blocks, list):
        system_text = "\n\n---\n\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in system_blocks
        )
    elif isinstance(system_blocks, str):
        system_text = system_blocks

    messages_sent = kwargs.get("messages", [])
    input_hash = hashlib.sha256(
        (system_text + "\n\n" + json.dumps(messages_sent, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()[:16]
    system_block_debug = []
    cached_est_tokens = 0
    if isinstance(system_blocks, list):
        for idx, block in enumerate(system_blocks, start=1):
            if not isinstance(block, dict):
                continue
            text = str(block.get("text", "") or "")
            est = _estimate_tokens(text)
            cached = bool(block.get("cache_control"))
            if cached:
                cached_est_tokens += est
            system_block_debug.append({
                "index": idx,
                "cached": cached,
                "est_tokens": est,
            })

    t0 = time.time()
    from conversation.llm_client import beta_headers
    extra_headers = kwargs.pop("extra_headers", {})
    # beta_headers() returns {"anthropic-beta": "..."} for Direct API,
    # empty dict for Bedrock (which rejects the header).
    extra_headers.update(beta_headers())
    resp = client.messages.create(extra_headers=extra_headers, **kwargs)
    elapsed = time.time() - t0

    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
    cost = (
        in_tok * _PRICE_IN
        + cache_read * _PRICE_IN * 0.1
        + cache_write * _PRICE_IN * 1.25
        + out_tok * _PRICE_OUT
    ) / 1_000_000
    tpt = elapsed / out_tok if out_tok > 0 else 0.0

    # Extract response text and tool calls for debug
    response_text = ""
    tool_calls = []
    for block in resp.content:
        if hasattr(block, "text"):
            response_text += block.text
        elif block.type == "tool_use":
            tool_calls.append({"name": block.name, "input": block.input})

    state["debug"]["api_calls"] += 1
    state["debug"]["input_tokens"] += in_tok
    state["debug"]["output_tokens"] += out_tok
    state["debug"]["cost_usd"] = float(state["debug"].get("cost_usd", 0.0)) + cost
    state["debug"]["turn_trace"].append({
        "wrapper": wrapper_name,
        "decision_effect": None,
        "input_hash": input_hash,
        "elapsed_s": round(elapsed, 2),
        "in_tok": in_tok,
        "out_tok": out_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": round(cost, 5),
        "s_per_tok": round(tpt, 4),
        "cached_est_tokens": cached_est_tokens,
        "system_blocks": system_block_debug,
        # Full content for debug UI
        "system_prompt": system_text,
        "messages_sent": messages_sent,
        "response_text": response_text,
        "tool_calls_made": tool_calls,
    })
    return resp


def _request_fingerprint(system_blocks, messages) -> str:
    """Stable fingerprint for duplicate-call guard on expensive wrappers."""
    if isinstance(system_blocks, list):
        system_text = "\n\n---\n\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in system_blocks
        )
    else:
        system_text = str(system_blocks or "")
    payload = system_text + "\n\n" + json.dumps(messages or [], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


# Stop words dropped when computing content-token overlap for the
# reached-answer gate. Kept conservative: only true filler tokens that
# don't carry meaning in answer phrases. We want "the skeletal muscle pump"
# in a student message to match locked_answer="skeletal muscle pump",
# but NOT match locked_answer="muscle" alone (single-token overlap is
# usually too weak — handled by requiring the full content set).
_OVERLAP_STOPWORDS = frozenset({
    "a", "an", "the", "of", "is", "are", "and", "or", "to",
    "in", "on", "at", "for", "by", "with", "from",
})

# Phrases that signal hedging / asking / denying rather than asserting.
# When any appear, Step A (token overlap) is skipped and we fall through
# to the LLM paraphrase check, which can read intent more reliably.
# Examples this catches:
#   "I don't know what skeletal muscle pump is" → tokens overlap but
#       the assertion isn't there → fall to LLM, which says no.
#   "Is it gravity?" → "gravity" doesn't overlap anyway, but if locked
#       answer were "gravity" we'd correctly defer to LLM.
_HEDGE_MARKERS = (
    "i don't know", "i dont know", "i do not know",
    "no idea", "not sure", "not certain",
    "i'm lost", "im lost", "idk", "i forget", "no clue", "not really",
    "i can't remember", "i cant remember", "can't remember",
)


def _content_tokens(s: str) -> list[str]:
    """Lowercased, punct-stripped tokens of `s` minus filler stopwords."""
    norm = _normalize_text(s)
    return [t for t in norm.split() if t and t not in _OVERLAP_STOPWORDS]


def _has_hedge(msg_lower_raw: str) -> bool:
    """True if the message contains any hedge/denial marker."""
    return any(h in msg_lower_raw for h in _HEDGE_MARKERS)


def _split_locked_answer(answer: str) -> list[str]:
    """Split a multi-component locked_answer into its top-level noun-phrase
    components. Single-component answers return a 1-element list.

    Splits on conjunctions/separators commonly used to enumerate components:
      - " and " / " or " (with surrounding spaces — won't split "android")
      - "," / ";" with optional whitespace

    Examples:
      "skeletal muscle pump"
        -> ["skeletal muscle pump"]                            (single)
      "left and right coronary arteries"
        -> ["left", "right coronary arteries"]                 (2-component)
      "ingestion, propulsion, mechanical digestion, chemical digestion"
        -> ["ingestion", "propulsion", "mechanical digestion",
            "chemical digestion"]                              (4-component)
      "pivot, hinge, condyloid, saddle, plane, ball-and-socket"
        -> ["pivot", "hinge", "condyloid", "saddle", "plane",
            "ball-and-socket"]                                 (6-component)

    Note: 'ball-and-socket' is hyphenated so it's treated as one token,
    not split by "and". Splitter operates on whitespace-padded "and" only.
    """
    if not answer or not answer.strip():
        return []
    # Lowercase for splitting; preserve original casing per component is
    # not needed downstream (token-overlap uses _content_tokens which
    # lowercases anyway).
    parts = re.split(r"\s+and\s+|\s+or\s+|[,;]\s*", answer.lower())
    parts = [p.strip() for p in parts if p.strip()]
    return parts


# Common short anatomy/biology nouns that frequently appear in legitimate
# Socratic scaffolding ("the heart muscle...", "what nerve innervates...")
# and would false-positive a single-word anchor leak check. When the
# locked_answer is one of these, we don't block the teacher from
# mentioning it — too high a false-positive rate.
_COMMON_ANCHOR_FALSE_POSITIVES = frozenset({
    # Generic anatomy
    "muscle", "nerve", "bone", "artery", "vein", "vessel", "tissue",
    "organ", "cell", "wall", "layer", "cavity", "chamber", "fluid",
    "blood", "skin", "joint", "system", "region", "branch", "trunk",
    # Generic biology
    "protein", "enzyme", "hormone", "molecule", "structure", "function",
    # Generic descriptors that might end up as a one-word anchor
    "left", "right", "anterior", "posterior", "superior", "inferior",
    "medial", "lateral", "deep", "central", "peripheral",
})


# Curated short clinical / anatomical / biological abbreviations that are
# DISTINCTIVE despite being below the 5-char floor. Without this set,
# `_is_distinctive_anchor` rejects them (e.g. "sa", "rca", "atp") because
# they're too short — but they're real anchors that need leak-protection.
# Lowercased; matched after the caller's lowercase normalization.
_DISTINCTIVE_SHORT_ABBREVIATIONS = frozenset({
    # Cardiac
    "sa", "av", "rca", "lca", "lad", "rcx", "lcx", "pda", "ivc", "svc",
    # Neuro
    "cn", "cns", "pns", "rem", "csf", "bbb",
    # Biochem
    "atp", "adp", "amp", "gtp", "gdp", "nad", "fad", "coa", "udp",
    "dna", "rna", "mrna", "trna", "rrna",
    "fc", "fab", "ig", "iga", "igg", "igm", "ige", "igd",
    # Lipids & blood
    "ldl", "hdl", "vldl", "rbc", "wbc", "cbc", "hb", "abg",
    # Endocrine
    "tsh", "fsh", "lh", "acth", "adh", "gh", "prl",
    # Imaging / instruments
    "ekg", "ecg", "eeg", "mri", "ct", "pet",
    # Other clinical
    "icu", "er", "or", "ot", "pt",
})


def _is_distinctive_anchor(token: str) -> bool:
    """True iff a single-word anchor is distinctive enough to safely
    block as a leak. Multi-word anchors are always considered distinctive
    (callers should special-case the multi-word path).

    Rules (any one suffices):
      1. Multi-word phrase (always distinctive).
      2. ≥5 chars AND not in the common-anatomy/biology stopword set.
         Catches: nucleus, ganglion, pepsin, septum, nephron, alveolus,
         hepatocyte, axillary, deltoid, etc.
      3. ALL-CAPS short token (2-4 chars), case preserved (e.g. "SA",
         "RCA", "ATP", "Fc"). Detected on raw input before lowercasing.
      4. Lowercased token in the curated short-abbreviation set
         (closes the gap when caller already lowercased).

    Skips: muscle, nerve, vein, bone, left, right (would false-positive).
    """
    raw = (token or "").strip()
    if not raw:
        return False
    parts_raw = raw.split()
    if len(parts_raw) >= 2:
        return True  # multi-word phrases are always distinctive enough
    word_raw = parts_raw[0]
    word = word_raw.lower()
    # Standard ≥5-char path
    if len(word) >= 5 and word not in _COMMON_ANCHOR_FALSE_POSITIVES:
        return True
    # ALL-CAPS short abbreviation — preserve original case detection
    if 2 <= len(word_raw) <= 4 and word_raw.isupper():
        return True
    # Curated short-abbreviation set (case-insensitive)
    if word in _DISTINCTIVE_SHORT_ABBREVIATIONS:
        return True
    return False


# 2026-05-01: _LETTER_HINT_PATTERNS + _has_letter_hint were the regex
# detector for letter / blank / etymology / MCQ / synonym / acronym
# leaks at hint-3. Per the user's "LLM-only QC" directive, replaced by
# conversation/classifiers.haiku_hint_leak_check, which reads the draft
# + locked_answer + aliases and returns a verdict. Validated 96.7% /
# 100% leak precision on 30 hand-curated cases (see
# data/artifacts/classifiers/2026-05-01T21-03-51/report.md). The
# classifier catches novel phrasings the regex didn't list ("the
# textbook uses a word starting with the letter f", "the medical word
# for funny bone is...") and avoids false-firing on legitimate
# Socratic scaffolding ("describe what property lets these cells...").

def _sentence_count(text: str) -> int:
    parts = [p.strip() for p in re.split(r"[.!?]+", text or "") if p.strip()]
    return len(parts)


def _question_count(text: str) -> int:
    return (text or "").count("?")


def _extract_question_text(text: str) -> str:
    """Return the final question-like segment for repetition checks."""
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"\?", text) if p.strip()]
    if not parts:
        return ""
    return parts[-1]


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    parts = re.split(r"[.!?]+", text, maxsplit=1)
    return (parts[0] or "").strip()


# 2026-05-01: _has_strong_affirmation was the regex sycophancy
# detector. Replaced by conversation/classifiers.haiku_sycophancy_check
# which is state-aware (only flags affirmation when student wasn't
# actually correct). Validated 100% accuracy on 27 cases.


def _recent_tutor_questions(messages: list[dict], limit: int = 3) -> list[str]:
    questions: list[str] = []
    for msg in reversed(messages or []):
        if msg.get("role") != "tutor":
            continue
        content = msg.get("content", "")
        if "?" not in content:
            continue
        q = _extract_question_text(content)
        if q:
            questions.append(q)
        if len(questions) >= limit:
            break
    return questions


def _is_repetitive_question(new_question: str, prior_questions: list[str], threshold: float) -> bool:
    a = _normalize_text(new_question)
    if not a:
        return False
    for q in prior_questions:
        b = _normalize_text(q)
        if not b:
            continue
        if SequenceMatcher(a=a, b=b).ratio() >= threshold:
            return True
    return False


def _extract_json_object(text: str) -> dict | None:
    """
    Robustly extract a JSON object from model text.
    Handles:
    - raw JSON
    - fenced JSON blocks
    - leading/trailing prose around JSON
    - python-dict style fallbacks when model slips
    """
    if not text:
        return None

    def _normalize_structured(s: str) -> str:
        s = (s or "").strip()
        s = s.replace("\u201c", "\"").replace("\u201d", "\"")
        s = s.replace("\u2018", "'").replace("\u2019", "'")
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
        return s.strip()

    def _remove_trailing_commas(s: str) -> str:
        return re.sub(r",(\s*[}\]])", r"\1", s)

    def _try_parse_dict(s: str) -> dict | None:
        if not s:
            return None
        candidates = [s, _remove_trailing_commas(s)]
        for cand in candidates:
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


def _latest_student_message(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "student":
            return str(msg.get("content", "")).strip()
    return ""


def _sanitize_locked_answer(
    candidate: str,
    chunks: list[dict],
    prior: str = "",
) -> tuple[str, str]:
    """
    Validate the locked_answer:
      1) shape: short noun phrase, not a sentence
      2) GROUNDING: the answer's content tokens must appear in at least one
         retrieved chunk's text. The lock-anchors prompt asks the LLM to
         ground the answer in propositions, but Haiku/Sonnet have parametric
         knowledge of anatomy and will produce textbook-correct answers
         (e.g. "axillary nerve" for a deltoid-innervation question) even when
         retrieval did not surface the supporting content. That's a real bug
         observed end-to-end (S4b conversation 2026-04-29: tutor declared
         "axillary nerve" locked despite retrieval returning chunks about
         the axillary artery, not nerve). The grounding check is a sentinel
         against that failure mode.
    """
    cand = (candidate or "").strip()
    if not cand:
        return (prior or "").strip(), "empty_input"

    cand_norm = _normalize_text(cand)
    prior_norm = _normalize_text(prior or "")

    # locked_answer must be a canonical noun phrase, NOT a sentence. Sentences
    # are caught by `sentence_markers` below. The word-count cap is a separate
    # guard against runaway descriptions. The original cap of 6 was tuned for
    # the proposition-era pipeline when answers were single anatomical terms
    # ("axillary nerve"). Some legitimate textbook answers ARE multi-component
    # lists — e.g. "Pivot, hinge, condyloid, saddle, plane, ball-and-socket"
    # for the 6 types of synovial joints, or "Bones as levers, synovial joints
    # as fulcrums, muscle contraction as effort, load as resistance" for the 4
    # components of a musculoskeletal lever system. The 6-word cap rejected
    # these as "too long" (verified 2026-04-29 via dean.py debug trace), which
    # broke the topic-engagement gate for any list-shaped topic. Relaxed to
    # 15 to allow comma-separated noun lists; sentence detection below still
    # catches actual prose.
    word_count = len(cand_norm.split())
    if word_count > 15:
        return prior_norm, "wiped_too_long"
    # Reject "and"-joined anchors. The lock prompt explicitly forbids this
    # ("no 'and' joining multiple ideas") because the gate is a single-utterance
    # token-overlap matcher — joined anchors like "left and right coronary
    # arteries" never match what students actually say. The repair path picks
    # up an empty result and re-prompts for a single umbrella term plus
    # per-component aliases. Note: we test for ' and ' with spaces, so the
    # hyphenated "ball-and-socket" anatomical term isn't false-positive.
    if " and " in cand_norm:
        return prior_norm, "wiped_and_joined"
    # Also reject anchors that are clearly sentences (contain verbs like "innervates",
    # "arises", "branches", "passes", "courses", etc.), even if short enough.
    sentence_markers = (
        "innervates", "innervate", "arises", "branches", "passes", "courses",
        "supplies", "controls", "causes", "results", "from the", "through the",
        "superior to", "inferior to", "above the", "below the", "near the",
    )
    if any(marker in cand_norm for marker in sentence_markers):
        return prior_norm, "wiped_sentence_like"

    # Grounding check: does the answer's distinctive content (non-stopword
    # words >= 4 chars) appear in at least one retrieved chunk's text? We
    # require that >= 60% of the answer's content tokens be present in some
    # chunk to count as grounded. This blocks parametric-knowledge leaks
    # without rejecting every valid short noun phrase.
    if chunks:
        STOPS = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at",
                 "for", "with", "by", "from"}
        ans_tokens = [t for t in cand_norm.split()
                      if t not in STOPS and len(t) >= 4]
        if ans_tokens:
            joined_corpus = " ".join(
                _normalize_text(c.get("text", "") or "") for c in chunks
            )
            hits = sum(1 for t in ans_tokens if t in joined_corpus)
            if (hits / len(ans_tokens)) < 0.60:
                return prior_norm, "wiped_ungrounded"

    return cand_norm, "kept"


def _match_topic_selection(student_text: str, options: list[str]) -> str:
    """
    Match explicit student selection to one of the presented topic options.
    Accepted forms:
      - exact/pasted option text
      - unambiguous substring match against one option
    Returns selected option text or empty string when no explicit match.
    """
    txt = _normalize_text(student_text)
    if not txt or not options:
        return ""

    # Numeric / ordinal selection (e.g., "1", "option 2", "first one", "let's do #3").
    ordinals = {
        "first": 1, "1st": 1, "one": 1,
        "second": 2, "2nd": 2, "two": 2,
        "third": 3, "3rd": 3, "three": 3,
        "fourth": 4, "4th": 4, "four": 4,
    }
    import re as _re
    num_match = _re.search(r"\b([1-4])\b", txt)
    picked_idx = None
    if num_match:
        picked_idx = int(num_match.group(1))
    else:
        for word, idx in ordinals.items():
            if _re.search(rf"\b{word}\b", txt):
                picked_idx = idx
                break
    if picked_idx is not None and 1 <= picked_idx <= len(options):
        return options[picked_idx - 1]

    normalized_options = [(_normalize_text(o), o) for o in options]

    # Exact normalized text
    for norm, original in normalized_options:
        if txt == norm:
            return original

    # Unambiguous inclusion
    candidates = []
    for norm, original in normalized_options:
        if len(txt) >= 8 and txt in norm:
            candidates.append(original)
        elif len(norm) >= 8 and norm in txt:
            candidates.append(original)

    uniq = []
    seen = set()
    for c in candidates:
        key = _normalize_text(c)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    if len(uniq) == 1:
        return uniq[0]

    return ""


def _clean_retrieval_query(text: str) -> str:
    """
    Keep retrieval input concise and semantic.
    - remove numbering artifacts ("2.", "3)")
    - trim low-information hedges
    - collapse whitespace
    """
    q = (text or "").strip()
    if not q:
        return ""
    q = re.sub(_RETRIEVAL_NOISE_PATTERNS[0], "", q)
    q = re.sub(_RETRIEVAL_NOISE_PATTERNS[1], "", q, flags=re.IGNORECASE)
    q = re.sub(_RETRIEVAL_NOISE_PATTERNS[2], " ", q).strip()
    if len(q) > 280:
        q = q[:280].rsplit(" ", 1)[0].strip()
    return q


def _build_retrieval_query(state: TutorState) -> str:
    """
    Build a retrieval query from the locked topic + latest student message.
    By the time we reach retrieval, a TOC-grounded topic lock is guaranteed,
    so the topic string alone is always a valid query. The latest message is
    blended in only when it adds fresh substantive detail.
    """
    topic = str(state.get("topic_selection", "") or "").strip()
    latest = _latest_student_message(state.get("messages", []))

    candidate = topic or latest
    topic_norm = _normalize_text(topic)
    latest_norm = _normalize_text(latest)
    # Skip the topic+latest blend when the latest message is the same as topic
    # (e.g. right after a card pick `_replace_latest_student_message` mirrors the
    # card text into the last student message) — otherwise the blended query
    # becomes "{x}. {x}" which inflates length and trips the OOD gate.
    if (
        topic
        and latest
        and len(latest.split()) >= 3
        and latest_norm != topic_norm
        and topic_norm not in latest_norm
        and latest_norm not in topic_norm
    ):
        # Keep topic anchor but preserve fresh student intent/detail.
        candidate = f"{topic}. {latest}"

    return _clean_retrieval_query(candidate)


def _retrieval_trace_payload(query: str, chunks: list[dict], top_n: int = 7) -> dict:
    top = []
    for i, c in enumerate(chunks[:top_n], start=1):
        top.append({
            "rank": i,
            "score": float(c.get("score", 0.0) or 0.0),
            "section_title": c.get("section_title", ""),
            "subsection_title": c.get("subsection_title", ""),
            "chunk_id": c.get("chunk_id", ""),
            "text_preview": str(c.get("text", "") or "")[:240],
        })
    return {
        "query": query,
        "top_chunks": top,
        "total_chunks_returned": len(chunks),
    }


def _coverage_gate(state: TutorState, retriever=None) -> dict | None:
    """
    Check whether retrieved_chunks actually cover the locked TOC node.

    Args:
        state: tutor state.
        retriever: optional retriever for picking SEMANTICALLY-RELATED
            alternative cards via sample_related. Without it, falls back
            to sample_diverse (random teachable picks).

    Returns None on pass. On fail, returns metadata for the caller (run_turn)
    to build a refuse turn with an LLM-authored intro + card list. Shape:
      {reason, topic_label, options, pending_user_choice, rejected_path,
       failure_count}

    Rules:
      1. Empty retrieval → fail hard.
      2. Locked topic has a known section/subsection AND none of the top-5
         chunks reference it → fail (retrieval drifted to another chapter).
      3. Top chunk cosine below ood_cosine_threshold → fail.
    """
    chunks = state.get("retrieved_chunks", []) or []
    locked = state.get("locked_topic") or {}
    topic_label = locked.get("subsection") or locked.get("section") or state.get("topic_selection", "this topic")
    rejected_paths = list(state.get("rejected_topic_paths", []) or [])
    rejected_set = set(rejected_paths)
    if locked.get("path"):
        rejected_set.add(locked["path"])

    def _refuse(reason: str) -> dict:
        matcher = get_topic_matcher()
        failure_count = len(rejected_set)
        min_chunks = 5 if failure_count < 2 else 8
        # Prefer SEMANTICALLY-RELATED alternatives via sample_related when
        # we have a retriever. Falls back to sample_diverse if retriever
        # is unavailable or returns nothing related. Without this,
        # students typing "brain" got cards like "DNA Replication" —
        # technically teachable but unrelated to the query.
        query_for_related = (
            state.get("topic_selection")
            or topic_label
            or _latest_student_message(state.get("messages", []))
            or ""
        )
        if retriever is not None and query_for_related:
            alternatives = matcher.sample_related(
                retriever, query_for_related, 3,
                min_chunk_count=min_chunks, exclude_paths=rejected_set,
            )
        else:
            alternatives = matcher.sample_diverse(
                3, min_chunk_count=min_chunks, exclude_paths=rejected_set,
            )
        option_labels = [_format_topic_label(m) for m in alternatives]
        option_meta = {
            label: {
                "path": m.path, "chapter": m.chapter, "section": m.section,
                "subsection": m.subsection, "difficulty": m.difficulty,
                "chunk_count": m.chunk_count, "limited": m.limited, "score": m.score,
            }
            for label, m in zip(option_labels, alternatives)
        }
        return {
            "reason": reason,
            "topic_label": topic_label,
            "failure_count": failure_count,
            "options": option_labels,
            "rejected_path": locked.get("path") or "",
            "pending_user_choice": {
                "kind": "topic", "options": option_labels, "topic_meta": option_meta,
            },
        }

    if not chunks:
        # Empty after hard section filter → this TOC node has no indexable
        # chunks in the current corpus. Refuse with alternatives.
        return _refuse("empty_retrieval")

    # Relevance floor via CE score. The CE returns near-1 for confident
    # in-scope hits and near-0 for OOD; intermediate values indicate a query
    # that is in-corpus but not perfectly answered by any single chunk
    # (typical for relational questions like "what nerve innervates X?" where
    # the chunks mention X in many contexts but only one actually states the
    # nerve). We want to PASS those queries through to the LLM, not refuse
    # them — so the gate uses a dedicated threshold (`dean_topic_gate_ce_threshold`)
    # that is much lower than the OOD cosine floor used by the retriever's
    # in-scope check. Default 0.05 — virtually any non-zero CE score passes.
    #
    # 2026-04-29: previously this gate read `ood_cosine_threshold` (0.45) and
    # refused valid topics like "what nerve innervates the deltoid?" on the
    # basis of CE score 0.20-0.40 — see e2e S6a where the deltoid topic was
    # refused and the conversation pivoted to "muscle tone".
    threshold = float(getattr(cfg.retrieval, "dean_topic_gate_ce_threshold", 0.05))
    top_score = chunks[0].get("score")
    if isinstance(top_score, (int, float)) and float(top_score) < threshold:
        return _refuse("low_relevance_score")

    return None


def _format_topic_label(m: TopicMatch) -> str:
    """Card label for a TOC match — human-readable, with a limited-coverage tag."""
    base = m.label
    if m.limited:
        return f"{base} · limited coverage"
    return base


def _replace_latest_student_message(messages: list[dict], new_content: str) -> list[dict]:
    """
    Replace latest student message content so downstream retrieval/classification
    runs on the selected topic text (instead of a numeric reply like '2').
    """
    patched = list(messages or [])
    for i in range(len(patched) - 1, -1, -1):
        msg = patched[i]
        if msg.get("role") == "student":
            new_msg = dict(msg)
            new_msg["content"] = new_content
            patched[i] = new_msg
            break
    return patched


class DeanAgent:
    def __init__(self, retriever, memory_client):
        """
        Args:
            retriever:      Retriever (or MockRetriever) instance
            memory_client:  PersistentMemory instance (memory/persistent_memory.py)
        Note: no embed_fn — correctness checking done by LLM, not cosine similarity.
        """
        from conversation.llm_client import make_anthropic_client, resolve_model
        self.client = make_anthropic_client()
        self.model = resolve_model(cfg.models.dean)
        self.retriever = retriever
        self.memory_client = memory_client
        save_tool_definitions()

    def run_turn(self, state: TutorState, teacher) -> dict:
        """
        Orchestrate one full Dean turn. Called by dean_node in nodes.py.

        Flow:
          1. _setup_call(state) → eval dict
          2. If student_reached_answer → return partial state (assessment handles rest)
          3. Help abuse gating (Python counter)
          4. teacher.draft_socratic(state) → draft
          5. _quality_check_call(state, draft) → {pass, critique, leak_detected}
          6. PASS → approved_response = draft
             FAIL → use Dean-proposed revised_teacher_draft when valid (single-pass repair)
             FAIL with no valid revision → Dean-authored fallback, log intervention
          7. Append {"role": "tutor", "content": approved_response} to state["messages"]
          8. Return partial state update dict

        Returns:
            dict with updated state fields (merged by dean_node)
        """
        # ── Topic engagement gate (explicit selection required) ───────────────
        # Until topic_confirmed=True, tutoring does not start.
        # 1) First pass: generate 3-4 scoped options.
        # 2) Subsequent passes: require explicit selection by number/text.
        topic_just_locked = False
        if state.get("topic_confirmed", False) and state.get("topic_options"):
            # topic_confirmed is monotonic in a session; clear any stale options.
            state["topic_options"] = []
            state["topic_question"] = ""
            state["pending_user_choice"] = {}

        if not state.get("topic_confirmed", False):
            from conversation.teacher import fire_activity as _fire_activity_pre
            _fire_activity_pre("Reading your message")

            messages = list(state.get("messages", []))
            topic_options = list(state.get("topic_options", []))
            latest_student = _latest_student_message(messages)

            # LLM intent classifier — replaces the old regex-based low-effort /
            # non-topic / ambiguity detectors. Returns {intent, normalized_topic,
            # tutor_reply, rationale}.
            _fire_activity_pre("Understanding your intent")
            intent_result = self._prelock_intent_call(state)
            intent = intent_result["intent"]
            normalized_topic = intent_result["normalized_topic"] or latest_student
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.prelock_intent",
                "intent": intent,
                "normalized_topic": normalized_topic,
                "rationale": intent_result["rationale"],
            })

            # Non-topic intents: LLM-authored reply, no routing through matcher.
            # Preserve any cards currently on screen so the student can still pick.
            if intent in {"greeting", "distress", "off_topic", "ambiguous"}:
                reply = intent_result["tutor_reply"] or (
                    "Please name a specific study topic to begin."
                )
                messages.append({"role": "tutor", "content": reply})
                state["hint_level"] = 0
                return {
                    "messages": messages,
                    "topic_confirmed": False,
                    "topic_options": topic_options,
                    "topic_question": state.get("topic_question", ""),
                    "topic_selection": "",
                    "pending_user_choice": state.get("pending_user_choice", {}),
                    "retrieved_chunks": [],
                    "locked_question": "",
                    "locked_answer": "",
                    "hint_level": 0,
                    "student_state": "question",
                    "debug": state["debug"],
                }

            # Card pick: try deterministic match on both raw and normalized forms.
            picked_topic: TopicMatch | None = None
            if intent == "card_pick" and topic_options:
                option_to_topic = (state.get("pending_user_choice") or {}).get("topic_meta", {}) or {}
                selected_label = (
                    _match_topic_selection(latest_student, topic_options)
                    or _match_topic_selection(normalized_topic, topic_options)
                )
                if selected_label:
                    entry_dict = option_to_topic.get(selected_label)
                    if entry_dict:
                        picked_topic = TopicMatch(
                            path=entry_dict["path"],
                            chapter=entry_dict["chapter"],
                            section=entry_dict["section"],
                            subsection=entry_dict["subsection"],
                            difficulty=entry_dict.get("difficulty", "moderate"),
                            chunk_count=int(entry_dict.get("chunk_count", 0)),
                            limited=bool(entry_dict.get("limited", False)),
                            score=float(entry_dict.get("score", 100.0)),
                        )

            # Free-text topic query (or a card_pick that couldn't resolve) →
            # SEMANTIC topic resolution via the chunks retriever.
            #
            # 2026-04-29: replaced the rapidfuzz token_set_ratio TopicMatcher
            # for free-text resolution. Empirically, fuzzy string matching of
            # natural student queries against TOC titles is not reliable: for
            # "What are the structural and functional differences between T
            # helper cells and cytotoxic T cells?" the matcher ranked "Stem
            # Cells" (token-set 67) above "T Cell Types and their Functions"
            # (43) because the latter title doesn't share the plural "Cells"
            # token. The chunks retriever uses dense embeddings + BM25 + CE
            # rerank — it gets this right (returns Ch21 T-cell content) — so
            # we use it directly to identify the topic and only fall back to
            # the title matcher when the retriever cannot.
            if picked_topic is None:
                # Prefer the RAW student message over the LLM-normalized
                # topic for retrieval. Why: normalization is lossy — it
                # collapses a sentence like "What are the structural and
                # functional distinctions between superior and inferior
                # venae cavae, and how do their tributary patterns differ?"
                # to "systemic veins and venae cavae", which retrieves a
                # smaller, differently-ranked chunk set (heart-anatomy-
                # biased) than the full sentence does. The full message
                # carries more keyword signal and lets the vote-based
                # resolution converge on the correct TOC node.
                #
                # We still fall back to normalized_topic (a) when latest
                # student message is empty/whitespace, and (b) for the
                # fuzzy matcher below — short normalized strings are what
                # rapidfuzz title-matching is tuned for.
                latest_clean = (latest_student or "").strip()
                query_for_match = latest_clean or normalized_topic
                fuzzy_query = normalized_topic or latest_clean
                semantic_top: TopicMatch | None = None
                _fire_activity_pre("Searching textbook for the topic")
                try:
                    sem_chunks = self.retriever.retrieve(query_for_match)
                except Exception as _e:
                    sem_chunks = []
                # Resolve topic by rank-weighted VOTE across top primaries
                # rather than picking primaries[0]. Why: cross-encoder scores
                # saturate at 1.000 in the high-relevance regime, so when 4
                # out of 5 top primaries agree on (chapter, section,
                # subsection) and 1 disagrees, the disagreeing one will
                # often appear at rank 0 by sort-noise — and primaries[0]
                # would pick the WRONG topic.
                #
                # Concrete failure observed (2026-04-29):
                #   Query: "What are the structural and functional
                #     distinctions between the superior and inferior venae
                #     cavae, and how do their tributary patterns differ?"
                #   primaries[0] = Ch19 Heart: Heart Defects     (score 1.0)
                #   primaries[1..4] = Ch20 Overview of Systemic Veins (1.0)
                #   → top-1 picks Heart Defects (wrong) but 4-of-5 vote
                #     correctly resolves to Systemic Veins.
                #
                # Vote weighting: 1/(rank+1) so earlier results count more
                # but a single rank-0 outlier can be overridden by 2-3
                # consistent results at ranks 1-3.
                primaries = [c for c in sem_chunks
                             if c.get("_window_role", "primary") == "primary"]
                topic_gate = float(
                    getattr(cfg.retrieval, "dean_topic_gate_ce_threshold", 0.05)
                )
                # Only consider primaries that pass the relevance gate.
                # Most production queries hit saturation (score=1.0) so
                # the gate filters out very weak retrievals (e.g. 1-word
                # queries that match nothing well).
                eligible = [
                    (i, c) for i, c in enumerate(primaries[:5])
                    if float(c.get("score", 0.0)) >= topic_gate
                ]
                if eligible:
                    weights: dict[tuple, float] = {}
                    rep: dict[tuple, dict] = {}  # representative chunk per path
                    for rank, c in eligible:
                        path_key = (
                            c.get("chapter_num", 0),
                            c.get("section_title", "") or "",
                            c.get("subsection_title", "") or "",
                        )
                        weights[path_key] = weights.get(path_key, 0.0) + 1.0 / (rank + 1)
                        # Keep first chunk seen for each path as the
                        # representative — its metadata fills TopicMatch.
                        if path_key not in rep:
                            rep[path_key] = c
                    best_path = max(weights, key=weights.get)
                    chosen = rep[best_path]
                    semantic_top = TopicMatch(
                        path=f"Ch{chosen.get('chapter_num', 0)}|"
                             f"{chosen.get('section_title', '')}|"
                             f"{chosen.get('subsection_title', '')}",
                        chapter=str(chosen.get("chapter_title", "")),
                        section=str(chosen.get("section_title", "")),
                        subsection=str(chosen.get("subsection_title", "")),
                        difficulty="moderate",
                        chunk_count=0,
                        limited=False,
                        teachable=True,
                        score=float(chosen.get("score", 1.0)),
                    )
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.topic_vote",
                        "n_eligible": len(eligible),
                        "winner_path": semantic_top.path,
                        "winner_weight": round(weights[best_path], 3),
                        "all_weights": {
                            f"Ch{p[0]}|{p[1]}|{p[2]}": round(w, 3)
                            for p, w in sorted(
                                weights.items(), key=lambda x: -x[1]
                            )[:5]
                        },
                    })

                # Fallback: if the retriever couldn't resolve, run the legacy
                # fuzzy-title matcher (with relaxed thresholds) so the
                # rejection-with-alternatives path still works for ambiguous
                # one-word queries like "joints" where retrieval has nothing
                # specific to lock on.
                # Fuzzy title matcher uses the (typically short) normalized
                # topic if available — rapidfuzz works better against
                # tight strings than full sentences.
                matcher = get_topic_matcher()
                result: MatchResult = matcher.match(fuzzy_query)
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.topic_match",
                    "retrieval_query": query_for_match[:200],
                    "fuzzy_query": fuzzy_query[:200],
                    "tier": result.tier,
                    "top_score": result.top.score if result.top else 0.0,
                    "top_path": result.top.path if result.top else "",
                    "semantic_resolved": bool(semantic_top),
                    "semantic_path": semantic_top.path if semantic_top else "",
                })

                # Optional topic-resolution trace (toggleable via env var).
                # Used to diagnose stuck-topic regressions; left in as a
                # debugging hook for future investigations.
                import os as _os_dbg
                if _os_dbg.environ.get("SOKRATIC_TOPIC_DEBUG"):
                    print(f"  [topic-resolve] query={query_for_match!r} "
                          f"semantic_top={'yes' if semantic_top else 'no'} "
                          f"semantic_path={(semantic_top.path if semantic_top else '')!r} "
                          f"fuzzy_tier={result.tier} "
                          f"fuzzy_top={(result.top.label if result.top else '')!r} "
                          f"fuzzy_score={(result.top.score if result.top else 0)}",
                          flush=True)

                # Vague-query gate (added 2026-05-01, refined for e2e):
                # If the user typed a SHORT vague query AND the fuzzy
                # matcher couldn't strongly confirm the topic, do NOT
                # commit to semantic_top — surface cards instead. The
                # semantic vote is too eager on broad words: typing
                # "brain" alone confidently picks "Requirements for Human
                # Life" because one chunk mentions brain cells needing
                # oxygen, even though the user clearly wants brain
                # anatomy. Show options.
                #
                # Refinement (2026-05-01 e2e bug A1): use the ORIGINAL
                # student message word count, not fuzzy_query (which is
                # the LLM-normalized condensed form like "heart"). A
                # query like "What part of the heart starts the
                # heartbeat?" (8 words) was being collapsed to "heart"
                # (1 word) by the intent classifier and then the gate
                # treated it as vague — showing random cards instead of
                # locking to Conduction System of the Heart. We now
                # require BOTH the original AND normalized forms to be
                # short; if the user typed a specific question, even
                # heavy normalization shouldn't trip the gate.
                fuzzy_q_words = len((fuzzy_query or "").split())
                latest_q_words = len((latest_student or "").split())
                vague_query = (
                    fuzzy_q_words < 3
                    and latest_q_words < 4
                    and result.tier != "strong"
                )
                if vague_query and semantic_top is not None:
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.vague_query_suppress_semantic_top",
                        "result": (
                            f"query={fuzzy_query!r} (words={fuzzy_q_words}, "
                            f"fuzzy_tier={result.tier}); semantic_top "
                            f"{semantic_top.path!r} suppressed in favor of cards"
                        ),
                    })
                    semantic_top = None

                if semantic_top is not None:
                    picked_topic = semantic_top
                elif result.tier == "strong" and result.top is not None:
                    picked_topic = result.top
                else:
                    rejected_set = set(state.get("rejected_topic_paths", []) or [])
                    if result.tier == "borderline":
                        candidates = [m for m in result.matches if m.path not in rejected_set][:3]
                        if not candidates:
                            # Prefer semantically related to the user's query
                            # over random teachable picks.
                            candidates = matcher.sample_related(
                                self.retriever, query_for_match, 3,
                                exclude_paths=rejected_set,
                            )
                        refuse_reason = "borderline_unresolved"
                    else:
                        # No-match path: query was too vague or off-domain.
                        # Use sample_related so the cards are at least
                        # semantically near the user's typed query (e.g.
                        # "brain" → cerebrum/brainstem, not random anatomy).
                        candidates = matcher.sample_related(
                            self.retriever, query_for_match, 3,
                            exclude_paths=rejected_set,
                        )
                        refuse_reason = "no_match"

                    new_options = [_format_topic_label(c) for c in candidates]
                    option_to_topic = {
                        label: {
                            "path": c.path,
                            "chapter": c.chapter,
                            "section": c.section,
                            "subsection": c.subsection,
                            "difficulty": c.difficulty,
                            "chunk_count": c.chunk_count,
                            "limited": c.limited,
                            "score": c.score,
                        }
                        for label, c in zip(new_options, candidates)
                    }
                    # LLM-authored refuse intro. Only the card list itself is
                    # rendered deterministically.
                    refuse = self._prelock_refuse_call(
                        state,
                        # Show the user-facing short topic name in the
                        # refuse copy, not the full sentence we used for
                        # retrieval. fuzzy_query is the cleaner short form.
                        rejected_topic=fuzzy_query,
                        failure_count=len(rejected_set),
                        refuse_reason=refuse_reason,
                    )
                    intro = refuse["tutor_reply"] or (
                        "Here are a few topics we cover — pick one or type a more specific term:"
                    )
                    numbered = "\n".join(
                        f"  {i+1}. {opt}" for i, opt in enumerate(new_options)
                    )
                    scoped_msg = f"{intro}\n\n{numbered}" if new_options else intro
                    messages.append({"role": "tutor", "content": scoped_msg})
                    return {
                        "messages": messages,
                        "topic_confirmed": False,
                        "topic_options": new_options,
                        "topic_question": intro,
                        "topic_selection": "",
                        "pending_user_choice": {
                            "kind": "topic",
                            "options": new_options,
                            "topic_meta": option_to_topic,
                        },
                        "retrieved_chunks": [],
                        "locked_question": "",
                        "locked_answer": "",
                        "hint_level": 0,
                        "student_state": None,
                        "debug": state["debug"],
                    }

            # Topic locked to a TOC node.
            #
            # We only OVERWRITE the latest student message with the
            # canonical TOC label when the message was a CARD PICK
            # ("1", "first option", "the second one"). Card-pick
            # responses are content-free shortcuts that downstream
            # rendering needs translated to the actual topic name.
            #
            # For free-text topic queries ("what is the function of the
            # SA node?") we KEEP the student's original phrasing in
            # the messages list. Two reasons:
            #   1) UX — the student's specific question stays visible
            #      in the chat transcript, not blanked out into the
            #      canonical label.
            #   2) Pedagogy — the teacher's first Socratic draft sees
            #      the actual question and can address it directly,
            #      instead of generating a generic foundational
            #      question for the whole subsection.
            # Topic resolution metadata (topic_selection, locked_topic,
            # debug.locked_topic_snapshot) still carries the canonical
            # label for retrieval / classification calls — those don't
            # need the rewrite.
            selected_label = picked_topic.label
            if intent == "card_pick":
                state["messages"] = _replace_latest_student_message(
                    messages, selected_label
                )
            state["topic_confirmed"] = True
            state["topic_selection"] = selected_label
            state["locked_topic"] = {
                "path": picked_topic.path,
                "chapter": picked_topic.chapter,
                "section": picked_topic.section,
                "subsection": picked_topic.subsection,
                "difficulty": picked_topic.difficulty,
                "chunk_count": picked_topic.chunk_count,
                "limited": picked_topic.limited,
                "score": picked_topic.score,
            }
            state["topic_options"] = []
            state["topic_question"] = ""
            state["pending_user_choice"] = {}
            # Re-fire retrieval whenever the topic changes. The earlier
            # "single-fire per session" optimisation kept chunks around even
            # when the student picked a different card after the coverage
            # gate refused — so the gate then ran against chunks for the
            # PREVIOUS topic and refused again, producing the runaway loop
            # observed in nidhi sessions 2026-04-30 (P0-A from the handoff).
            # Resetting retrieval_calls here lets _retrieve_on_topic_lock
            # fire again with the new card's section filter.
            state["retrieved_chunks"] = []
            if "debug" in state and isinstance(state["debug"], dict):
                state["debug"]["retrieval_calls"] = 0
            state["locked_question"] = ""
            state["locked_answer"] = ""
            state["hint_level"] = 0
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.topic_locked",
                "topic_path": picked_topic.path,
                "label": selected_label,
                "chunk_count": picked_topic.chunk_count,
                "limited": picked_topic.limited,
            })
            topic_just_locked = True
            # Persist the flag so the ack-emit branch (further down,
            # right before teacher.draft_socratic) sees it and produces
            # the deterministic "topic + question" message instead of a
            # paraphrased hint. The flag is consumed (set False) at
            # ack-emit time. Pre-locked sessions set this flag in
            # backend/api/session.py:_apply_prelock instead.
            state["topic_just_locked"] = True

        if topic_just_locked:
            _fire_activity_pre(f"Topic locked: {selected_label}")
            _fire_activity_pre("Loading textbook context")
            self._retrieve_on_topic_lock(state)

            # Strict-groundedness coverage gate: retrieval must return real,
            # in-section content for the locked TOC node. If it doesn't, we
            # refuse instead of teaching from parametric knowledge or from
            # chunks that drifted off-topic (this is what caused the
            # liver→spinal-cord bug in the 2026-04-21 live session).
            gate = _coverage_gate(state, retriever=self.retriever)
            if gate is not None:
                messages = list(state.get("messages", []))
                # Safety cap: after N=4 consecutive coverage-gate rejections
                # in the same session, abandon the card-rejection loop and
                # ask the student to freeform what they want to learn. This
                # is the P0-A "triple2_s1 runaway" fix from the handoff —
                # without it, students hit 14+ rejections before the session
                # ends. coverage_gap_events counts refusals across the whole
                # session, so we read its prospective post-increment value.
                cgap_after = int(state["debug"].get("coverage_gap_events", 0)) + 1
                rejected = list(state.get("rejected_topic_paths", []) or [])
                rejected_path = gate.get("rejected_path") or ""
                if rejected_path and rejected_path not in rejected:
                    rejected.append(rejected_path)
                if cgap_after >= 4:
                    state["debug"]["coverage_gap_events"] = cgap_after
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.coverage_gate_freeform_fallback",
                        "result": f"refused {cgap_after}x; switching to freeform",
                    })
                    fallback_msg = (
                        "I'm having trouble finding a strong textbook anchor for the "
                        "topics we've tried. Rather than keep cycling through cards, "
                        "let's go freeform: in your own words, what specifically about "
                        "anatomy do you want to work on right now? Be as concrete as "
                        "you can (a structure, a process, a clinical scenario)."
                    )
                    messages.append({"role": "tutor", "content": fallback_msg})
                    return {
                        "messages": messages,
                        "topic_confirmed": False,
                        "topic_options": [],
                        "topic_question": "",
                        "topic_selection": "",
                        "locked_topic": None,
                        "pending_user_choice": {},
                        "retrieved_chunks": [],
                        "locked_question": "",
                        "locked_answer": "",
                        "hint_level": 0,
                        "student_state": "question",
                        "rejected_topic_paths": rejected,
                        "debug": state["debug"],
                    }
                # LLM-authored intro; deterministic-render the card list.
                refuse = self._prelock_refuse_call(
                    state,
                    rejected_topic=gate.get("topic_label", "") or "",
                    failure_count=int(gate.get("failure_count", 0)),
                    refuse_reason=gate.get("reason", "") or "",
                )
                intro = refuse["tutor_reply"] or (
                    "Let's pick a topic with stronger coverage:"
                )
                option_labels = gate.get("options") or []
                numbered = "\n".join(
                    f"  {i+1}. {opt}" for i, opt in enumerate(option_labels)
                )
                msg = f"{intro}\n\n{numbered}" if option_labels else intro
                messages.append({"role": "tutor", "content": msg})
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.coverage_gate",
                    "result": gate["reason"],
                    "locked_topic": state.get("locked_topic"),
                })
                state["debug"]["coverage_gap_events"] = cgap_after
                return {
                    "messages": messages,
                    "topic_confirmed": False,
                    "topic_options": gate.get("options", []),
                    "topic_question": "",
                    "topic_selection": "",
                    "locked_topic": None,
                    "pending_user_choice": gate.get("pending_user_choice", {}),
                    "retrieved_chunks": [],
                    "locked_question": "",
                    "locked_answer": "",
                    "hint_level": 0,
                    "student_state": "question",
                    "rejected_topic_paths": rejected,
                    "debug": state["debug"],
                }

            _fire_activity_pre("Setting up the anchor question")
            anchors = self._lock_anchors_call(state)
            state["locked_question"] = str(anchors.get("locked_question", "") or "").strip()
            state["locked_answer"] = str(anchors.get("locked_answer", "") or "").strip()
            # Aliases consumed by reached_answer_gate Step A (token-overlap).
            # Sanitization already done inside _lock_anchors_call; just store.
            raw_aliases_out = anchors.get("locked_answer_aliases", []) or []
            state["locked_answer_aliases"] = (
                [str(a) for a in raw_aliases_out if isinstance(a, str) and str(a).strip()]
                if isinstance(raw_aliases_out, list) else []
            )
            # Two-tier (Change 2026-04-30): full_answer for grading layer.
            state["full_answer"] = str(anchors.get("full_answer", "") or "").strip() or state["locked_answer"]
            if not state["locked_question"] or not state["locked_answer"]:
                # Optional debug trace (toggleable via SOKRATIC_TOPIC_DEBUG env var).
                import os as _os_dbg
                if _os_dbg.environ.get("SOKRATIC_TOPIC_DEBUG"):
                    print(f"  [anchor-fail] locked_topic={state.get('locked_topic')} "
                          f"locked_question={state.get('locked_question')!r} "
                          f"locked_answer={state.get('locked_answer')!r} "
                          f"rationale={anchors.get('rationale','')!r}",
                          flush=True)
                anchor_fail_count = int(state.get("debug", {}).get("anchor_fail_count", 0)) + 1
                state["debug"]["anchor_fail_count"] = anchor_fail_count
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.anchor_extraction_failed",
                    "result": f"anchors empty (attempt {anchor_fail_count}) — topic too broad or retrieval weak",
                    "rationale": str(anchors.get("rationale", "") or ""),
                })
                # Anchor extraction failed — unlock the topic and show fresh
                # coverage-tested alternatives with an LLM-authored intro.
                # Tier 1 #1.4 fix (e2e bug A1+G1+B1): use sample_related
                # so the alternatives match what the student was actually
                # asking about, not random teachable picks. Without this,
                # "What part of the heart starts the heartbeat?" gets
                # alternatives like "Pulmonary Circulation / Large
                # Intestine / Sensory Pathways" — useless cards.
                messages = list(state.get("messages", []))
                matcher = get_topic_matcher()
                rejected_set = set(state.get("rejected_topic_paths", []) or [])
                failed_path = (state.get("locked_topic") or {}).get("path") or ""
                if failed_path:
                    rejected_set.add(failed_path)
                # Build a query for sample_related from the student's
                # original topic input — fall back to the latest student
                # message if topic_selection is empty.
                related_query = str(state.get("topic_selection") or "").strip()
                if not related_query:
                    for m in reversed(state.get("messages", []) or []):
                        if m.get("role") == "student":
                            related_query = str(m.get("content", "") or "").strip()
                            break
                if related_query and self.retriever is not None:
                    alternatives = matcher.sample_related(
                        self.retriever, related_query, n=3,
                        min_chunk_count=5, exclude_paths=rejected_set,
                    )
                else:
                    alternatives = matcher.sample_diverse(
                        3, min_chunk_count=5, exclude_paths=rejected_set,
                    )
                option_labels = [_format_topic_label(m) for m in alternatives]
                option_meta = {
                    label: {
                        "path": m.path, "chapter": m.chapter, "section": m.section,
                        "subsection": m.subsection, "difficulty": m.difficulty,
                        "chunk_count": m.chunk_count, "limited": m.limited, "score": m.score,
                    }
                    for label, m in zip(option_labels, alternatives)
                }
                fail = self._prelock_anchor_fail_call(
                    state, state.get("topic_selection", "") or ""
                )
                intro = fail["tutor_reply"] or (
                    "Let's try a different angle. Pick one of these or type a more specific term:"
                )
                numbered = "\n".join(
                    f"  {i+1}. {opt}" for i, opt in enumerate(option_labels)
                )
                msg = f"{intro}\n\n{numbered}" if option_labels else intro
                messages.append({"role": "tutor", "content": msg})
                rejected_list = list(state.get("rejected_topic_paths", []) or [])
                if failed_path and failed_path not in rejected_list:
                    rejected_list.append(failed_path)
                return {
                    "messages": messages,
                    "topic_confirmed": False,
                    "topic_options": option_labels,
                    "topic_question": intro,
                    "topic_selection": "",
                    "locked_topic": None,
                    "pending_user_choice": {
                        "kind": "topic",
                        "options": option_labels,
                        "topic_meta": option_meta,
                    },
                    "retrieved_chunks": [],
                    "locked_question": "",
                    "locked_answer": "",
                    "hint_level": 0,
                    "student_state": "question",
                    "rejected_topic_paths": rejected_list,
                    "debug": state["debug"],
                }
            # NOTE (2026-04-29 Change 2): we used to bump hint_level to 1
            # here so the immediate teacher.draft_socratic call would
            # render hint_plan[0]. With the new topic-acknowledgement flow,
            # the lock turn emits a deterministic ack instead of a hint —
            # so hint_level stays at 0 here. On the student's NEXT
            # incorrect attempt, the increment logic moves 0 → 1 (a single
            # step, see updated clamp below) and plan[0] fires as the
            # first scaffold. Keeping the bump would have skipped plan[0].
            # Sticky snapshot of the successful lock for the grading guard.
            # state.locked_topic can be transiently cleared by later partial
            # updates (e.g. a retry path); the snapshot is write-once and
            # survives those clears, so grading can trust it reached a grounded
            # state at least once this session.
            if state.get("locked_topic") and not state["debug"].get("locked_topic_snapshot"):
                state["debug"]["locked_topic_snapshot"] = dict(state["locked_topic"])
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.anchors_locked",
                "locked_question": state["locked_question"],
                "locked_answer": state["locked_answer"],
                "rationale": str(anchors.get("rationale", "") or ""),
            })
            _fire_activity_pre("Planning the hint progression")
            hint_plan = self._hint_plan_call(state)
            state["debug"]["hint_plan"] = hint_plan
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.hint_plan_initialized",
                "result": f"{len(hint_plan)} hints initialized",
                "hints": hint_plan,
            })
        elif state.get("topic_confirmed", False):
            # Recovery path: if topic is confirmed but retrieval is missing and answer isn't locked yet,
            # re-run retrieval on refined topic text.
            if not state.get("retrieved_chunks", []) and not state.get("locked_answer", ""):
                self._retrieve_on_topic_lock(state)

        # Bypass classification when the lock JUST happened — either in
        # this run_turn (free-text path, LOCAL=True) or from a pre-lock
        # helper that hasn't been consumed yet (state flag=True). Both
        # signals mean "the student's latest message is a topic pick or
        # kickstarter, not an answer attempt." Forcing student_state to
        # 'question' keeps the hint counter from advancing on the ack turn.
        if topic_just_locked or state.get("topic_just_locked", False):
            eval_result = {
                "student_state": "question",
                "student_reached_answer": False,
                "confidence_score": 0.0,
                "hint_level": int(state.get("hint_level", 0)),
                "search_needed": False,
                "critique": "topic_selected_start_tutoring",
            }
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.topic_lock_guard",
                "result": "bypassed_setup_classification_on_topic_selection",
            })
        else:
            from conversation.teacher import fire_activity as _fire_activity
            _fire_activity("Reading your message")
            eval_result = self._setup_call(state)
        try:
            parsed_hint_level = int(eval_result.get("hint_level", state.get("hint_level", 0)))
        except (TypeError, ValueError):
            parsed_hint_level = int(state.get("hint_level", 0))
        sanitized_locked, sanitize_action = _sanitize_locked_answer(
            str(state.get("locked_answer", "")),
            state.get("retrieved_chunks", []),
            str(state.get("locked_answer", "")),
        )
        state["debug"]["turn_trace"].append({
            "wrapper": "dean.sanitize_locked_answer",
            "candidate": str(state.get("locked_answer", ""))[:100],
            "action": sanitize_action,
            "final": sanitized_locked,
        })
        eval_result = {
            "student_state": eval_result.get("student_state", "irrelevant"),
            "student_reached_answer": bool(eval_result.get("student_reached_answer", False)),
            "confidence_score": eval_result.get("confidence_score", 0.0),
            "hint_level": parsed_hint_level,
            "locked_answer": sanitized_locked,
            "search_needed": bool(eval_result.get("search_needed", False)),
            "critique": eval_result.get("critique", ""),
        }

        confidence_score = self._compute_student_confidence(state, eval_result)
        eval_result["confidence_score"] = confidence_score
        # NOTE (2026-04-29 reached-gate refactor): the previous design
        # gated student_reached_answer on (LLM-self-rated answer_confidence
        # >= 0.72). That conflated step-correctness ("on the right track")
        # with answer-reached ("stated the locked answer"), producing
        # false positives — e.g. student says "gravity" while locked
        # answer is "skeletal muscle pump", LLM returns 0.95, threshold
        # flips reached=True, tutor fabricates "you've correctly identified
        # the skeletal muscle pump."
        #
        # New gate: deterministic token-overlap (against locked_answer +
        # aliases) with hedge-detection short-circuit, plus an LLM
        # paraphrase fallback that must quote the student verbatim. The
        # confidence_score number is kept on state for telemetry/mastery
        # rolling-mean but no longer drives reached.
        latest_student_msg = ""
        for _msg in reversed(state.get("messages", []) or []):
            if (_msg or {}).get("role") == "student":
                latest_student_msg = str(_msg.get("content", "") or "")
                break
        # On the topic-just-locked turn, the latest student message is
        # the topic selection (a card pick or "Let's begin"), not an
        # answer attempt. Don't run the gate against it — the alias list
        # might accidentally token-overlap with a card label or filler
        # phrase and falsely flip reached. The ack-emit branch below
        # will produce the question; the student's NEXT message is the
        # first real attempt and the gate runs as normal then.
        skip_gate_for_ack = bool(state.get("topic_just_locked", False))
        # Phase 1 (2026-04-30): activity log surfacing for the reached-gate
        # decision. Mirrors Claude's "tool call X, Y, Z" pattern — when
        # the gate fires, show the student which step matched.
        from conversation.teacher import fire_activity as _fa_gate
        if latest_student_msg and state.get("locked_answer") and not skip_gate_for_ack:
            _fa_gate("Checking if your message reached the answer")
            gate_result = self.reached_answer_gate(state, latest_student_msg)
            gp = gate_result.get("path", "unknown")
            if gate_result.get("reached"):
                if gp == "overlap":
                    _fa_gate("Answer recognized (matched your wording)")
                elif gp == "paraphrase":
                    _fa_gate("Answer recognized (matched your paraphrase)")
                else:
                    _fa_gate("Answer recognized")
            else:
                # Don't surface "not reached" on every turn — too noisy.
                # Only show it when the LLM explicitly judged + rejected
                # (i.e. student said something that LOOKED like an answer).
                if gp in {"llm_no_quote", "no_overlap_no_paraphrase"}:
                    _fa_gate("Answer not yet reached, continuing")
        elif skip_gate_for_ack:
            gate_result = {
                "reached": False,
                "evidence": "",
                "path": "skipped_topic_just_locked",
            }
        else:
            # No student message yet (rapport phase, etc.) or no locked
            # answer — gate cannot fire. Treat as not reached.
            gate_result = {
                "reached": False,
                "evidence": "",
                "path": "skipped_no_msg_or_lock",
            }
        eval_result["student_reached_answer"] = bool(gate_result.get("reached", False))
        # K-of-N partial reach support: capture coverage so the mastery
        # scorer can apply partial credit. Defaults to 1.0 for full reach
        # via legacy paths, 0.0 when not reached.
        gate_coverage = float(gate_result.get("coverage", 1.0 if eval_result["student_reached_answer"] else 0.0))
        gate_path_value = str(gate_result.get("path", "unknown"))

        # Apply eval results to state
        state["student_state"] = eval_result["student_state"]
        state["student_reached_answer"] = eval_result["student_reached_answer"]
        state["student_reach_coverage"] = round(_clamp01(gate_coverage), 3)
        state["student_reach_path"] = gate_path_value
        state["locked_answer"] = eval_result["locked_answer"] or state["locked_answer"]
        state["student_answer_confidence"] = confidence_score

        samples = int(state.get("confidence_samples", 0))
        prev_mean = float(state.get("student_mastery_confidence", 0.0))
        new_mean = confidence_score if samples <= 0 else ((prev_mean * samples) + confidence_score) / (samples + 1)
        state["confidence_samples"] = samples + 1
        state["student_mastery_confidence"] = round(_clamp01(new_mean), 3)
        state["debug"]["turn_trace"].append({
            "wrapper": "dean.confidence_score",
            "result": (
                f"answer_conf={confidence_score:.3f}, "
                f"mastery_conf={state['student_mastery_confidence']:.3f}, "
                f"reached={state['student_reached_answer']} "
                f"coverage={state['student_reach_coverage']:.2f} "
                f"(gate_path={gate_path_value})"
            ),
            "locked_answer": state.get("locked_answer", ""),
            "gate_path": gate_path_value,
            "gate_evidence": str(gate_result.get("evidence", ""))[:160],
            "gate_coverage": state["student_reach_coverage"],
            "gate_n_matched": gate_result.get("n_matched"),
            "gate_n_total": gate_result.get("n_total"),
        })

        hint_before = int(state.get("hint_level", 0))
        hint_reason = "unchanged"
        # Hint progression:
        # - increment only for "incorrect" once tutoring is unlocked (hint>=1)
        # - allow max_hints + 1 to signal "hints exhausted" routing in after_dean
        if eval_result["student_state"] == "incorrect":
            current_hint = int(state.get("hint_level", 0))
            if current_hint < 1:
                # First incorrect after the topic-lock acknowledgement
                # (Change 2): jump 0 → 1 so plan[0] fires as the first
                # scaffold. Old code clamped to 1 then incremented to 2,
                # which silently skipped plan[0] entirely.
                next_hint = 1
                hint_reason = "incorrect_first_post_ack"
            elif current_hint >= state["max_hints"]:
                next_hint = state["max_hints"] + 1
                hint_reason = "incorrect_max_hints_exceeded"
            else:
                next_hint = current_hint + 1
                hint_reason = "incorrect_increment"
            state["hint_level"] = min(next_hint, state["max_hints"] + 1)
        hint_after = int(state.get("hint_level", 0))
        active_hint = ""
        hint_plan = state["debug"].get("hint_plan", []) if isinstance(state.get("debug"), dict) else []
        if isinstance(hint_plan, list) and hint_after >= 1:
            idx = min(max(hint_after - 1, 0), len(hint_plan) - 1) if hint_plan else -1
            if idx >= 0:
                active_hint = str(hint_plan[idx])
        state["debug"]["turn_trace"].append({
            "wrapper": "dean.hint_progress",
            "hint_before": hint_before,
            "hint_after": hint_after,
            "hint_reason": hint_reason,
            "active_hint": active_hint,
        })
        state["debug"].setdefault("hint_progress", []).append({
            "turn": int(state.get("turn_count", 0)),
            "student_state": state.get("student_state"),
            "hint_before": hint_before,
            "hint_after": hint_after,
            "reason": hint_reason,
            "active_hint": active_hint,
        })

        if state["student_reached_answer"]:
            return {
                "student_state": state["student_state"],
                "student_reached_answer": True,
                "student_answer_confidence": state["student_answer_confidence"],
                "student_mastery_confidence": state["student_mastery_confidence"],
                "confidence_samples": state["confidence_samples"],
                "locked_question": state.get("locked_question", ""),
                "locked_answer": state["locked_answer"],
                "locked_answer_aliases": state.get("locked_answer_aliases", []),
            "full_answer": state.get("full_answer", "") or state.get("locked_answer", ""),
                "retrieved_chunks": state["retrieved_chunks"],
                "topic_confirmed": state.get("topic_confirmed", False),
                "topic_just_locked": bool(state.get("topic_just_locked", False)),
                "topic_options": state.get("topic_options", []),
                "topic_question": state.get("topic_question", ""),
                "topic_selection": state.get("topic_selection", ""),
                "pending_user_choice": state.get("pending_user_choice", {}),
                "debug": state["debug"],
            }

        # Early exit if hints exhausted — skip Teacher, route directly to assessment
        if state.get("hint_level", 0) > state.get("max_hints", 3):
            return {
                "messages": state["messages"],
                "hint_level": state["hint_level"],
                "student_state": state["student_state"],
                "student_reached_answer": state["student_reached_answer"],
                "student_answer_confidence": state.get("student_answer_confidence", 0.0),
                "student_mastery_confidence": state.get("student_mastery_confidence", 0.0),
                "confidence_samples": state.get("confidence_samples", 0),
                "locked_question": state.get("locked_question", ""),
                "locked_answer": state["locked_answer"],
                "locked_answer_aliases": state.get("locked_answer_aliases", []),
            "full_answer": state.get("full_answer", "") or state.get("locked_answer", ""),
                "retrieved_chunks": state["retrieved_chunks"],
                "topic_confirmed": state.get("topic_confirmed", False),
                "topic_just_locked": bool(state.get("topic_just_locked", False)),
                "topic_options": state.get("topic_options", []),
                "topic_question": state.get("topic_question", ""),
                "topic_selection": state.get("topic_selection", ""),
                "pending_user_choice": state.get("pending_user_choice", {}),
                "help_abuse_count": state.get("help_abuse_count", 0),
                "off_topic_count": state.get("off_topic_count", 0),
                "total_low_effort_turns": state.get("total_low_effort_turns", 0),
                "total_off_topic_turns": state.get("total_off_topic_turns", 0),
                "clinical_low_effort_count": state.get("clinical_low_effort_count", 0),
                "clinical_off_topic_count": state.get("clinical_off_topic_count", 0),
                "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
                "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
                "mastery_tier": state.get("mastery_tier", "not_assessed"),
                "dean_retry_count": 0,
                "dean_critique": "",
                "debug": state["debug"],
            }

        # ============================================================
        # Change 4 (2026-04-30): unified counter system (deterministic
        # state-machine counters, not semantic judgments).
        #
        # Two counters track conversation health:
        #   - help_abuse_count: consecutive low_effort turns
        #   - off_topic_count:  consecutive off-DOMAIN turns (category C)
        #     — domain-tangential questions handled by exploration_judge
        #       (category B) do NOT increment this counter
        #
        # Plus two non-resetting telemetry counters that the mastery
        # scorer reads to assess session-wide patterns:
        #   - total_low_effort_turns
        #   - total_off_topic_turns
        # ============================================================

        # ----- help_abuse_count (low_effort) -----
        if state["student_state"] == "low_effort":
            state["help_abuse_count"] = state.get("help_abuse_count", 0) + 1
            state["total_low_effort_turns"] = state.get("total_low_effort_turns", 0) + 1
        else:
            # Reset on ANY engaged turn (correct/partial/incorrect/question/irrelevant).
            # Note: irrelevant resets help_abuse but increments off_topic.
            state["help_abuse_count"] = 0

        # ----- off_topic_count (irrelevant AND not domain-tangential) -----
        # We need to know whether this turn's "irrelevant" is category B
        # (domain-tangential — exploration_judge.needed=True) or category
        # C (off-domain — no exploration). The exploration judge runs
        # later in this method (line ~1620 area), but for the counter
        # we can use a simpler heuristic up front: if the message
        # contains overt off-domain markers (we'll defer to the LLM
        # judge and run it now if state is irrelevant).
        is_off_domain = False
        if state["student_state"] == "irrelevant":
            is_off_domain = self._is_off_domain_judgment(state)
        if is_off_domain:
            state["off_topic_count"] = state.get("off_topic_count", 0) + 1
            state["total_off_topic_turns"] = state.get("total_off_topic_turns", 0) + 1
        else:
            state["off_topic_count"] = 0

        # ----- Activity-log surfacing for strikes (UI debugging) -----
        from conversation.teacher import fire_activity as _fa
        if state["help_abuse_count"] > 0 and state["help_abuse_count"] < cfg.dean.help_abuse_threshold:
            _fa(f"Help-abuse strike {state['help_abuse_count']}/{cfg.dean.help_abuse_threshold}")
        if state["off_topic_count"] > 0 and state["off_topic_count"] < cfg.dean.off_topic_threshold:
            _fa(f"Off-topic strike {state['off_topic_count']}/{cfg.dean.off_topic_threshold}")

        # ----- Threshold actions -----
        # help_abuse strike 4 → advance hint with LLM-narrated transition
        if state["help_abuse_count"] >= cfg.dean.help_abuse_threshold:
            prev_hint = int(state.get("hint_level", 0))
            new_hint = min(prev_hint + 1, state["max_hints"] + 1)
            state["hint_level"] = new_hint
            state["help_abuse_count"] = 0
            _fa(f"Help-abuse threshold reached: advancing hint {prev_hint} → {new_hint}")
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.help_abuse_advance_hint",
                "result": (
                    f"help_abuse_threshold ({cfg.dean.help_abuse_threshold}) reached; "
                    f"advancing hint_level {prev_hint} → {new_hint} with narration brief"
                ),
                "hint_before": prev_hint,
                "hint_after": new_hint,
                "hint_reason": "help_abuse_threshold_advance",
            })
            # Narration brief: the dean's pre-flight critique (fed to
            # teacher.draft_socratic) tells the LLM to PHRASE the
            # transition. Not hardcoded — the LLM picks the wording.
            # IMPORTANT: re-assert the no-leak rule explicitly here. The
            # earlier version of this brief said "deliver the new hint"
            # without that guard, and the LLM caved under stonewalling
            # pressure — observed verbatim leak "The structure combines
            # 'coronary' + 'sinus'" at the cap turn (session 2026-04-30).
            # The brief now forbids naming any anchor or alias term.
            state["_dean_warning_brief_pending"] = (
                f"The student has been unable to engage with hint level {prev_hint} "
                f"(four consecutive low-effort responses). On this turn, naturally "
                f"acknowledge the lack of progress and announce that you're moving "
                f"to a more direct hint (level {new_hint}). Keep it brief, supportive, "
                f"non-shaming. Then deliver the new hint as a non-revealing direct "
                f"probe — do NOT name any component of the locked answer, any of its "
                f"aliases, or any distinctive noun from the textbook full_answer. "
                f"Asking the question more pointedly is allowed; revealing answer "
                f"terms (or spelling them as 'X + Y, put together') is not."
            )

        # off_topic strike 4 → terminate WHOLE session
        if state["off_topic_count"] >= cfg.dean.off_topic_threshold:
            _fa("Off-topic threshold reached: ending session")
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.off_topic_terminate",
                "result": (
                    f"off_topic_threshold ({cfg.dean.off_topic_threshold}) reached; "
                    f"terminating session (clinical_mastery_tier=not_assessed, "
                    f"core_mastery_tier=not_assessed)"
                ),
                "off_topic_count_at_terminate": state["off_topic_count"],
            })
            # Mark mastery tiers as not_assessed BEFORE routing to
            # memory_update so the mastery scorer sees the right state.
            state["core_mastery_tier"] = "not_assessed"
            state["clinical_mastery_tier"] = "not_assessed"
            state["mastery_tier"] = "not_assessed"
            # Force route to memory_update by exhausting hints AND
            # signaling that clinical should be skipped.
            state["hint_level"] = state["max_hints"] + 1
            state["assessment_turn"] = 3  # signals "done" so after_assessment skips clinical
            state["off_topic_count"] = 0
            state["_off_topic_terminated"] = True
            # Narration brief: dean tells teacher to produce the farewell.
            state["_dean_warning_brief_pending"] = (
                "The student has gone off-domain four times in a row "
                "(asking about subjects outside this textbook). On this "
                "turn, produce a brief, polite farewell: acknowledge the "
                "drift, suggest they come back when they want to focus on "
                f"{getattr(getattr(cfg, 'domain', object()), 'short', 'the subject')}, "
                "and end without asking another question. Do NOT teach further."
            )

        from conversation.teacher import fire_activity

        # D.2 — Adaptive-RAG (Jeong 2024) complexity tier classification.
        # Logged only today; doesn't gate behavior. Provides citable
        # architectural component for the thesis lit-review section and
        # produces telemetry (per-turn tier distribution) for evaluation.
        # See _classify_complexity docstring for the full rationale on
        # why we don't yet replace the existing exploration judge.
        if bool(getattr(cfg.dean, "adaptive_rag_enabled", True)):
            fire_activity("Classifying your question")
            classification = self._classify_complexity(state)
            tier = classification.get("tier", "simple")
            fire_activity(f"Question tier: {tier}")

        # Exploration retrieval: LLM judges whether the student's turn has
        # tangential curiosity that warrants a one-shot un-section-filtered
        # retrieval. Budget-capped per session (cfg.session.exploration_max).
        fire_activity("Considering related topics")
        self._exploration_retrieval_maybe(state)

        # Change 2 (2026-04-29): topic-acknowledgement turn.
        # When the topic just locked (this turn for free-text path, or via
        # _apply_prelock for revisit path), emit a deterministic message
        # that announces the topic location AND states the locked_question
        # verbatim — instead of jumping straight into a paraphrased hint.
        # No LLM, no QC loop, no hint-plan consumption. The student gets
        # one full turn to attempt the actual question before any hints fire.
        if state.get("topic_just_locked", False):
            fire_activity("Showing the question")
            approved_response = self._build_topic_ack_message(state)
            state["topic_just_locked"] = False
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.topic_ack_emitted",
                "result": "ack_message_emitted_skipping_teacher_draft",
                "locked_question": state.get("locked_question", ""),
            })
        else:
            # Teacher drafts one response.
            # Provide Dean QC guidance preflight on first attempt so Teacher is
            # aligned before generation, not only after a rejection.
            state["dean_critique"] = self._teacher_preflight_brief(state)
            fire_activity("Drafting response")
            draft = teacher.draft_socratic(state)
            fire_activity("Reviewing draft for accuracy")
            quality = self._evaluate_tutoring_draft(state, draft)

            if quality["pass"]:
                approved_response = draft
            else:
                revised = (quality.get("revised_teacher_draft") or "").strip()
                revised_ok = False
                if revised:
                    revised_det = self._deterministic_tutoring_check(state, revised)
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean._deterministic_quality_check_revised",
                        "result": "PASS" if revised_det["pass"] else f"FAIL: {revised_det['critique']}",
                        "reason_codes": revised_det.get("reason_codes", []),
                    })
                    revised_ok = bool(revised_det["pass"])

                if revised_ok:
                    approved_response = revised
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.revised_teacher_draft_applied",
                        "result": "Applied Dean revised_teacher_draft (single-pass repair)",
                    })
                    # The streamed first draft is now stale — its content
                    # WILL differ from `approved_response`. Tell the WS
                    # handler to clear the frontend's streaming buffer so
                    # the user doesn't see content X stream in then get
                    # abruptly replaced by Y. (Best-effort; no-op when
                    # no callback is installed, e.g. eval harness.)
                    from conversation.teacher import fire_stream_invalidate
                    fire_stream_invalidate()
                    fire_activity("Refining response")
                else:
                    state["dean_critique"] = self._format_dean_critique(quality)
                    state["dean_retry_count"] = 1
                    self._log_intervention(
                        state["student_id"], state["turn_count"], state["dean_critique"], draft
                    )
                    approved_response = self._dean_fallback(state)
                    state["debug"]["interventions"] += 1
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.fallback",
                        "tool_called": None,
                        "result": "Dean fallback used (no valid revised_teacher_draft)",
                    })
                    # Same reason as above — the streamed draft is being
                    # discarded in favor of the dean's fallback message.
                    from conversation.teacher import fire_stream_invalidate
                    fire_stream_invalidate()
                    fire_activity("Falling back to safe response")

        # Change 6 (2026-04-30): hint indicator caption.
        # Append "— Hint X of Y —" to the tutor message when we're in a
        # tutoring turn that's actively rendering a hint (hint_level >= 1).
        # Suppressed for the topic-ack turn (no hint consumed yet) and for
        # the help-abuse cap turn (where the LLM narrates the transition
        # itself; double-tagging would be redundant). Frontend can detect
        # the marker and style as a small amber pill if desired.
        try:
            cur_hint = int(state.get("hint_level", 0))
            max_hint = int(state.get("max_hints", 3) or 3)
            # Skip the indicator on the topic-ack turn (handled separately
            # via the topic_just_locked branch which doesn't run this code
            # path) and when the cap-narration brief was just emitted (the
            # narration already explains the transition).
            cap_just_fired = bool(state.get("_dean_warning_brief_pending"))
            should_tag = (
                cur_hint >= 1
                and cur_hint <= max_hint
                and not cap_just_fired
                and not state.get("topic_just_locked", False)
            )
            if should_tag:
                if cur_hint == max_hint:
                    suffix = f"\n\n_— Last hint ({cur_hint} of {max_hint}) — give it your best try. —_"
                else:
                    suffix = f"\n\n_— Hint {cur_hint} of {max_hint} —_"
                approved_response = f"{approved_response}{suffix}"
        except (TypeError, ValueError):
            pass

        state["messages"].append({"role": "tutor", "content": approved_response, "phase": "tutoring"})

        # Anti-loop guard: force assessment if interventions keep accumulating.
        max_interventions = int(getattr(cfg.dean, "max_interventions_before_force_assessment", 4))
        if state["debug"].get("interventions", 0) >= max_interventions:
            state["hint_level"] = state["max_hints"] + 1
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.force_assessment_loop_guard",
                "result": f"forced_assessment_after_{state['debug'].get('interventions', 0)}_interventions",
            })

        return {
            "messages": state["messages"],
            "hint_level": state["hint_level"],
            "student_state": state["student_state"],
            "student_reached_answer": state["student_reached_answer"],
            "student_answer_confidence": state["student_answer_confidence"],
            "student_mastery_confidence": state["student_mastery_confidence"],
            "confidence_samples": state["confidence_samples"],
            "locked_question": state.get("locked_question", ""),
            "locked_answer": state["locked_answer"],
            "locked_answer_aliases": state.get("locked_answer_aliases", []),
            "full_answer": state.get("full_answer", "") or state.get("locked_answer", ""),
            "retrieved_chunks": state["retrieved_chunks"],
            "topic_confirmed": state.get("topic_confirmed", False),
            "topic_just_locked": bool(state.get("topic_just_locked", False)),
            "topic_options": state.get("topic_options", []),
            "topic_question": state.get("topic_question", ""),
            "topic_selection": state.get("topic_selection", ""),
            "pending_user_choice": state.get("pending_user_choice", {}),
            "help_abuse_count": state["help_abuse_count"],
            # Change 4: persist new counters
            "off_topic_count": state.get("off_topic_count", 0),
            "total_low_effort_turns": state.get("total_low_effort_turns", 0),
            "total_off_topic_turns": state.get("total_off_topic_turns", 0),
            # Change 5.1: persist clinical counters too
            "clinical_low_effort_count": state.get("clinical_low_effort_count", 0),
            "clinical_off_topic_count": state.get("clinical_off_topic_count", 0),
            # Change 4.5: mastery tiers may have been set to not_assessed
            # by off-topic terminate; persist those.
            "core_mastery_tier": state.get("core_mastery_tier", "not_assessed"),
            "clinical_mastery_tier": state.get("clinical_mastery_tier", "not_assessed"),
            "mastery_tier": state.get("mastery_tier", "not_assessed"),
            "dean_retry_count": 0,
            "dean_critique": "",
            "debug": state["debug"],
        }

    def _compute_student_confidence(self, state: TutorState, eval_result: dict) -> float:
        """
        Compute per-turn answer confidence without changing categorical labels.
        Uses model score directly; falls back to state-based default when missing.
        """
        student_state = str(eval_result.get("student_state", "")).strip().lower()
        raw = eval_result.get("confidence_score", None)
        try:
            model_score = None if raw is None else _clamp01(float(raw))
        except (TypeError, ValueError):
            model_score = None

        if model_score is not None:
            return round(_clamp01(model_score), 3)

        fallback_by_state = {
            "correct": 0.74,
            "partial_correct": 0.56,
            "question": 0.42,
            "incorrect": 0.24,
            "irrelevant": 0.10,
            "low_effort": 0.05,
        }
        return round(float(fallback_by_state.get(student_state, 0.35)), 3)

    def _retrieve_on_topic_lock(self, state: TutorState) -> None:
        """
        Retrieval for topic scoping/locking.
        Product invariant for this milestone: retrieval fires at most once per session.
        """
        retrieval_calls = int(state.get("debug", {}).get("retrieval_calls", 0))
        if retrieval_calls >= 1:
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.retrieval_guard",
                "result": "skipped_retrieval_already_fired_once",
                "retrieval_calls": retrieval_calls,
            })
            return

        query = _build_retrieval_query(state)
        if not query:
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.retrieval_query_guard",
                "result": "skipped_retrieval_due_to_ambiguous_query",
            })
            return

        # Anchor lock + hint plan benefit from wider recall — ask for more
        # candidate chunks here than a typical per-turn Teacher draft uses.
        locked = state.get("locked_topic") or {}
        chunks = search_textbook(
            query,
            self.retriever,
            top_k=12,
            locked_section=locked.get("section") or None,
            locked_subsection=locked.get("subsection") or None,
        )
        state["retrieved_chunks"] = chunks
        state["debug"]["retrieval_calls"] = int(state["debug"].get("retrieval_calls", 0)) + 1
        retrieval_schema_keys = sorted(list(chunks[0].keys())) if chunks else []
        state["debug"]["turn_trace"].append({
            "wrapper": "dean.python_retrieval",
            "result": f"{len(chunks)} chunks returned | query={query}",
            "retrieval": _retrieval_trace_payload(query, chunks),
            "retrieval_calls": state["debug"]["retrieval_calls"],
            "retrieval_schema_keys": retrieval_schema_keys,
        })

    def _classify_complexity(self, state: TutorState) -> dict:
        """D.2 — Adaptive-RAG (Jeong 2024) query complexity classifier.

        Classifies the student's most recent message into one of three
        tiers driving downstream retrieval strategy:

          simple      — direct factual / definitional, single-hop
          tangential  — curiosity drift, warrants exploration retrieval
          complex     — multi-step / cross-topic synthesis, multi-hop
                        candidate (D.4 stub — currently treated as simple)

        Returns:
            {"tier": "simple"|"tangential"|"complex", "rationale": str}
            or {"tier": "simple", "rationale": "<error>"} on any failure
            so callers can rely on the dict shape.

        Cost: one Haiku-tier LLM call per turn (~$0.001). Result is also
        logged to turn_trace under wrapper="dean.complexity_classifier"
        for thesis-evaluable telemetry — frequency distribution across
        tiers can be reported in the ablation table.

        Behavior coupling: today the tier is logged only — runtime
        behavior is unchanged (the existing _exploration_retrieval_maybe
        still uses its internal judge). A future commit can replace
        that judge with the tier signal once we have data on how often
        the two agree.
        """
        if not state.get("topic_confirmed"):
            # Pre-lock messages aren't tutoring queries; classifier is
            # only meaningful after a topic is locked.
            return {"tier": "simple", "rationale": "pre_lock_no_classification"}

        last_student_msg = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "student":
                last_student_msg = str(msg.get("content", ""))
                break
        if not last_student_msg.strip():
            return {"tier": "simple", "rationale": "empty_student_message"}

        static_prompt = getattr(cfg.prompts, "dean_complexity_classifier_static", "")
        dynamic_template = getattr(cfg.prompts, "dean_complexity_classifier_dynamic", "")
        if not static_prompt or not dynamic_template:
            return {"tier": "simple", "rationale": "classifier_prompts_missing"}

        conversation_history = render_history(state.get("messages", []))
        try:
            user_prompt = dynamic_template.format(
                locked_subsection=str(
                    (state.get("locked_topic") or {}).get("subsection", "") or "(none)"
                ),
                locked_question=state.get("locked_question", "") or "(none)",
                conversation_history=conversation_history,
                latest_student_message=last_student_msg,
                **_domain_prompt_vars(),
            )
        except Exception:
            return {"tier": "simple", "rationale": "prompt_format_error"}

        # Re-use _timed_create + _cached_system from this module so
        # cache + telemetry behave identically to the other Dean calls.
        try:
            resp = _timed_create(
                self.client,
                state,
                "dean.complexity_classifier",
                model=self.model,
                temperature=0,
                max_tokens=160,
                system=_cached_system(
                    getattr(cfg.prompts, "dean_base", ""),
                    static_prompt,
                    "",
                    "",
                    "",
                ),
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = (resp.content[0].text or "").strip()
        except Exception as e:
            return {"tier": "simple", "rationale": f"call_error:{type(e).__name__}"}

        parsed = _extract_json_object(text) or {}
        tier = str(parsed.get("tier", "") or "").strip().lower()
        rationale = str(parsed.get("rationale", "") or "").strip()
        if tier not in ("simple", "tangential", "complex"):
            tier = "simple"
            if not rationale:
                rationale = "fallback_invalid_tier"

        state["debug"]["turn_trace"].append({
            "wrapper": "dean.complexity_classifier",
            "tier": tier,
            "rationale": rationale[:200],
        })
        # Persist on debug for the dashboard / export. NOT used for
        # routing yet — see method docstring.
        state["debug"]["complexity_tier"] = tier
        return {"tier": tier, "rationale": rationale}

    def _exploration_retrieval_maybe(self, state: TutorState) -> None:
        """
        Budget-capped exploration retrieval. When the student asks something
        tangential to the locked topic (a connected concept, a prerequisite,
        a cross-system link), this fetches un-section-filtered chunks and
        merges them into `retrieved_chunks` so Teacher can address the
        tangent without drifting off the locked question.

        Skipped if:
          - no topic locked yet,
          - no session budget remaining,
          - LLM judge says the latest message is on-topic / venting / OOD.

        Cost: one LLM classification per tutoring turn (small, Haiku-grade).
        When the judge fires exploration, one extra retrieval call is made.
        """
        if not state.get("topic_confirmed") or not state.get("locked_question"):
            return
        budget = int(state.get("exploration_max", 0) or 0)
        used = int(state.get("exploration_used", 0) or 0)
        if budget <= 0 or used >= budget:
            return

        conversation_history = render_history(state.get("messages", []))
        wrapper_delta = (
            getattr(cfg.prompts, "dean_exploration_judge_delta", "")
            or getattr(cfg.prompts, "dean_exploration_judge_static", "")
        )
        dynamic_prompt = getattr(cfg.prompts, "dean_exploration_judge_dynamic", "").format(
            topic_selection=state.get("topic_selection", "") or "",
            locked_question=state.get("locked_question", "") or "",
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )
        resp = _timed_create(
            self.client,
            state,
            "dean._exploration_judge",
            model=self.model,
            temperature=0,
            max_tokens=200,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                "",
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Judge and return strict JSON only."}],
        )
        parsed = _extract_json_object((resp.content[0].text or "").strip()) or {}
        needed = bool(parsed.get("exploration_needed", False))
        query = str(parsed.get("exploration_query", "") or "").strip()
        if not needed or not query:
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.exploration_judge",
                "result": "skip",
                "needed": needed,
                "query": query,
                "rationale": str(parsed.get("rationale", "") or ""),
            })
            return

        # Exploration retrieval deliberately runs with NO section filter so it
        # can surface related content from any chapter.
        extra = search_textbook(query, self.retriever, top_k=3)
        if not extra:
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.exploration_retrieval",
                "result": "empty",
                "query": query,
            })
            return

        tagged = []
        for c in extra:
            row = dict(c)
            row["exploration"] = True
            tagged.append(row)

        # Append to locked-section chunks so Teacher sees both contexts; keep
        # locked-section chunks first so Socratic questioning stays grounded.
        state["retrieved_chunks"] = list(state.get("retrieved_chunks", []) or []) + tagged
        state["exploration_used"] = used + 1
        state["debug"]["turn_trace"].append({
            "wrapper": "dean.exploration_retrieval",
            "result": f"fired ({len(tagged)} chunks)",
            "query": query,
            "budget_remaining": budget - (used + 1),
        })

    def _prelock_intent_call(self, state: TutorState) -> dict:
        """
        LLM-driven intent classifier for the pre-lock (topic-selection) phase.
        Replaces the rule-based low-effort / non-topic / ambiguity detectors.

        Returns a dict: {intent, normalized_topic, tutor_reply, rationale}.
          intent ∈ {topic_query, card_pick, greeting, distress, off_topic, ambiguous}
          normalized_topic — cleaned topic string for topic_query/card_pick, else ""
          tutor_reply      — LLM-written reply for non-topic intents, else ""
        """
        messages = state.get("messages", [])
        latest = _latest_student_message(messages) or ""
        topic_options = list(state.get("topic_options", []))
        conversation_history = render_history(messages)

        wrapper_delta = (
            getattr(cfg.prompts, "dean_prelock_intent_delta", "")
            or getattr(cfg.prompts, "dean_prelock_intent_static", "")
        )
        dynamic_prompt = getattr(cfg.prompts, "dean_prelock_intent_dynamic", "").format(
            topic_options=json.dumps(topic_options, ensure_ascii=False),
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )
        resp = _timed_create(
            self.client,
            state,
            "dean._prelock_intent_call",
            model=self.model,
            temperature=0,
            max_tokens=320,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                "",
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{
                "role": "user",
                "content": f"Student's latest message: {latest!r}\nReturn strict JSON only.",
            }],
        )
        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text) or {}

        valid_intents = {
            "topic_query", "card_pick", "greeting",
            "distress", "off_topic", "ambiguous",
        }
        intent = str(parsed.get("intent", "") or "").strip().lower()
        if intent not in valid_intents:
            # Parse fallback: treat substantive multi-word text as a topic query,
            # everything else as ambiguous (handled by LLM reply downstream).
            tokens = [t for t in _normalize_text(latest).split() if t]
            intent = "topic_query" if len(tokens) >= 2 else "ambiguous"

        return {
            "intent": intent,
            "normalized_topic": str(parsed.get("normalized_topic", "") or "").strip(),
            "tutor_reply": str(parsed.get("tutor_reply", "") or "").strip(),
            "rationale": str(parsed.get("rationale", "") or "").strip(),
        }

    def _prelock_refuse_call(
        self,
        state: TutorState,
        rejected_topic: str,
        failure_count: int,
        refuse_reason: str,
    ) -> dict:
        """
        LLM-authored intro text for a refuse-with-cards turn. Used when a TOC
        match fails or the coverage gate rejects a locked topic. The caller is
        responsible for rendering the card list after `tutor_reply`.
        """
        conversation_history = render_history(state.get("messages", []))
        wrapper_delta = (
            getattr(cfg.prompts, "dean_prelock_refuse_delta", "")
            or getattr(cfg.prompts, "dean_prelock_refuse_static", "")
        )
        dynamic_prompt = getattr(cfg.prompts, "dean_prelock_refuse_dynamic", "").format(
            rejected_topic=rejected_topic or "",
            failure_count=int(failure_count),
            refuse_reason=refuse_reason or "",
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )
        resp = _timed_create(
            self.client,
            state,
            "dean._prelock_refuse_call",
            model=self.model,
            temperature=0.2,
            max_tokens=220,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                "",
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Write the refuse intro. Return strict JSON only."}],
        )
        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text) or {}
        return {
            "tutor_reply": str(parsed.get("tutor_reply", "") or "").strip(),
            "rationale": str(parsed.get("rationale", "") or "").strip(),
        }

    def _prelock_anchor_fail_call(
        self,
        state: TutorState,
        topic_selection: str,
    ) -> dict:
        """
        LLM-authored intro text for the anchor-extraction-failed path. Used when
        retrieval succeeded but no clean pedagogical anchor could be locked.
        """
        conversation_history = render_history(state.get("messages", []))
        wrapper_delta = (
            getattr(cfg.prompts, "dean_prelock_anchor_fail_delta", "")
            or getattr(cfg.prompts, "dean_prelock_anchor_fail_static", "")
        )
        dynamic_prompt = getattr(cfg.prompts, "dean_prelock_anchor_fail_dynamic", "").format(
            topic_selection=topic_selection or "",
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )
        resp = _timed_create(
            self.client,
            state,
            "dean._prelock_anchor_fail_call",
            model=self.model,
            temperature=0.2,
            max_tokens=180,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                "",
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Write the anchor-fail intro. Return strict JSON only."}],
        )
        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text) or {}
        return {
            "tutor_reply": str(parsed.get("tutor_reply", "") or "").strip(),
            "rationale": str(parsed.get("rationale", "") or "").strip(),
        }

    def _lock_anchors_call(self, state: TutorState) -> dict:
        """
        Lock both question and answer anchors immediately after topic-lock retrieval.
        Returns a dict with: locked_question, locked_answer, rationale.

        Change (2026-04-30, post-18-convo eval review): added lock-time
        section filtering. The 18-convo run produced lock drift (e.g.
        student asked about long bone structure, dean locked on "skeletal
        cardiac and smooth muscle" because the matcher's chunks included
        muscle attachment content). Filter retrieved chunks to ONLY those
        whose subsection_title matches state.locked_topic.subsection
        before passing to the lock LLM. This forces the lock to be
        grounded in the actually-locked subsection, not whatever
        adjacent content the chunker pulled in.
        """
        topic_selection = str(state.get("topic_selection", "") or "").strip()

        # Filter chunks to the locked subsection ONLY. If the filter
        # leaves us with fewer than `min_chunks` (default 2), fall back
        # to the unfiltered set so we don't fail to lock entirely.
        all_chunks = state.get("retrieved_chunks", []) or []
        locked_topic = state.get("locked_topic") or {}
        target_subsection = str(locked_topic.get("subsection", "") or "").strip()
        if target_subsection and all_chunks:
            in_section = [
                c for c in all_chunks
                if str((c or {}).get("subsection_title") or "").strip() == target_subsection
            ]
            min_chunks_for_lock = 2
            if len(in_section) >= min_chunks_for_lock:
                section_chunks = in_section
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean._lock_anchors_call.section_filter",
                    "result": (
                        f"filtered to {len(in_section)}/{len(all_chunks)} chunks "
                        f"in subsection {target_subsection!r}"
                    ),
                    "in_section_count": len(in_section),
                    "total_count": len(all_chunks),
                })
            else:
                section_chunks = all_chunks
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean._lock_anchors_call.section_filter",
                    "result": (
                        f"in-section count {len(in_section)} < min_chunks_for_lock "
                        f"({min_chunks_for_lock}); falling back to unfiltered "
                        f"{len(all_chunks)} chunks"
                    ),
                    "in_section_count": len(in_section),
                    "total_count": len(all_chunks),
                })
        else:
            section_chunks = all_chunks

        chunks_str = _format_chunks(section_chunks)
        conversation_history = render_history(state.get("messages", []))
        wrapper_delta = (
            getattr(cfg.prompts, "dean_lock_anchors_delta", "")
            or getattr(cfg.prompts, "dean_lock_anchors_static", "")
        )
        dynamic_prompt = getattr(cfg.prompts, "dean_lock_anchors_dynamic", "").format(
            topic_selection=topic_selection,
            retrieved_propositions=chunks_str,
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )

        resp = _timed_create(
            self.client, state, "dean._lock_anchors_call",
            model=self.model,
            temperature=0,
            max_tokens=360,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                chunks_str,
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Lock anchors and return strict JSON."}],
        )
        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text)
        if parsed is None:
            fallback_answer_raw = self._extract_answer_parametric(state)
            fallback_answer, fallback_action = _sanitize_locked_answer(
                fallback_answer_raw,
                state.get("retrieved_chunks", []),
                "",
            )
            state["debug"]["turn_trace"].append({
                "wrapper": "dean._lock_anchors_call_parse_fallback",
                "result": "parse_failed",
                "fallback_answer": fallback_answer,
                "fallback_action": fallback_action,
            })
            return {
                "locked_question": topic_selection if fallback_answer else "",
                "locked_answer": fallback_answer,
                "locked_answer_aliases": [],
                "full_answer": fallback_answer,  # fallback: same as locked_answer
                "rationale": "parse_failed",
            }

        locked_question = str(parsed.get("locked_question", "") or "").strip()
        locked_answer_raw = str(parsed.get("locked_answer", "") or "").strip()
        # Two-tier (Change 2026-04-30): full_answer is the complete textbook
        # answer (may be a list/sentence). Falls back to locked_answer if
        # the lock prompt didn't produce it (older runs / minor LLM omissions).
        full_answer_raw = str(parsed.get("full_answer", "") or "").strip()
        # Cap full_answer at 400 chars to prevent runaway. If empty,
        # fall back to locked_answer (preserves backward compat).
        full_answer = full_answer_raw[:400] if full_answer_raw else ""
        locked_answer, sanitize_action = _sanitize_locked_answer(
            locked_answer_raw,
            state.get("retrieved_chunks", []),
            "",
        )
        # Aliases: optional list of equivalent phrasings produced by the lock
        # prompt. Used by reached_answer_gate's Step A token-overlap check
        # so paraphrases get credit without needing the LLM fallback. We
        # sanitize permissively — empty/duplicate aliases are dropped, the
        # locked_answer itself is filtered out (it's checked separately),
        # and we cap at 5 to keep prompts and overlap-loops bounded.
        raw_aliases = parsed.get("locked_answer_aliases", []) or []
        if isinstance(raw_aliases, str):  # tolerate single-string output
            raw_aliases = [raw_aliases]
        seen_lower: set[str] = set()
        locked_answer_aliases: list[str] = []
        locked_lower = locked_answer.lower().strip() if locked_answer else ""

        def _push_alias(a_clean: str) -> bool:
            """Add a single alias if it passes filters. Returns True iff added.
            Hard cap on individual alias length: 50 chars (longer = LLM
            smuggled in a sentence, not a phrase)."""
            a_low = a_clean.lower()
            if not a_clean or a_low == locked_lower or a_low in seen_lower:
                return False
            if len(a_clean) > 50:
                return False
            seen_lower.add(a_low)
            locked_answer_aliases.append(a_clean)
            return True

        for a in raw_aliases:
            if not isinstance(a, str):
                continue
            a_clean = a.strip()
            # Defensive: the lock prompt forbids "and"-joined aliases
            # ("LCA and RCA" matches no real student utterance), but LLMs
            # occasionally violate it. Split such entries on ' and ' and admit
            # each piece as its own alias — that's what the prompt asked for.
            if " and " in a_clean.lower():
                pieces = [p.strip() for p in re.split(r"\s+and\s+", a_clean, flags=re.IGNORECASE) if p.strip()]
                for piece in pieces:
                    _push_alias(piece)
                    if len(locked_answer_aliases) >= 5:
                        break
            else:
                _push_alias(a_clean)
            if len(locked_answer_aliases) >= 5:
                break
        # Optional debug trace (toggleable via SOKRATIC_TOPIC_DEBUG env var).
        import os as _os_dbg
        if _os_dbg.environ.get("SOKRATIC_TOPIC_DEBUG"):
            print(f"  [lock-anchors] raw_answer={locked_answer_raw!r} "
                  f"action={sanitize_action!r} final={locked_answer!r}",
                  flush=True)
        state["debug"]["turn_trace"].append({
            "wrapper": "dean.sanitize_locked_answer",
            "candidate": locked_answer_raw[:100],
            "action": sanitize_action,
            "final": locked_answer,
        })
        # Repair once with a focused LLM pass if answer still failed sanitization.
        if not locked_answer:
            repair_resp = _timed_create(
                self.client,
                state,
                "dean._lock_anchors_repair_call",
                model=self.model,
                temperature=0,
                # Bumped from 140 to 240 (2026-04-29): the new schema with
                # locked_answer_aliases + rationale wouldn't fit in 140 tokens,
                # causing JSON to truncate mid-output. JSON parse then failed
                # silently and locked_answer stayed empty even when the LLM
                # had produced a perfectly-good answer term.
                max_tokens=240,
                system=_cached_system(
                    getattr(cfg.prompts, "dean_base", ""),
                    wrapper_delta,
                    chunks_str,
                    conversation_history,
                    dynamic_prompt,
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        "Return strict JSON only. locked_question must be specific. "
                        "locked_answer must be 1-5 words, a SINGLE noun phrase (like "
                        "'axillary nerve' or 'quadrangular space'). NO lists, NO verbs, "
                        "NO cord origins, NO supporting facts — only the target term. "
                        "**HARD RULE: never use 'and' to join two ideas.** If the "
                        "question is about TWO components (e.g. 'left and right coronary "
                        "arteries'), pick a single umbrella term ('coronary arteries') "
                        "for locked_answer and put each component as a SEPARATE alias "
                        "(e.g. aliases=['left coronary artery', 'right coronary artery', "
                        "'LCA', 'RCA']). The umbrella covers the gate; aliases match "
                        "what the student actually says. "
                        "Keep rationale to <=15 words and aliases to 4 short phrases so the "
                        "JSON fits well within max_tokens."
                    ),
                }],
            )
            repair_text = (repair_resp.content[0].text or "").strip()
            repair = _extract_json_object(repair_text)
            if repair is not None:
                repaired_question = str(repair.get("locked_question", "") or "").strip()
                repaired_raw = str(repair.get("locked_answer", "") or "").strip()
                repaired_answer, repaired_action = _sanitize_locked_answer(
                    repaired_raw,
                    state.get("retrieved_chunks", []),
                    "",
                )
                # Optional debug trace (toggleable via SOKRATIC_TOPIC_DEBUG env var).
                if _os_dbg.environ.get("SOKRATIC_TOPIC_DEBUG"):
                    print(f"  [lock-anchors-repair] raw={repaired_raw!r} "
                          f"action={repaired_action!r} final={repaired_answer!r}",
                          flush=True)
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.sanitize_locked_answer",
                    "candidate": repaired_raw[:100],
                    "action": repaired_action,
                    "final": repaired_answer,
                    "decision_effect": "anchor_repair_attempt",
                })
                if repaired_question:
                    locked_question = repaired_question
                if repaired_answer:
                    locked_answer = repaired_answer
                    parsed["rationale"] = (
                        str(parsed.get("rationale", "") or "").strip()
                        + " | repaired_once"
                    ).strip(" |")
                    # Repair often produces cleaner aliases (the original
                    # answer was wiped because it was a sentence; aliases
                    # generated alongside that sentence tend to also be
                    # paraphrases-of-a-sentence rather than term variants).
                    # Prefer the repair's aliases when available.
                    repair_aliases_raw = repair.get("locked_answer_aliases", []) or []
                    if isinstance(repair_aliases_raw, str):
                        repair_aliases_raw = [repair_aliases_raw]
                    if isinstance(repair_aliases_raw, list) and repair_aliases_raw:
                        seen_lower2: set[str] = set()
                        rebuilt: list[str] = []
                        locked_lower2 = locked_answer.lower().strip()
                        for a in repair_aliases_raw:
                            if not isinstance(a, str):
                                continue
                            ac = a.strip()
                            al = ac.lower()
                            if not ac or al == locked_lower2 or al in seen_lower2:
                                continue
                            if len(ac) > 50:
                                continue
                            seen_lower2.add(al)
                            rebuilt.append(ac)
                            if len(rebuilt) >= 5:
                                break
                        if rebuilt:
                            locked_answer_aliases = rebuilt

        # Final fallback (added 2026-05-01): if both the original lock and the
        # repair attempt produced "and"-joined answers (Haiku frequently does
        # this for "two of X" questions despite explicit prompt rules), accept
        # the raw answer rather than dead-ending the lock. The alias splitter
        # above has already converted joined aliases into per-component
        # aliases — those will match what students actually say. The
        # locked_answer itself being a sentence-shape phrase only means the
        # gate's literal-match path won't fire on it; aliases cover the gap.
        # Without this fallback, the user sees a card-rejection loop instead
        # of getting their topic locked (regression observed 2026-05-01).
        if not locked_answer:
            fallback_raw = locked_answer_raw or ""
            if " and " in fallback_raw.lower() and len(fallback_raw.split()) <= 15:
                # Try to derive an umbrella. Heuristic: "X and Y NOUN(S)" → "NOUN(S)".
                # Split on " and ", take the LONGEST part, drop leading modifiers
                # ("left", "right", "anterior", etc.). If that fails, use the longer part.
                parts = [p.strip() for p in re.split(r"\s+and\s+", fallback_raw, flags=re.IGNORECASE) if p.strip()]
                modifiers = {"left", "right", "anterior", "posterior", "superior",
                             "inferior", "upper", "lower", "medial", "lateral",
                             "deep", "superficial", "internal", "external"}
                # Longest-suffix umbrella attempt: from the longer part, strip
                # leading modifier tokens.
                longer = max(parts, key=lambda s: len(s.split())) if parts else fallback_raw
                tokens = longer.split()
                while tokens and tokens[0].lower() in modifiers:
                    tokens = tokens[1:]
                umbrella = " ".join(tokens) if tokens else longer
                # If the umbrella is too short (1 word) or empty, fall back to the
                # full longer part.
                if not umbrella or len(umbrella.split()) < 2:
                    umbrella = longer
                locked_answer = umbrella
                # Add each split part as an alias (deduped against locked_answer).
                for p in parts:
                    if p and p.lower() != locked_answer.lower():
                        if not any(p.lower() == a.lower() for a in locked_answer_aliases):
                            if len(locked_answer_aliases) < 5 and len(p) <= 50:
                                locked_answer_aliases.append(p)
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean._lock_anchors_call.and_join_fallback",
                    "result": f"raw={fallback_raw!r} → umbrella={locked_answer!r}",
                })

        # If the lock prompt didn't produce a full_answer, fall back to
        # locked_answer for backward compat — but flag it so we know
        # this session has a degraded full_answer.
        if not full_answer:
            full_answer = locked_answer
            state["debug"]["turn_trace"].append({
                "wrapper": "dean._lock_anchors_call.full_answer_fallback",
                "result": "full_answer empty in LLM output; fell back to locked_answer",
            })
        return {
            "locked_question": locked_question,
            "locked_answer": locked_answer,
            "locked_answer_aliases": locked_answer_aliases,
            "full_answer": full_answer,
            "rationale": str(parsed.get("rationale", "") or ""),
        }

    def _hint_plan_call(self, state: TutorState) -> list[str]:
        """
        Build a 3-step progressive hint plan after anchors lock.
        Returned hints are guidance intents for Teacher, not direct answer reveals.
        """
        conversation_history = render_history(state.get("messages", []))
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        dynamic_prompt = getattr(cfg.prompts, "dean_hint_plan_dynamic", "").format(
            locked_question=state.get("locked_question", ""),
            locked_answer=state.get("locked_answer", ""),
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )
        wrapper_delta = (
            getattr(cfg.prompts, "dean_hint_plan_delta", "")
            or getattr(cfg.prompts, "dean_hint_plan_static", "")
        )
        resp = _timed_create(
            self.client,
            state,
            "dean._hint_plan_call",
            model=self.model,
            temperature=0,
            max_tokens=220,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                chunks_str,
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Return strict JSON only."}],
        )
        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text)
        if parsed is None:
            return []
        hints = parsed.get("hints", [])
        if not isinstance(hints, list):
            return []
        cleaned = []
        for h in hints:
            s = str(h or "").strip()
            if s:
                cleaned.append(s)
        return cleaned[:3]

    def _extract_answer_parametric(self, state: TutorState) -> str:
        """
        Parametric (LLM-from-memory) answer extraction is disabled under the
        strict-groundedness policy: answers must come from retrieval over the
        indexed corpus, never from the model's training data. Callers fall
        back to deterministic chunk-derived anchors instead.
        """
        state["debug"]["turn_trace"].append({
            "wrapper": "dean._extract_answer_parametric",
            "result": "disabled_strict_groundedness",
        })
        return ""

    def _build_topic_ack_message(self, state: TutorState) -> str:
        """
        Build the deterministic topic-acknowledgement message that fires on
        the first dean_node turn after a topic locks. Two-line format:

            Got it — let's work on **{subsection}** from Chapter {N} → {section}.

            {locked_question}

        No LLM call. Robust to missing fields: degrades gracefully if any
        path component is empty (uses what's available, omits the rest).

        Leak guard (Tier 1 #1.4 fix, e2e bug E1): when the subsection
        title contains the locked_answer or any alias (e.g. subsection
        "The Aorta" with locked_answer "aorta"), rendering the
        subsection verbatim pre-reveals the answer. In that case,
        fall back to section-level phrasing ("let's work on a topic
        from Section X") which doesn't echo the answer.
        """
        locked_topic = state.get("locked_topic") or {}
        subsection = (
            str(locked_topic.get("subsection") or "").strip()
            or str(state.get("topic_selection") or "").strip()
            or "this topic"
        )
        section = str(locked_topic.get("section") or "").strip()
        path_str = str(locked_topic.get("path") or "")
        # Path format: "Chapter N: ... > section > subsection". Extract N.
        chapter_num = ""
        m = re.match(r"\s*Chapter\s+(\d+)", path_str)
        if m:
            chapter_num = m.group(1)
        else:
            chapter_str = str(locked_topic.get("chapter") or "")
            m2 = re.match(r"\s*(\d+)", chapter_str)
            if m2:
                chapter_num = m2.group(1)

        # ---- Leak guard: subsection contains locked_answer ----
        # If the subsection title would echo the locked answer, mask it.
        # Compare normalized content tokens of the subsection vs the
        # locked_answer + each alias — if any candidate's tokens are a
        # subset of the subsection's tokens, that's a leak risk.
        locked_answer_for_check = str(state.get("locked_answer") or "").strip()
        aliases_for_check = list(state.get("locked_answer_aliases") or [])
        masked = False
        if subsection and locked_answer_for_check:
            sub_tokens = set(_content_tokens(subsection))
            for cand in [locked_answer_for_check] + aliases_for_check:
                if not isinstance(cand, str):
                    continue
                cand_tokens = set(_content_tokens(cand))
                if not cand_tokens:
                    continue
                # Only flag distinctive overlaps (avoid masking on common
                # nouns like "system" / "muscle" alone). Use the existing
                # _is_distinctive_anchor helper for the candidate.
                if not _is_distinctive_anchor(cand):
                    continue
                if cand_tokens.issubset(sub_tokens):
                    masked = True
                    break

        # Build location clause defensively — only include parts we have.
        if chapter_num and section:
            location = f"from Chapter {chapter_num} → {section}"
        elif section:
            location = f"from {section}"
        elif chapter_num:
            location = f"from Chapter {chapter_num}"
        else:
            location = ""

        if masked:
            # Mask the subsection — use a generic placeholder that doesn't
            # leak the answer. Keep section/chapter context so the student
            # still knows where they are.
            if location:
                header = f"Got it — let's work on a topic {location}."
            else:
                header = "Got it — let's work on this topic."
        else:
            header = f"Got it — let's work on **{subsection}**"
            if location:
                header = f"{header} {location}"
            header = header + "."

        locked_question = str(state.get("locked_question") or "").strip()
        if not locked_question:
            # Defensive fallback: extremely rare since dean_node only sets
            # topic_just_locked=True after both anchors are populated. Still,
            # better to ask SOMETHING than nothing.
            fallback_reference = "this topic" if masked else subsection
            locked_question = (
                f"To get started: what do you already know about {fallback_reference}?"
            )

        return f"{header}\n\n{locked_question}"

    # ============================================================
    # Off-domain detector (Change 4, 2026-04-30; rewritten 2026-05-01).
    #
    # 2026-05-01: replaced the keyword regex (vape|smoke|alcohol|...)
    # with a Haiku classifier (conversation/classifiers.haiku_off_domain_check)
    # per the user's "LLM-only QC" directive. The classifier
    # disambiguates clinical questions involving substances ("how does
    # alcohol damage liver hepatocytes?") from off-domain chitchat
    # ("let's just get drunk"). The regex couldn't make this distinction —
    # both fired \balcohol\b. Validated 100% on 27 hand-curated cases.
    #
    # Activates only when student_state is already "irrelevant" (i.e.
    # dean's LLM classifier already decided the message is off-target).
    # The Haiku classifier then categorizes which kind of off-domain it
    # is (substance / sexual / profanity / chitchat / jailbreak /
    # answer_demand). Counter increments deterministic; only the
    # classification step is LLM-driven.
    # ============================================================

    def _is_off_domain_judgment(self, state: TutorState) -> bool:
        """Fast deterministic check: is the latest student message off-DOMAIN
        (category C) rather than domain-tangential (category B)?

        Default to True when student_state == 'irrelevant' AND the message
        contains an off-domain keyword. Otherwise False — the message
        might be a legitimate domain question that just isn't on the
        locked topic (let exploration_judge handle that).
        """
        latest_student_msg = ""
        for _msg in reversed(state.get("messages", []) or []):
            if (_msg or {}).get("role") == "student":
                latest_student_msg = str(_msg.get("content", "") or "")
                break
        if not latest_student_msg:
            return False
        # Off-domain detection (replaces _OFF_DOMAIN_REGEX per 2026-05-01
        # LLM-only directive). Haiku classifier disambiguates clinical
        # questions involving substances ("how does alcohol damage liver")
        # from off-domain chitchat ("let's get drunk"). Validated 100%
        # accuracy on 27 hand-curated cases.
        from conversation.classifiers import haiku_off_domain_check
        try:
            r = haiku_off_domain_check(latest_student_msg)
        except Exception:
            return False  # Fail-open: don't strike on classifier infra failure
        try:
            state["debug"]["turn_trace"].append({
                "wrapper": "classifiers.haiku_off_domain",
                "result": r.get("verdict", "clean"),
                "category": r.get("category", ""),
                "evidence": str(r.get("evidence", ""))[:160],
                "elapsed_s": float(r.get("_elapsed_s", 0.0)),
            })
        except Exception:
            pass
        return r.get("verdict") == "off_domain"

    def _build_strike_warning_brief(self, state: TutorState) -> str:
        """Return a string to append to dean_critique on strike turns.
        The TEACHER's prompt sees this and naturally weaves the warning
        into the response. Empty string when no warning applies.

        Strike 1: no warning (single low-effort can be just thinking)
        Strike 2: gentle nudge
        Strike 3: firmer warning + offer to switch
        Strike 4: handled separately (advances hint OR terminates)
        """
        help_n = int(state.get("help_abuse_count", 0))
        ot_n = int(state.get("off_topic_count", 0))

        warnings = []
        if help_n == 2:
            warnings.append(
                "STRIKE WARNING: This is the student's SECOND consecutive "
                "low-effort response. After delivering your normal hint, "
                "naturally and briefly remind them that 'even a partial guess "
                "or a wrong answer helps me see your thinking.' Keep it "
                "supportive, not shaming. Don't lecture."
            )
        elif help_n == 3:
            warnings.append(
                "STRIKE WARNING: This is the student's THIRD consecutive "
                "low-effort response. After delivering your normal hint, "
                "tell them they can switch topics or take a break if they're "
                "stuck. Mention that on the next pass they'll get a more "
                "direct hint automatically. Keep it brief, kind, and let "
                "them stay in control."
            )

        if ot_n == 2:
            warnings.append(
                "OFF-TOPIC WARNING: This is the student's SECOND consecutive "
                "off-domain message. Briefly acknowledge their question is "
                "outside our anatomy/textbook scope, and refocus them on "
                f"the locked topic ('{state.get('topic_selection', 'the current topic')}'). "
                "Don't engage with the off-domain content."
            )
        elif ot_n == 3:
            warnings.append(
                "OFF-TOPIC WARNING: This is the student's THIRD consecutive "
                "off-domain message. Tell them clearly that one more "
                "off-domain question will end the session, and ask them "
                "to focus on the locked topic. Brief, firm, polite."
            )

        return "\n\n".join(warnings)

    def reached_answer_gate(self, state: TutorState, student_msg: str) -> dict:
        """
        Strict gate that decides whether the student has STATED the locked
        answer in their most recent message. Replaces the old
        confidence_score >= 0.72 threshold which conflated step-correctness
        with answer-reach (the 'gravity counted as muscle pump' bug).

        Three-step strategy:

          Step A.1 — deterministic full token-overlap (free, fast):
            If the message contains all content tokens of locked_answer or
            any single alias AND no hedge marker is present, accept as
            FULL reach (coverage=1.0). Evidence is the matched phrase.

          Step A.2 — K-of-N partial reach (multi-component answers only):
            For locked_answer like "left and right coronary arteries" or
            "ingestion, propulsion, mechanical digestion, chemical
            digestion", split into N >= 2 components. Count how many
            components or aliases match by token overlap. If matches >=
            ceil(N/2), accept as PARTIAL reach (coverage=K/N, path=
            'partial_overlap' when K<N else 'overlap'). Closes the gap
            where a student saying just "LCA" on a multi-component answer
            was previously rejected.

          Step B — LLM paraphrase fallback (~one cheap call):
            Otherwise, ask the LLM whether the message paraphrases the
            answer. The LLM must quote a verbatim substring of the
            student message; quote-or-no-reach is enforced post-call so
            a hallucinated quote forces reached=False.

        Bias: reached=False on any ambiguity. False positives fabricate
        confirmations the student didn't earn — much worse than running
        one extra hint turn. Partial reach is a softer affirmation that
        still routes to assessment so the student gets credit for what
        they DID say without requiring full coverage of multi-component
        answers.

        Returns: dict with keys
          reached:    bool          (true on full or partial reach)
          coverage:   float in [0,1] (1.0 on full, K/N on partial,
                                      0.0 on no-reach paths)
          evidence:   str           (matched span or LLM quote)
          path:       str           (overlap | partial_overlap |
                                    paraphrase | hedge_block |
                                    no_overlap_no_paraphrase | no_lock |
                                    llm_no_quote | llm_parse_fail |
                                    llm_error)
          n_matched:  int           (components matched, multi-component only)
          n_total:    int           (total components, multi-component only)
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

            # Check full match against aliases ONLY when answer is single-
            # component. For multi-component answers, aliases are per-
            # component identifiers (the lock-anchor prompt produces e.g.
            # ["LCA", "left coronary artery", "RCA", "right coronary
            # artery"] for "left and right coronary arteries"). A single
            # alias match would cheat the K-of-N partial-reach logic
            # below — student says "LCA" and it'd count as full reach.
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

            # ---- Step A.2: K-of-N partial reach for multi-component answers ----
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
                    # Also dedup by subset/superset: if a longer match
                    # already covers this one's tokens (or vice versa),
                    # skip the shorter to avoid double-counting "left
                    # coronary" + "left coronary artery" as 2 hits.
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
        result = self._reached_check_llm(
            state, student_msg, locked_answer, aliases, msg_norm
        )
        # Ensure coverage key is present even on LLM path (binary 1.0 / 0.0).
        if "coverage" not in result:
            result["coverage"] = 1.0 if result.get("reached") else 0.0
        return result

    def _reached_check_llm(
        self,
        state: TutorState,
        student_msg: str,
        locked_answer: str,
        aliases: list[str],
        msg_norm: str,
    ) -> dict:
        """LLM step of reached_answer_gate. Quote-or-reject post-validation."""
        # Last 8 messages = ~4 tutor/student exchanges. Enough context for
        # the LLM to see what was being asked without prompt bloat.
        recent_history = render_history(state.get("messages", [])[-8:])
        aliases_str = ", ".join(aliases) if aliases else "(none)"

        static_block = getattr(cfg.prompts, "dean_reached_check_static", "")
        dynamic_template = getattr(cfg.prompts, "dean_reached_check_dynamic", "")
        if not static_block or not dynamic_template:
            # Defensive: prompts missing → don't try to fabricate a check.
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.reached_answer_gate",
                "result": "prompt_missing",
            })
            return {"reached": False, "evidence": "", "path": "prompt_missing"}

        dynamic_prompt = dynamic_template.format(
            locked_answer=locked_answer,
            aliases=aliases_str,
            recent_history=recent_history,
            student_msg=student_msg,
        )

        try:
            resp = _timed_create(
                self.client, state, "dean.reached_answer_gate",
                model=self.model,
                temperature=0,
                max_tokens=160,
                system=_cached_system(
                    getattr(cfg.prompts, "dean_base", ""),
                    static_block,
                    "",  # no chunks needed for this judgment
                    recent_history,
                    dynamic_prompt,
                ),
                messages=[{
                    "role": "user",
                    "content": "Decide and return strict JSON.",
                }],
            )
        except Exception as exc:
            # Any API hiccup → not reached. We'd rather extend a session
            # than fabricate a confirmation.
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.reached_answer_gate",
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

        # Strict gate: if the LLM claims reached, it must quote a verbatim
        # substring of the student message. Two checks (normalized first,
        # then raw lowercase) so we accept legit quotes that just differ
        # in punctuation/whitespace, but reject hallucinated quotes that
        # don't appear at all.
        if reached_claim:
            ev_norm = _normalize_text(evidence)
            quote_present = (
                bool(ev_norm) and ev_norm in msg_norm
            ) or (
                bool(evidence) and evidence.lower() in (student_msg or "").lower()
            )
            if not quote_present:
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.reached_answer_gate",
                    "result": "llm_no_quote",
                    "claimed_evidence": evidence[:120],
                })
                return {
                    "reached": False,
                    "evidence": evidence,  # kept for telemetry
                    "path": "llm_no_quote",
                }

        path = "paraphrase" if reached_claim else "no_overlap_no_paraphrase"
        return {
            "reached": reached_claim,
            "evidence": evidence if reached_claim else "",
            "path": path,
        }

    def _setup_call(self, state: TutorState) -> dict:
        """
        Dean setup classification call (single-call, no tool loop).
        Retrieval and answer locking are done in Python before this call.

        Returns:
            dict with student_state, student_reached_answer, hint_level,
                  search_needed, critique
        """
        conversation_history = render_history(state.get("messages", []))
        wrapper_delta = (
            getattr(cfg.prompts, "dean_setup_delta", "")
            or cfg.prompts.dean_setup_classify_static
        )
        dynamic_prompt = cfg.prompts.dean_setup_classify_dynamic.format(
            locked_answer=state.get("locked_answer", ""),
            locked_question=state.get("locked_question", ""),
            hint_level=state.get("hint_level", 0),
            turn_count=state.get("turn_count", 0),
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        resp = _timed_create(
            self.client, state, "dean._setup_call",
            model=self.model,
            temperature=0,
            max_tokens=220,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                chunks_str,
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Classify this turn and return JSON."}],
        )

        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text)
        if parsed is None:
            return self._setup_local_fallback(state)

        student_state = str(parsed.get("student_state", "irrelevant")).strip().lower()
        valid_states = {"correct", "partial_correct", "incorrect", "question", "irrelevant", "low_effort"}
        if student_state not in valid_states:
            student_state = "irrelevant"
        try:
            hint_level = int(parsed.get("hint_level", state.get("hint_level", 0)))
        except (TypeError, ValueError):
            hint_level = int(state.get("hint_level", 0))

        result = {
            "student_state": student_state,
            "student_reached_answer": bool(parsed.get("student_reached_answer", False)),
            "confidence_score": parsed.get("confidence_score", None),
            "hint_level": hint_level,
            "search_needed": False,
            "critique": str(parsed.get("critique", "") or ""),
        }
        for entry in reversed(state["debug"]["turn_trace"]):
            if entry.get("wrapper") == "dean._setup_call":
                entry["result"] = f"student_state={result['student_state']}, hint={result['hint_level']}"
                entry["locked_answer_final"] = state.get("locked_answer", "")
                entry["decision_effect"] = "classification_only"
                break
        return result

    def _setup_local_fallback(self, state: TutorState) -> dict:
        """
        Local classification fallback when Dean JSON parsing fails.
        Uses safe defaults — no domain-specific heuristics.
        """
        msg = _latest_student_message(state.get("messages", []))
        txt = (msg or "").strip().lower()
        locked = _normalize_text(state.get("locked_answer", "") or "")

        low_effort_set = {"", "idk", "i don't know", "i dont know", "don't know", "dont know", "help"}
        if txt in low_effort_set:
            student_state = "low_effort"
        elif locked and locked in _normalize_text(txt):
            student_state = "correct"
        elif "?" in txt:
            student_state = "question"
        else:
            student_state = "incorrect"

        result = {
            "student_state": student_state,
            "student_reached_answer": student_state == "correct" and bool(locked),
            "confidence_score": {
                "correct": 0.74,
                "partial_correct": 0.56,
                "question": 0.42,
                "incorrect": 0.24,
                "irrelevant": 0.10,
                "low_effort": 0.05,
            }.get(student_state, 0.35),
            "hint_level": int(state.get("hint_level", 0)),
            "search_needed": False,
            "critique": "Dean setup parse fallback used.",
        }
        state["debug"]["turn_trace"].append({
            "wrapper": "dean._setup_local_fallback",
            "decision_effect": "classification_fallback",
            "result": f"student_state={result['student_state']}, hint={result['hint_level']}",
        })
        return result

    def _evaluate_tutoring_draft(self, state: TutorState, teacher_draft: str) -> dict:
        """
        Two-stage quality gate for tutoring drafts:
        1) deterministic Python checks (fast, no API call)
        2) Dean LLM quality check when deterministic checks pass
        """
        deterministic = self._deterministic_tutoring_check(state, teacher_draft)
        state["debug"]["turn_trace"].append({
            "wrapper": "dean._deterministic_quality_check",
            "result": "PASS" if deterministic["pass"] else f"FAIL: {deterministic['critique']}",
            "reason_codes": deterministic.get("reason_codes", []),
        })
        if not deterministic["pass"]:
            return deterministic
        return self._quality_check_call(state, teacher_draft, phase="tutoring")

    def _deterministic_tutoring_check(self, state: TutorState, teacher_draft: str) -> dict:
        """
        Local deterministic checks to catch obvious violations before spending API tokens.
        """
        text = teacher_draft or ""
        lowered = _normalize_text(text)
        locked = _normalize_text(state.get("locked_answer", ""))
        student_state = str(state.get("student_state") or "").strip().lower()
        reason_codes: list[str] = []

        q_count = _question_count(text)
        if q_count == 0:
            reason_codes.append("missing_question")
        elif q_count > 1:
            reason_codes.append("multi_question")

        # Word-boundary reveal check. Use _is_distinctive_anchor so that
        # single-word distinctive anchors like "nucleus", "ganglion",
        # "pepsin" are checked too — previously the >=2 words filter
        # excluded them entirely (observed 2026-05-01 nidhi CNS session
        # where "nucleus" leaked verbatim through Hint 1, 2, and 3).
        # Common short anatomy nouns ("muscle", "nerve", "vein") still
        # skipped to avoid false-positives on legitimate scaffolding.
        if locked and _is_distinctive_anchor(locked):
            if re.search(rf"\b{re.escape(locked)}\b", lowered):
                reason_codes.append("reveal_risk")

        # Extended reveal check on aliases. Same distinctive-anchor rule:
        # single-word aliases like "ganglion" (the PNS counterpart to
        # "nucleus") get checked when distinctive.
        aliases = state.get("locked_answer_aliases") or []
        for alias in aliases:
            alias_norm = _normalize_text(alias or "")
            if alias_norm and _is_distinctive_anchor(alias_norm):
                if re.search(rf"\b{re.escape(alias_norm)}\b", lowered):
                    reason_codes.append("reveal_risk_alias")
                    break

        # Hint-leak detection (replaces _LETTER_HINT_PATTERNS regex per
        # 2026-05-01 LLM-only directive). Haiku classifier reads the
        # draft and decides if it contains a letter / blank / etymology
        # / MCQ / synonym / acronym leak. State-aware via the locked
        # answer + aliases passed in. Validated 96.7% accuracy / 100%
        # leak precision on 30 hand-curated cases (see
        # data/artifacts/classifiers/2026-05-01T21-03-51).
        from conversation.classifiers import (
            haiku_hint_leak_check, haiku_sycophancy_check,
        )
        # Build the args once; running both classifiers in parallel
        # below to keep latency at max(individual) rather than sum.
        hint_kwargs = {
            "draft": text,
            "locked_answer": state.get("locked_answer", "") or "",
            "aliases": state.get("locked_answer_aliases") or [],
        }
        # Sycophancy classifier needs student_state + reach_fired so it
        # can apply asymmetric stakes (affirmation is OK only when
        # student is correct AND reach gate fired).
        reach_fired_now = bool(state.get("student_reached_answer"))
        sycoph_kwargs = {
            "draft": text,
            "student_state": student_state,
            "reach_fired": reach_fired_now,
        }
        # Parallelize the two classifier calls — each is ~1.5–2s on
        # Haiku 4.5; running back-to-back would add 3–4s/turn while
        # parallel keeps it at ~2s/turn.
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as _ex:
                _hint_fut = _ex.submit(haiku_hint_leak_check, **hint_kwargs)
                _sycoph_fut = _ex.submit(haiku_sycophancy_check, **sycoph_kwargs)
                hint_result = _hint_fut.result()
                sycoph_result = _sycoph_fut.result()
        except Exception:
            # Fail-open: if classifier infra falls over, don't block
            # the draft on a phantom leak. The LLM Dean QC will still
            # examine the draft.
            hint_result = {"verdict": "clean", "evidence": "", "rationale": "classifier_error"}
            sycoph_result = {"verdict": "clean", "evidence": "", "rationale": "classifier_error"}
        if hint_result.get("verdict") == "leak":
            reason_codes.append("letter_hint")
            try:
                state["debug"]["turn_trace"].append({
                    "wrapper": "classifiers.haiku_hint_leak",
                    "result": "leak",
                    "leak_type": hint_result.get("leak_type", ""),
                    "evidence": str(hint_result.get("evidence", ""))[:160],
                    "elapsed_s": float(hint_result.get("_elapsed_s", 0.0)),
                })
            except Exception:
                pass

        # full_answer content-token leak: extract distinctive multi-char nouns
        # from the textbook full_answer and check if any appear word-bounded
        # in the teacher draft. We require 2+ such tokens to fire (one stray
        # term might be incidental scaffolding; two together is a leak).
        full_ans = _normalize_text(state.get("full_answer", "") or "")
        if full_ans:
            STOPS = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at",
                     "for", "with", "by", "from", "is", "are", "as", "into",
                     "that", "this", "these", "those", "their", "its"}
            distinctive = [
                t for t in full_ans.split()
                if len(t) >= 5 and t not in STOPS
            ]
            # Cap to top-8 distinctive tokens to keep the regex cost bounded.
            distinctive = distinctive[:8]
            hits = sum(
                1 for t in distinctive
                if re.search(rf"\b{re.escape(t)}\b", lowered)
            )
            if hits >= 2:
                reason_codes.append("reveal_risk_full_answer")

        if _sentence_count(text) > 4:
            reason_codes.append("verbosity")

        # Sycophancy detection (replaces _STRONG_AFFIRM_PATTERNS regex
        # per 2026-05-01 LLM-only directive). The Haiku classifier was
        # already invoked above in parallel with the hint-leak check;
        # we just consume its verdict here. State-aware: classifier
        # only fires "sycophantic" when student_state is NOT correct
        # OR reach gate did not fire. Validated 100% accuracy on 27
        # hand-curated cases (15 sycophantic + 12 clean).
        if sycoph_result.get("verdict") == "sycophantic":
            reason_codes.append("sycophancy_risk")
            try:
                state["debug"]["turn_trace"].append({
                    "wrapper": "classifiers.haiku_sycophancy",
                    "result": "sycophantic",
                    "evidence": str(sycoph_result.get("evidence", ""))[:160],
                    "elapsed_s": float(sycoph_result.get("_elapsed_s", 0.0)),
                })
            except Exception:
                pass

        prior_questions = _recent_tutor_questions(state.get("messages", []), limit=3)
        repetition_threshold = float(
            getattr(getattr(cfg, "thresholds", object()), "repetition_similarity", 0.9)
        )
        if _is_repetitive_question(_extract_question_text(text), prior_questions, repetition_threshold):
            reason_codes.append("question_repetition")

        if not reason_codes:
            return {
                "pass": True,
                "critique": "",
                "leak_detected": False,
                "reason_codes": [],
                "rewrite_instruction": "",
                "revised_teacher_draft": "",
                "parse_ok": True,
            }

        instruction_map = {
            "missing_question": "End with exactly one concrete Socratic question.",
            "multi_question": "Ask only one question total in the final sentence.",
            "reveal_risk": "Remove answer mentions and ask a non-revealing probe question.",
            "reveal_risk_alias": "Remove the alias term you mentioned and ask the student to articulate it themselves.",
            "reveal_risk_full_answer": "Remove the textbook nouns from the draft; probe with non-revealing language.",
            "verbosity": "Cut to at most 4 short sentences.",
            "generic_filler": "Replace generic empathy with a specific reasoning step.",
            "sycophancy_risk": "Do not strongly affirm; acknowledge uncertainty and probe reasoning.",
            "question_repetition": "Use a new angle that is not a near-duplicate of recent questions.",
            "letter_hint": "Do NOT use letter hints, first-letter clues, or rhyme hints. Ask about a property or relationship instead.",
        }
        primary = reason_codes[0]
        critique = f"Deterministic checks failed: {', '.join(reason_codes)}."
        return {
            "pass": False,
            "critique": critique,
            "leak_detected": any(c in reason_codes for c in (
                "reveal_risk", "reveal_risk_alias", "reveal_risk_full_answer", "letter_hint"
            )),
            "reason_codes": reason_codes,
            "rewrite_instruction": instruction_map.get(primary, "Improve clarity and ask one specific question."),
            "revised_teacher_draft": "",
            "parse_ok": True,
        }

    def _format_dean_critique(self, quality: dict) -> str:
        """
        Format retry critique into a structured instruction block for Teacher.
        """
        codes = quality.get("reason_codes") or ["other"]
        instruction = quality.get("rewrite_instruction") or "Tighten the draft and end with one specific question."
        critique = quality.get("critique", "")
        return (
            f"reason_codes={codes}\n"
            f"rewrite_instruction={instruction}\n"
            f"critique={critique}"
        )

    def _deterministic_assessment_check(self, state: TutorState, draft: str) -> dict:
        """
        Pre-LLM deterministic checks for assessment-phase drafts.

        Catches three classes of leak/violation that the LLM QC was
        observed to miss in the 2026-04-30 nidhi clinical session:
          1. Locked-answer or alias mentioned verbatim (whole-phrase, ≥2 tokens).
          2. Two or more distinctive nouns from full_answer recited.
          3. Internal chunk-citation patterns ([6], passage [7], reference 3).

        Returns the same shape as `_deterministic_tutoring_check` so it
        can short-circuit `_quality_check_call` for assessment.
        """
        text = draft or ""
        lowered = _normalize_text(text)
        reason_codes: list[str] = []

        locked = _normalize_text(state.get("locked_answer", ""))
        if locked and _is_distinctive_anchor(locked):
            if re.search(rf"\b{re.escape(locked)}\b", lowered):
                reason_codes.append("no_reveal")

        aliases = state.get("locked_answer_aliases") or []
        for alias in aliases:
            alias_norm = _normalize_text(alias or "")
            if alias_norm and _is_distinctive_anchor(alias_norm):
                if re.search(rf"\b{re.escape(alias_norm)}\b", lowered):
                    if "no_reveal" not in reason_codes:
                        reason_codes.append("no_reveal")
                    break

        # Hint-leak detection (replaces _LETTER_HINT_PATTERNS regex per
        # 2026-05-01 LLM-only directive). Same Haiku classifier as
        # tutoring path. Single call here (no parallel partner —
        # sycophancy isn't checked in assessment phase since the
        # student is being graded, not coached).
        from conversation.classifiers import haiku_hint_leak_check
        try:
            hint_result = haiku_hint_leak_check(
                draft=text,
                locked_answer=state.get("locked_answer", "") or "",
                aliases=state.get("locked_answer_aliases") or [],
            )
        except Exception:
            hint_result = {"verdict": "clean", "evidence": "", "rationale": "classifier_error"}
        if hint_result.get("verdict") == "leak":
            reason_codes.append("letter_hint")
            try:
                state["debug"]["turn_trace"].append({
                    "wrapper": "classifiers.haiku_hint_leak_assessment",
                    "result": "leak",
                    "leak_type": hint_result.get("leak_type", ""),
                    "evidence": str(hint_result.get("evidence", ""))[:160],
                    "elapsed_s": float(hint_result.get("_elapsed_s", 0.0)),
                })
            except Exception:
                pass

        full_ans = _normalize_text(state.get("full_answer", "") or "")
        if full_ans:
            STOPS = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at",
                     "for", "with", "by", "from", "is", "are", "as", "into",
                     "that", "this", "these", "those", "their", "its"}
            distinctive = [
                t for t in full_ans.split()
                if len(t) >= 5 and t not in STOPS
            ][:8]
            hits = sum(
                1 for t in distinctive
                if re.search(rf"\b{re.escape(t)}\b", lowered)
            )
            if hits >= 2 and "no_reveal" not in reason_codes:
                reason_codes.append("no_reveal")

        # Chunk-citation patterns. These are internal artifacts that must
        # never surface to the student. Observed: "from passage [6] and
        # [10]", "reference [7] and [10]", "according to passage 3".
        chunk_cite_patterns = [
            r"\bpassage\s*\[?\d+\]?",
            r"\breference\s*\[?\d+\]?",
            r"\[\s*\d+\s*\]\s*(?:and\s*\[\s*\d+\s*\])?",
            r"\bchunk\s*\d+",
        ]
        for pat in chunk_cite_patterns:
            if re.search(pat, text, flags=re.IGNORECASE):
                reason_codes.append("chunk_citation")
                break

        if not reason_codes:
            return {
                "pass": True, "critique": "", "leak_detected": False,
                "reason_codes": [], "rewrite_instruction": "",
                "revised_teacher_draft": "", "parse_ok": True,
            }

        instruction_map = {
            "no_reveal": "Remove answer terms / aliases / full_answer nouns and ask the student to reason from prior dialog instead.",
            "chunk_citation": "Do NOT cite internal chunk numbers like [6] or 'passage [7]' — these are internal artifacts and must never reach the student.",
            "letter_hint": "Do NOT use letter hints, first-letter clues, or rhyme hints. Probe a property or relationship instead.",
        }
        primary = reason_codes[0]
        critique = f"Deterministic checks failed: {', '.join(reason_codes)}."
        return {
            "pass": False, "critique": critique,
            "leak_detected": "no_reveal" in reason_codes or "letter_hint" in reason_codes,
            "reason_codes": reason_codes,
            "rewrite_instruction": instruction_map.get(primary, "Tighten the draft and remove the flagged content."),
            "revised_teacher_draft": "", "parse_ok": True,
        }

    def _teacher_preflight_brief(self, state: TutorState) -> str:
        """
        Non-LLM Dean guidance passed to Teacher before first draft each turn.
        This makes Teacher generation explicitly aware of Dean QC constraints.

        Change 4: also weaves in any pending narration brief (hint-advance
        narration on help_abuse=4, off-topic farewell on off_topic=4) and
        any escalating strike warnings for strikes 1-3.
        """
        student_state = state.get("student_state") or "unknown"
        hint_level = int(state.get("hint_level", 0))
        hint_plan = state.get("debug", {}).get("hint_plan", [])
        active_hint = ""
        if isinstance(hint_plan, list) and hint_plan and hint_level >= 1:
            idx = min(max(hint_level - 1, 0), len(hint_plan) - 1)
            active_hint = str(hint_plan[idx] or "")

        base = (
            "reason_codes=['preflight']\n"
            "rewrite_instruction=Generate one concise Socratic reply that will pass Dean QC.\n"
            "critique=Preflight constraints: exactly one question mark, 2-4 sentences, "
            "no generic filler lead-ins, no answer reveal by naming/elimination, and "
            "one concrete next reasoning step.\n"
            f"context=student_state:{student_state},hint_level:{hint_level},active_hint:{active_hint}"
        )

        # Change 4: pending narration brief (set by counter threshold actions
        # earlier in this turn). Highest priority — overrides normal critique.
        pending = str(state.get("_dean_warning_brief_pending") or "").strip()
        if pending:
            base = base + "\n\n=== SPECIAL NARRATION BRIEF (override) ===\n" + pending
            # Consume the brief so it doesn't fire again on retry.
            state["_dean_warning_brief_pending"] = ""

        # Change 4: escalating strike warnings (1-3). Lower priority than
        # the override brief above but added when present.
        strike_warning = self._build_strike_warning_brief(state)
        if strike_warning:
            base = base + "\n\n=== STRIKE WARNING ===\n" + strike_warning

        return base

    def _quality_check_call(self, state: TutorState, teacher_draft: str, phase: str = "tutoring") -> dict:
        """
        Dean's quality check call.
        Checks Teacher's draft against EULER 4 criteria + LeakGuard Level 3.

        Returns:
            dict: {"pass": bool, "critique": str, "leak_detected": bool}
        """
        # Deterministic pre-check for ASSESSMENT phase. Catches the most
        # common leak patterns (alias mention, chunk citation, multi-noun
        # full_answer recitation) before spending an LLM call. Mirrors the
        # tutoring-phase check but tuned for clinical drafts which are
        # 2-5 sentences (vs 1-3 for tutoring) and may legitimately
        # discuss anatomy in scenario form.
        # Observed in nidhi session 2026-04-30: "the RIGHT coronary artery"
        # + "passage [6] and [10]" leaked through because the assessment QC
        # prompt was both more permissive AND missing the alias / full_answer
        # context. This check runs FIRST so those don't ship.
        if phase == "assessment":
            det = self._deterministic_assessment_check(state, teacher_draft)
            state["debug"].setdefault("turn_trace", []).append({
                "wrapper": "dean._deterministic_assessment_check",
                "result": "PASS" if det["pass"] else f"FAIL: {det['critique']}",
                "reason_codes": det.get("reason_codes", []),
            })
            if not det["pass"]:
                return det

        last_student_msg = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "student":
                last_student_msg = msg.get("content", "")
                break
        conversation_history = render_history(state.get("messages", []))

        if phase == "assessment" and hasattr(cfg.prompts, "dean_quality_check_assessment_static"):
            wrapper_delta = (
                getattr(cfg.prompts, "dean_quality_check_assessment_delta", "")
                or cfg.prompts.dean_quality_check_assessment_static
            )
            aliases_for_qc = state.get("locked_answer_aliases") or []
            dynamic_prompt = cfg.prompts.dean_quality_check_assessment_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                locked_answer_aliases=", ".join(aliases_for_qc) if aliases_for_qc else "(none)",
                full_answer=state.get("full_answer", "") or state.get("locked_answer", ""),
                last_student_message=last_student_msg,
                teacher_draft=teacher_draft,
                conversation_history=conversation_history,
                **_domain_prompt_vars(),
            )
        else:
            wrapper_delta = (
                getattr(cfg.prompts, "dean_quality_check_tutoring_delta", "")
                or cfg.prompts.dean_quality_check_static
            )
            # B'.2 — pass the prior preflight critique (set by the deterministic
            # check or an earlier dean_critique on this turn) so the quality
            # checker's rewrite_instruction is targeted at the actual failure
            # mode, not generic. Empty on the first eval; populated on retry.
            prior_critique = str(state.get("dean_critique", "") or "").strip()
            aliases_for_qc = state.get("locked_answer_aliases") or []
            dynamic_prompt = cfg.prompts.dean_quality_check_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                locked_answer_aliases=", ".join(aliases_for_qc) if aliases_for_qc else "(none)",
                full_answer=state.get("full_answer", "") or state.get("locked_answer", ""),
                last_student_message=last_student_msg,
                teacher_draft=teacher_draft,
                prior_preflight_critique=prior_critique,
                conversation_history=conversation_history,
                **_domain_prompt_vars(),
            )

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        resp = _timed_create(
            self.client, state, "dean._quality_check_call",
            model=self.model,
            temperature=0,
            max_tokens=420,
            system=_cached_system(
                getattr(cfg.prompts, "dean_base", ""),
                wrapper_delta,
                chunks_str,
                conversation_history,
                dynamic_prompt,
            ),
            messages=[{"role": "user", "content": "Evaluate this Teacher draft."}],
        )

        text = (resp.content[0].text or "").strip()

        parsed = _extract_json_object(text)
        if parsed is None:
            has_pass = bool(re.search(r"\bpass\b", text, flags=re.IGNORECASE))
            has_fail = bool(re.search(r"\bfail\b", text, flags=re.IGNORECASE))
            inferred_pass = has_pass and not has_fail
            inferred_leak = bool(re.search(r"reveal|leak", text, flags=re.IGNORECASE))
            critique = (text or "").strip()
            if critique:
                critique = re.sub(r"\s+", " ", critique)[:220]
            if not critique:
                critique = "Could not parse quality check response."
            result = {
                "pass": inferred_pass,
                "critique": critique,
                "leak_detected": inferred_leak,
                "reason_codes": ["other"],
                "rewrite_instruction": "Return strict JSON object only.",
                "revised_teacher_draft": teacher_draft if not inferred_pass else "",
            }
            parse_ok = False
        else:
            result = parsed
            parse_ok = True

        raw_codes = result.get("reason_codes", [])
        if isinstance(raw_codes, str):
            reason_codes = [raw_codes]
        elif isinstance(raw_codes, list):
            reason_codes = [str(c) for c in raw_codes if str(c).strip()]
        else:
            reason_codes = ["other"]

        passed = bool(result.get("pass", False))
        revised_teacher_draft = str(result.get("revised_teacher_draft", "") or "").strip()
        # Update the timing entry already added by _timed_create with pass/fail result
        for entry in reversed(state["debug"]["turn_trace"]):
            if entry.get("wrapper") == "dean._quality_check_call":
                entry["result"] = "PASS" if passed else f"FAIL: {result.get('critique', '')}"
                entry["reason_codes"] = reason_codes
                entry["rewrite_instruction"] = result.get("rewrite_instruction", "")
                entry["parse_ok"] = parse_ok
                entry["decision_effect"] = "qc_pass" if passed else "qc_fail"
                if revised_teacher_draft:
                    entry["revised_teacher_draft"] = revised_teacher_draft
                break

        return {
            "pass": passed,
            "critique": result.get("critique", ""),
            "leak_detected": result.get("leak_detected", False),
            "reason_codes": reason_codes,
            "rewrite_instruction": result.get("rewrite_instruction", ""),
            "revised_teacher_draft": revised_teacher_draft,
            "parse_ok": parse_ok,
        }

    def _clinical_turn_call(self, state: TutorState) -> dict:
        """
        Evaluate one clinical-response turn and produce targeted coaching feedback.

        Returns:
            {
              "student_state": "correct|partial_correct|incorrect",
              "confidence_score": float,
              "pass": bool,
              "feedback_message": str
            }
        """
        conversation_history = render_history(state.get("messages", []))
        student_msg = _latest_student_message(state.get("messages", []))
        last_tutor_msg = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "tutor":
                last_tutor_msg = str(msg.get("content", "") or "").strip()
                break

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        wrapper_delta = getattr(
            cfg.prompts,
            "dean_clinical_turn_delta",
            "",
        ) or getattr(
            cfg.prompts,
            "dean_clinical_turn_static",
            (
                "You are the Dean evaluating a {student_descriptor}'s clinical reasoning response. "
                "Return strict JSON only."
            ),
        )
        dynamic_prompt = getattr(
            cfg.prompts,
            "dean_clinical_turn_dynamic",
            (
                "Locked answer: {locked_answer}\n"
                "Clinical turn: {clinical_turn_count}/{clinical_max_turns}\n"
                "Tutor question: {last_tutor_message}\n"
                "Student response: {student_message}\n"
                "Conversation history:\n{conversation_history}"
            ),
        ).format(
            locked_answer=state.get("locked_answer", ""),
            locked_answer_aliases=", ".join(state.get("locked_answer_aliases") or []) or "(none)",
            full_answer=state.get("full_answer", "") or state.get("locked_answer", ""),
            clinical_turn_count=state.get("clinical_turn_count", 0) + 1,
            clinical_max_turns=state.get("clinical_max_turns", 3),
            last_tutor_message=last_tutor_msg,
            student_message=student_msg,
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )

        system_blocks = _cached_system(
            getattr(cfg.prompts, "dean_base", ""),
            wrapper_delta,
            chunks_str,
            conversation_history,
            dynamic_prompt,
        )
        messages_payload = [{"role": "user", "content": "Evaluate this clinical response and return JSON."}]
        fingerprint = _request_fingerprint(system_blocks, messages_payload)
        dedupe_store = state["debug"].setdefault("_dedupe_results", {})
        last = dedupe_store.get("dean._clinical_turn_call")
        if isinstance(last, dict) and last.get("fingerprint") == fingerprint:
            state["debug"]["turn_trace"].append({
                "wrapper": "dean._clinical_turn_call.dedupe_guard",
                "result": "reused_previous_result_for_identical_input",
                "decision_effect": "dedupe_reuse",
            })
            cached_result = last.get("result")
            if isinstance(cached_result, dict):
                return dict(cached_result)

        resp = _timed_create(
            self.client,
            state,
            "dean._clinical_turn_call",
            model=self.model,
            temperature=0,
            max_tokens=360,
            system=system_blocks,
            messages=messages_payload,
        )

        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text)
        if parsed is None:
            fallback_result = self._clinical_turn_local_fallback(state, student_msg)
            dedupe_store["dean._clinical_turn_call"] = {
                "fingerprint": fingerprint,
                "result": fallback_result,
            }
            return fallback_result

        student_state = str(parsed.get("student_state", "incorrect")).strip().lower()
        if student_state not in {"correct", "partial_correct", "incorrect"}:
            student_state = "incorrect"
        try:
            confidence = _clamp01(float(parsed.get("confidence_score", 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        feedback = str(parsed.get("feedback_message", "") or "").strip()
        passed = bool(parsed.get("pass", False))

        # Keep gating deterministic on state+confidence.
        clinical_threshold = float(getattr(getattr(cfg, "thresholds", object()), "clinical_reached_confidence", 0.72))
        passed = bool(student_state == "correct" and confidence >= clinical_threshold)

        if not passed and not feedback:
            feedback = self._assessment_clinical_followup_fallback(state)

        result = {
            "student_state": student_state,
            "confidence_score": round(confidence, 3),
            "pass": passed,
            "feedback_message": feedback,
        }
        dedupe_store["dean._clinical_turn_call"] = {"fingerprint": fingerprint, "result": result}
        return result

    def _clinical_turn_local_fallback(self, state: TutorState, student_msg: str) -> dict:
        """
        Deterministic backup when clinical-eval JSON cannot be parsed.
        """
        txt = _normalize_text(student_msg)
        locked = _normalize_text(state.get("locked_answer", ""))
        has_reasoning = any(k in txt for k in ("because", "since", "due to", "therefore", "so "))

        if locked and locked in txt and has_reasoning:
            student_state = "correct"
            confidence = 0.75
            passed = True
            feedback = ""
        elif has_reasoning and len(txt) > 24:
            student_state = "partial_correct"
            confidence = 0.56
            passed = False
            feedback = self._assessment_clinical_followup_fallback(state)
        else:
            student_state = "incorrect"
            confidence = 0.30
            passed = False
            feedback = self._assessment_clinical_followup_fallback(state)

        state["debug"]["turn_trace"].append({
            "wrapper": "dean._clinical_turn_local_fallback",
            "result": f"state={student_state}, conf={confidence:.3f}, pass={passed}",
        })
        return {
            "student_state": student_state,
            "confidence_score": round(confidence, 3),
            "pass": passed,
            "feedback_message": feedback,
        }

    def _close_session_call(self, state: TutorState) -> dict:
        """
        Single batched close-session call:
        returns tiers + rationale + student-facing closeout + memory summary.
        """
        reached = bool(state.get("student_reached_answer", False))
        outcome = "reached_answer" if reached else "did_not_reach_answer"
        conversation_history = render_history(state.get("messages", []))
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        topic_selection = str(state.get("topic_selection", "") or "").strip() or _latest_student_message(
            state.get("messages", [])
        )
        dynamic_prompt = cfg.prompts.dean_close_session_dynamic.format(
            outcome=outcome,
            locked_answer=state.get("locked_answer", ""),
            locked_answer_aliases=", ".join(state.get("locked_answer_aliases") or []) or "(none)",
            full_answer=state.get("full_answer", "") or state.get("locked_answer", ""),
            topic_selection=topic_selection,
            student_reached_answer=state.get("student_reached_answer", False),
            hint_level=state.get("hint_level", 0),
            max_hints=state.get("max_hints", 3),
            student_answer_confidence=state.get("student_answer_confidence", 0.0),
            student_mastery_confidence=state.get("student_mastery_confidence", 0.0),
            clinical_opt_in=state.get("clinical_opt_in"),
            clinical_completed=state.get("clinical_completed", False),
            clinical_turn_count=state.get("clinical_turn_count", 0),
            clinical_max_turns=state.get("clinical_max_turns", 3),
            clinical_history=json.dumps(state.get("clinical_history", [])),
            weak_topics=json.dumps(state.get("weak_topics", [])),
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )

        system_blocks = _cached_system(
            getattr(cfg.prompts, "dean_base", ""),
            cfg.prompts.dean_close_session_static,
            chunks_str,
            conversation_history,
            dynamic_prompt,
        )
        messages_payload = [{"role": "user", "content": "Return strict JSON only."}]
        fingerprint = _request_fingerprint(system_blocks, messages_payload)
        dedupe_store = state["debug"].setdefault("_dedupe_results", {})
        last = dedupe_store.get("dean._close_session_call")
        if isinstance(last, dict) and last.get("fingerprint") == fingerprint:
            state["debug"]["turn_trace"].append({
                "wrapper": "dean._close_session_call.dedupe_guard",
                "result": "reused_previous_result_for_identical_input",
                "decision_effect": "dedupe_reuse",
            })
            cached_result = last.get("result")
            if isinstance(cached_result, dict):
                return dict(cached_result)

        try:
            resp = _timed_create(
                self.client,
                state,
                "dean._close_session_call",
                model=self.model,
                temperature=0,
                max_tokens=900,
                system=system_blocks,
                messages=messages_payload,
            )
            text = (resp.content[0].text or "").strip()
        except Exception:
            fallback = self._close_session_fallback_payload(state, parse_error=True)
            dedupe_store["dean._close_session_call"] = {"fingerprint": fingerprint, "result": fallback}
            return fallback

        parsed = _extract_json_object(text)
        if parsed is None:
            fallback = self._close_session_fallback_payload(state, parse_error=True)
            dedupe_store["dean._close_session_call"] = {"fingerprint": fingerprint, "result": fallback}
            return fallback

        tiers = {"strong", "proficient", "developing", "needs_review", "not_assessed"}
        core_tier = str(parsed.get("core_mastery_tier", "") or "").strip().lower()
        clinical_tier = str(parsed.get("clinical_mastery_tier", "") or "").strip().lower()
        mastery_tier = str(parsed.get("mastery_tier", "") or "").strip().lower()
        if core_tier not in tiers or clinical_tier not in tiers or mastery_tier not in tiers:
            fallback = self._close_session_fallback_payload(state, parse_error=True)
            dedupe_store["dean._close_session_call"] = {"fingerprint": fingerprint, "result": fallback}
            return fallback

        grading_rationale = str(parsed.get("grading_rationale", "") or "").strip()
        student_msg = str(parsed.get("student_facing_message", "") or "").strip()
        memory_summary = str(parsed.get("memory_summary", "") or "").strip()
        if not student_msg or not memory_summary:
            fallback = self._close_session_fallback_payload(state, parse_error=True)
            dedupe_store["dean._close_session_call"] = {"fingerprint": fingerprint, "result": fallback}
            return fallback

        # Post-filter the close-session output for chunk-citation patterns
        # only. Anchor mentions ARE allowed at closure — that's the proper
        # place to give the student the answer they didn't reach so they
        # know what to study (P1-D textbook-grounded summary). What's NEVER
        # ok at closure: internal chunk numbers like `[6]`, `passage [7]`.
        # Those are internal artifacts that should never reach the student
        # at any phase.
        chunk_cite_patterns = [
            r"\bpassage\s*\[?\d+\]?",
            r"\breference\s*\[?\d+\]?",
            r"\[\s*\d+\s*\]\s*(?:and\s*\[\s*\d+\s*\])?",
            r"\bchunk\s*\d+",
        ]
        for field_name, field_value in (
            ("student_facing_message", student_msg),
            ("grading_rationale", grading_rationale),
        ):
            for pat in chunk_cite_patterns:
                if re.search(pat, field_value, flags=re.IGNORECASE):
                    state["debug"].setdefault("turn_trace", []).append({
                        "wrapper": f"dean._close_session_call.chunk_cite_filter.{field_name}",
                        "result": "stripped chunk citation",
                        "original_excerpt": field_value[:200],
                    })
                    # Strip the citation pattern in-place rather than
                    # falling back to the whole hand-written close — keeps
                    # the LLM's substantive content while removing the
                    # internal artifact.
                    cleaned = re.sub(pat, "", field_value, flags=re.IGNORECASE)
                    cleaned = re.sub(r"\s+", " ", cleaned).strip()
                    if field_name == "student_facing_message":
                        student_msg = cleaned
                    elif field_name == "grading_rationale":
                        grading_rationale = cleaned
                    field_value = cleaned
        for entry in reversed(state["debug"]["turn_trace"]):
            if entry.get("wrapper") == "dean._close_session_call":
                entry["result"] = "close_session_json_ok"
                entry["decision_effect"] = "session_close_evaluated"
                break

        result = {
            "core_mastery_tier": core_tier,
            "clinical_mastery_tier": clinical_tier,
            "mastery_tier": mastery_tier,
            "grading_rationale": grading_rationale,
            "student_facing_message": student_msg,
            "memory_summary": memory_summary,
            "fallback_used": False,
        }
        dedupe_store["dean._close_session_call"] = {"fingerprint": fingerprint, "result": result}
        return result

    def _close_session_fallback_payload(self, state: TutorState, parse_error: bool = False) -> dict:
        """
        Deterministic fallback if close-session JSON is malformed or the call fails.
        """
        reached = bool(state.get("student_reached_answer", False))
        clinical_done = bool(state.get("clinical_completed", False))
        if reached and clinical_done:
            core = "proficient"
            clinical = "proficient"
            overall = "proficient"
        elif reached:
            core = "developing"
            clinical = "not_assessed"
            overall = "developing"
        else:
            core = "needs_review"
            clinical = "not_assessed"
            overall = "needs_review"

        topic = str(state.get("topic_selection", "") or "").strip() or "this topic"
        answer = str(state.get("locked_answer", "") or "").strip()
        if reached and answer:
            student_facing = (
                f"You reached the core answer ({answer}) for {topic}. "
                "Strong progress today—let’s keep building clinical transfer on the next pass."
            )
        elif answer:
            student_facing = (
                f"The correct answer for {topic} is {answer}. "
                "Good effort—this topic is marked for focused review next session."
            )
        else:
            student_facing = (
                "Good effort this session. We’ll revisit this topic with a tighter step-by-step approach next time."
            )

        memory_summary = (
            f"Session on {topic}. "
            f"Reached answer: {reached}. "
            f"Overall tier: {overall}. "
            f"Hint level ended at {state.get('hint_level', 0)}."
        )
        state["debug"]["turn_trace"].append({
            "wrapper": "dean._close_session_fallback",
            "decision_effect": "fallback_used",
            "result": "used_parse_fallback" if parse_error else "used_fallback",
        })
        return {
            "core_mastery_tier": core,
            "clinical_mastery_tier": clinical,
            "mastery_tier": overall,
            "grading_rationale": "Fallback grading used due to malformed or unavailable close-session output.",
            "student_facing_message": student_facing,
            "memory_summary": memory_summary,
            "fallback_used": True,
        }

    def _assessment_clinical_fallback(self, state: TutorState) -> str:
        """
        Dean-written fallback clinical question if Teacher fails quality twice.
        """
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        system = _apply_domain_vars(
            "You are a {domain_short} tutor writing a single {assessment_dimension} question. "
            "Do not restate the answer. Ask exactly one question grounded in {assessment_dimension_examples}. "
            "2-3 sentences max."
        )
        user_msg = (
            f"Correct answer (for context): {state.get('locked_answer', '')}\n\n"
            f"Relevant textbook chunks:\n{chunks_str}\n\n"
            "Write one clinical question only."
        )
        resp = _timed_create(
            self.client, state, "dean.assessment_fallback",
            model=self.model,
            max_tokens=220,
            system=_cached_system(system, "", "", "", ""),
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text

    def _assessment_clinical_followup_fallback(self, state: TutorState) -> str:
        """
        Dean-written fallback coaching follow-up for clinical multi-turn loops.
        Must include what was right, what to correct, and one follow-up question.
        """
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        student_msg = _latest_student_message(state.get("messages", []))
        system = _apply_domain_vars(
            "You are a {domain_short} tutor giving corrective coaching on {assessment_dimension}. "
            "Write 3-4 sentences max. Include exactly: "
            "1) 'What you got right:' with one specific point, "
            "2) 'What to correct next:' with one specific correction, "
            "3) one final sentence that asks exactly one follow-up question."
        )
        user_msg = (
            f"Locked answer for context: {state.get('locked_answer', '')}\n"
            f"Clinical turn: {state.get('clinical_turn_count', 0) + 1}/{state.get('clinical_max_turns', 3)}\n"
            f"Student response: {student_msg}\n\n"
            f"Relevant textbook chunks:\n{chunks_str}\n\n"
            "Write the coaching follow-up message now."
        )
        resp = _timed_create(
            self.client, state, "dean.assessment_clinical_followup_fallback",
            model=self.model,
            max_tokens=260,
            system=_cached_system(system, "", "", "", ""),
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text

    def _dean_fallback(self, state: TutorState) -> str:
        """Dean writes directly when Teacher fails twice. Generic safe Socratic nudge."""
        history = render_history(state.get("messages", []))
        domain_short = getattr(cfg.domain, "short", "the subject")
        system = _apply_domain_vars(
            "You are a Socratic {domain_short} tutor. The student is working through a problem. "
            "Do NOT give the answer or any specific facts. "
            "Ask one open-ended question that gets them thinking about what they already know. "
            "2 sentences max. Must end with a question."
        )
        resp = _timed_create(
            self.client, state, "dean.fallback",
            model=self.model,
            max_tokens=128,
            system=_cached_system(system, "", "", "", ""),
            messages=[
                {"role": "user", "content": f"Conversation so far:\n{history}\n\nWrite a safe fallback question."}
            ],
        )
        return resp.content[0].text

    def _log_prompt(self, conv_id: str, turn: int, wrapper: str, prompt: str) -> None:
        """Save full assembled prompt to data/artifacts/session_prompts/."""
        try:
            out_dir = Path(cfg.paths.artifacts) / "session_prompts"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{conv_id}_turn_{turn}_{wrapper}.txt"
            out_path.write_text(prompt)
        except Exception:
            pass  # non-fatal

    def _log_intervention(self, conv_id: str, turn: int, critique: str, draft: str) -> None:
        """Append Dean intervention record to data/artifacts/dean_interventions/."""
        try:
            out_dir = Path(cfg.paths.artifacts) / "dean_interventions"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{conv_id}_interventions.json"

            existing = []
            if out_path.exists():
                try:
                    existing = json.loads(out_path.read_text())
                except json.JSONDecodeError:
                    existing = []

            existing.append({"turn": turn, "critique": critique, "rejected_draft": draft})
            out_path.write_text(json.dumps(existing, indent=2))
        except Exception:
            pass  # non-fatal


# --- Formatting helpers (shared with teacher.py pattern) ---

def _format_chunks(chunks: list[dict]) -> str:
    if not chunks:
        return "(no textbook passages retrieved yet)"
    parts = []
    for i, chunk in enumerate(chunks, 1):
        chapter = chunk.get("chapter_title", "")
        section = chunk.get("section_title", "")
        subsection = chunk.get("subsection_title", "")
        page = chunk.get("page", "")
        score = chunk.get("score")
        location = " > ".join(filter(None, [chapter, section, subsection]))
        meta_bits = []
        if page:
            meta_bits.append(f"p.{page}")
        if isinstance(score, (int, float)):
            meta_bits.append(f"score={float(score):.2f}")
        meta = f" ({', '.join(meta_bits)})" if meta_bits else ""
        parts.append(f"[{i}]{meta} {location}\n---\n{chunk.get('text', '')}")
    return "\n\n===\n\n".join(parts)
