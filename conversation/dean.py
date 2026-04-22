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

# Sonnet pricing (per million tokens)
_PRICE_IN = 3.0
_PRICE_OUT = 15.0
_BANNED_FILLER_PREFIXES = (
    "i can see",
    "i notice",
    "i hear you",
    "that's okay",
)
_STRONG_AFFIRM_PATTERNS = (
    r"\bexactly\b",
    r"\bthat'?s right\b",
    r"\byou got it\b",
    r"\bcorrect\b",
    r"\bperfect\b",
    r"\byes[, ]+that'?s\b",
    r"\byou'?re right\b",
)
_RETRIEVAL_LOW_SIGNAL = {
    "",
    "idk",
    "i don't know",
    "i dont know",
    "dont know",
    "don't know",
    "not sure",
    "no idea",
    "help",
    "this",
    "that",
    "it",
    "yes",
    "no",
}
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
    Stable cacheable system structure:
      1) role_base + wrapper_delta
      2) frozen retrieved chunks/propositions
      3) rendered conversation history
      4) turn-local deltas (uncached)
    """
    blocks: list[dict] = []
    # Haiku 4.5 minimum is ~2048 actual tokens. Our estimate is len/4 and often
    # overshoots vs the tokenizer on structured/repetitive text, so require a comfortable margin.
    cache_min_tokens = 4000
    role_base = _apply_domain_vars(role_base)
    wrapper_delta = _apply_domain_vars(wrapper_delta)
    chunks = _apply_domain_vars(chunks)
    turn_deltas = _apply_domain_vars(turn_deltas)
    cached_primary = "\n\n".join(part for part in [role_base, wrapper_delta, chunks, history] if part)

    # Anthropic caching needs large enough cacheable content.
    # If stable content is short, include turn-local deltas in the cacheable block
    # so prefix caching can still activate from turn 2 onward.
    if turn_deltas and _estimate_tokens(cached_primary) < cache_min_tokens:
        cached_primary = "\n\n".join(part for part in [cached_primary, turn_deltas] if part)
        turn_deltas = ""

    if cached_primary:
        blocks.append({"type": "text", "text": cached_primary, "cache_control": {"type": "ephemeral"}})
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
    extra_headers = kwargs.pop("extra_headers", {})
    extra_headers["anthropic-beta"] = "prompt-caching-2024-07-31"
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


def _has_strong_affirmation(text: str) -> bool:
    first = _normalize_text(_first_sentence(text))
    if not first:
        return False
    return any(re.search(pat, first) for pat in _STRONG_AFFIRM_PATTERNS)


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


def _deterministic_anchor_fallback(chunks: list[dict], topic: str) -> tuple[str, str]:
    """
    Last-resort anchors when the LLM lock call fails twice.

    Heuristic: scan retrieved propositions for the first short noun phrase that
    looks like a terminal anatomical/concept term. If nothing useful, return ("", "").
    Caller will gracefully degrade.
    """
    if not chunks:
        return "", ""
    # Look for a concept/term pattern in top chunk text.
    for ch in chunks[:3]:
        text = str(ch.get("text", "") or "").strip()
        if not text:
            continue
        # Prefer a short phrase ending in a nerve/muscle/space/structure noun.
        for m in re.finditer(
            r"\b([A-Za-z]+(?:\s[A-Za-z]+){0,2}\s(?:nerve|muscle|artery|vein|joint|space|ligament|tendon|plexus))\b",
            text,
        ):
            phrase = m.group(1).strip().lower()
            # Reject super-generic matches.
            if phrase.split()[0] in {"the", "a", "this", "that", "which", "any"}:
                continue
            if 2 <= len(phrase.split()) <= 4:
                q = f"Which {phrase.split()[-1]} is central to: {topic}?"
                return q, phrase
    return "", ""


def _sanitize_locked_answer(
    candidate: str,
    chunks: list[dict],
    prior: str = "",
) -> tuple[str, str]:
    """
    Keep locked_answer concise.
    Grounding is enforced by the lock-anchors LLM prompt itself; here we avoid
    brittle exact-string filters that can reject valid semantically-correct anchors.
    """
    cand = (candidate or "").strip()
    if not cand:
        return (prior or "").strip(), "empty_input"

    cand_norm = _normalize_text(cand)
    prior_norm = _normalize_text(prior or "")

    # locked_answer must be a canonical short noun phrase (1-5 words).
    # If it's longer, the Dean produced a sentence/description, not an anchor.
    word_count = len(cand_norm.split())
    if word_count > 6:
        return prior_norm, "wiped_too_long"
    # Also reject anchors that are clearly sentences (contain verbs like "innervates",
    # "arises", "branches", "passes", "courses", etc.), even if short enough.
    sentence_markers = (
        "innervates", "innervate", "arises", "branches", "passes", "courses",
        "supplies", "controls", "causes", "results", "from the", "through the",
    )
    if any(marker in cand_norm for marker in sentence_markers):
        return prior_norm, "wiped_sentence_like"

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


def _is_low_effort_topic_reply(student_text: str) -> bool:
    txt = _normalize_text(student_text)
    if not txt:
        return True
    low_effort_set = {
        "idk", "i don't know", "i dont know", "don't know", "dont know",
        "not sure", "no idea", "help",
        "hi", "hello", "hey", "yo", "sup", "start", "continue",
    }
    if txt in low_effort_set:
        return True
    tokens = [t for t in txt.split() if t]
    if len(tokens) == 1 and len(tokens[0]) <= 3:
        return True
    greeting_tokens = {"hi", "hello", "hey", "yo", "sup", "there", "pls", "please"}
    if tokens and len(tokens) <= 2 and all(t in greeting_tokens for t in tokens):
        return True
    return False


def _is_generic_topic_reply(student_text: str) -> bool:
    """
    Detect broad/generic topic names that are not specific enough to lock.
    Uses domain config for the generic terms list.
    """
    txt = _normalize_text(student_text)
    if not txt:
        return True
    generic_terms = set(getattr(cfg.domain, "generic_topic_terms", []))
    if txt in generic_terms:
        return True
    # Also check multi-word generic terms
    for term in generic_terms:
        if " " in term and txt == _normalize_text(term):
            return True
    tokens = [t for t in txt.split() if t]
    if len(tokens) == 1 and len(tokens[0]) <= 5:
        return True
    return False


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


def _is_ambiguous_retrieval_query(text: str) -> bool:
    txt = _normalize_text(text)
    if txt in _RETRIEVAL_LOW_SIGNAL:
        return True
    tokens = [t for t in txt.split() if t]
    if len(tokens) >= 3:
        return False
    # Allow specific one/two-word domain entities (e.g., "axillary nerve", "Newton's third law").
    if len(tokens) in (1, 2):
        if all(len(t) >= 4 for t in tokens):
            return False
    return True


def _build_retrieval_query(state: TutorState) -> str:
    """
    Build a retrieval query that is robust to noisy student phrasing.
    Preference:
      1) selected scoped topic
      2) latest student message
      3) blend both when student adds useful detail
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
        and latest_norm not in _RETRIEVAL_LOW_SIGNAL
        and len(latest.split()) >= 3
        and latest_norm != topic_norm
        and topic_norm not in latest_norm
        and latest_norm not in topic_norm
    ):
        # Keep topic anchor but preserve fresh student intent/detail.
        candidate = f"{topic}. {latest}"

    cleaned = _clean_retrieval_query(candidate)
    if _is_ambiguous_retrieval_query(cleaned):
        fallback = _clean_retrieval_query(topic)
        if fallback and not _is_ambiguous_retrieval_query(fallback):
            return fallback
        return ""
    return cleaned


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
        self.client = anthropic.Anthropic()
        self.model = cfg.models.dean
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
            messages = list(state.get("messages", []))
            topic_options = list(state.get("topic_options", []))

            if not topic_options:
                latest_student = _latest_student_message(messages)
                if _is_low_effort_topic_reply(latest_student):
                    state["hint_level"] = 0
                    example = getattr(cfg.domain, "example_topic_specific", "a specific concept")
                    reprompt = (
                        f"Please name a specific study topic to begin "
                        f"(for example: {example})."
                    )
                    messages.append({"role": "tutor", "content": reprompt})
                    return {
                        "messages": messages,
                        "topic_confirmed": False,
                        "topic_options": [],
                        "topic_question": "",
                        "topic_selection": "",
                        "pending_user_choice": {},
                        "retrieved_chunks": [],
                        "locked_question": "",
                        "locked_answer": "",
                        "hint_level": 0,
                        "student_state": "question",
                        "debug": state["debug"],
                    }
                scoped = teacher.draft_topic_engagement(state)
                scoped_q = str(scoped.get("question", "") or "").strip()
                topic_options = list(scoped.get("options", []) or [])
                scoped_msg = str(scoped.get("message", "") or "").strip()
                if not scoped_msg:
                    scoped_msg = scoped_q
                # Inline numbered options in the tutor text so any client (including
                # plain-text/CLI) can see and pick them. UIs can still render cards.
                if topic_options:
                    numbered = "\n".join(
                        f"  {i+1}. {opt}" for i, opt in enumerate(topic_options)
                    )
                    scoped_msg = (
                        f"{scoped_msg}\n\n{numbered}\n\n"
                        f"Reply with a number (1-{len(topic_options)}) or paste the option text."
                    )
                messages.append({"role": "tutor", "content": scoped_msg})
                return {
                    "messages": messages,
                    "topic_confirmed": False,
                    "topic_options": topic_options,
                    "topic_question": scoped_q,
                    "topic_selection": "",
                    "pending_user_choice": {"kind": "topic", "options": topic_options},
                    "retrieved_chunks": [],  # force fresh retrieval once topic is selected
                    "locked_question": "",
                    "locked_answer": "",
                    "hint_level": 0,
                    "student_state": None,
                    "debug": state["debug"],
                }

            # Options already shown: require explicit selection.
            latest_student = _latest_student_message(messages)
            selected = _match_topic_selection(latest_student, topic_options)
            if not selected:
                # Allow free-text custom topic only if it's a specific, concise topic
                # phrase (≤10 words, non-greeting, non-generic). Long rambling
                # replies like "I'll go with the first one about X" should NOT
                # become the topic string — force a reprompt instead.
                if latest_student and not _is_low_effort_topic_reply(latest_student):
                    if not _is_generic_topic_reply(latest_student):
                        tokens = latest_student.strip().split()
                        if 1 <= len(tokens) <= 10:
                            selected = latest_student.strip()

            if not selected:
                state["hint_level"] = 0
                if _is_generic_topic_reply(latest_student):
                    example = getattr(cfg.domain, "example_topic_specific", "a specific concept")
                    reprompt = (
                        f"Please choose one focus card, or type a more specific topic "
                        f"(for example: {example})."
                    )
                else:
                    reprompt = (
                        "Please pick one of the focus cards below, or write a different topic "
                        "you want to explore."
                    )
                messages.append({"role": "tutor", "content": reprompt})
                return {
                    "messages": messages,
                    "topic_confirmed": False,
                    "topic_options": topic_options,
                    "topic_question": state.get("topic_question", ""),
                    "topic_selection": "",
                    "pending_user_choice": {"kind": "topic", "options": topic_options},
                    "hint_level": 0,
                    "student_state": "question",
                    "debug": state["debug"],
                }

            # Convert the latest student selection into concrete topic text so
            # retrieval/classification runs on semantic content, not "2".
            state["messages"] = _replace_latest_student_message(messages, selected)
            state["topic_confirmed"] = True
            state["topic_selection"] = selected
            state["topic_options"] = []
            state["topic_question"] = ""
            state["pending_user_choice"] = {}
            # Retrieval is single-fire per session. If it already fired earlier,
            # keep existing chunks instead of clearing and re-querying.
            if int(state.get("debug", {}).get("retrieval_calls", 0)) <= 0:
                state["retrieved_chunks"] = []
            state["locked_question"] = ""
            state["locked_answer"] = ""
            state["hint_level"] = 0
            topic_just_locked = True

        if topic_just_locked:
            self._retrieve_on_topic_lock(state)
            anchors = self._lock_anchors_call(state)
            state["locked_question"] = str(anchors.get("locked_question", "") or "").strip()
            state["locked_answer"] = str(anchors.get("locked_answer", "") or "").strip()
            if not state["locked_question"] or not state["locked_answer"]:
                anchor_fail_count = int(state.get("debug", {}).get("anchor_fail_count", 0)) + 1
                state["debug"]["anchor_fail_count"] = anchor_fail_count
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.anchor_extraction_failed",
                    "result": f"anchors empty (attempt {anchor_fail_count}) — topic too broad or retrieval weak",
                    "rationale": str(anchors.get("rationale", "") or ""),
                })
                # After 2 failed attempts, fall back to a deterministic anchor from
                # the top retrieved proposition rather than looping on 'narrower focus'.
                if anchor_fail_count >= 2:
                    forced_q, forced_a = _deterministic_anchor_fallback(
                        state.get("retrieved_chunks", []),
                        state.get("topic_selection", "") or "this topic",
                    )
                    if forced_q and forced_a:
                        state["locked_question"] = forced_q
                        state["locked_answer"] = forced_a
                        state["debug"]["turn_trace"].append({
                            "wrapper": "dean.anchor_forced_fallback",
                            "result": "Deterministic fallback anchors accepted after 2 failures",
                            "locked_question": forced_q,
                            "locked_answer": forced_a,
                        })
                        # Continue into tutoring with the forced anchors.
                    else:
                        # Truly nothing useful retrieved — give up gracefully.
                        messages = list(state.get("messages", []))
                        messages.append({
                            "role": "tutor",
                            "content": (
                                "I'm having trouble finding a focused angle for this topic "
                                "in my materials. Let's try a different, more specific question "
                                "— what aspect would you like to start with?"
                            ),
                        })
                        return {
                            "messages": messages,
                            "topic_confirmed": False,
                            "topic_options": [],
                            "topic_question": "",
                            "topic_selection": "",
                            "pending_user_choice": {},
                            "retrieved_chunks": [],
                            "locked_question": "",
                            "locked_answer": "",
                            "hint_level": 0,
                            "student_state": "question",
                            "debug": state["debug"],
                        }
                else:
                    messages = list(state.get("messages", []))
                    example = getattr(cfg.domain, "example_topic_specific", "a specific concept")
                    reprompt = (
                        "I need a narrower focus before we begin tutoring. "
                        f"Please pick one focus card or type a specific topic (for example: {example})."
                    )
                    messages.append({"role": "tutor", "content": reprompt})
                    return {
                        "messages": messages,
                        "topic_confirmed": False,
                        "topic_options": [],
                        "topic_question": "",
                        "topic_selection": "",
                        "pending_user_choice": {},
                        # Keep retrieved chunks so a narrowed follow-up can proceed
                        # without re-firing retrieval in the same session.
                        "retrieved_chunks": state.get("retrieved_chunks", []),
                        "locked_question": "",
                        "locked_answer": "",
                        "hint_level": 0,
                        "student_state": "question",
                        "debug": state["debug"],
                    }
            if int(state.get("hint_level", 0)) <= 0:
                state["hint_level"] = 1
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.anchors_locked",
                "locked_question": state["locked_question"],
                "locked_answer": state["locked_answer"],
                "rationale": str(anchors.get("rationale", "") or ""),
            })
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

        if topic_just_locked:
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
        reached_threshold = float(getattr(getattr(cfg, "thresholds", object()), "reached_answer_confidence", 0.72))
        model_reached = bool(eval_result.get("student_reached_answer", False))
        eval_result["student_reached_answer"] = (
            model_reached
            and confidence_score >= reached_threshold
            and bool(state.get("locked_answer", ""))
        )

        # Apply eval results to state
        state["student_state"] = eval_result["student_state"]
        state["student_reached_answer"] = eval_result["student_reached_answer"]
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
                f"reached={state['student_reached_answer']}"
            ),
            "locked_answer": state.get("locked_answer", ""),
        })

        hint_before = int(state.get("hint_level", 0))
        hint_reason = "unchanged"
        # Hint progression:
        # - increment only for "incorrect" once tutoring is unlocked (hint>=1)
        # - allow max_hints + 1 to signal "hints exhausted" routing in after_dean
        if eval_result["student_state"] == "incorrect":
            current_hint = int(state.get("hint_level", 0))
            if current_hint <= 0:
                current_hint = 1
            if current_hint >= state["max_hints"]:
                next_hint = state["max_hints"] + 1
            else:
                next_hint = current_hint + 1
            state["hint_level"] = min(next_hint, state["max_hints"] + 1)
            hint_reason = "incorrect_increment"
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
                "retrieved_chunks": state["retrieved_chunks"],
                "topic_confirmed": state.get("topic_confirmed", False),
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
                "retrieved_chunks": state["retrieved_chunks"],
                "topic_confirmed": state.get("topic_confirmed", False),
                "topic_options": state.get("topic_options", []),
                "topic_question": state.get("topic_question", ""),
                "topic_selection": state.get("topic_selection", ""),
                "pending_user_choice": state.get("pending_user_choice", {}),
                "help_abuse_count": state.get("help_abuse_count", 0),
                "dean_retry_count": 0,
                "dean_critique": "",
                "debug": state["debug"],
            }

        # Help abuse gating (pure Python counter)
        if state["student_state"] == "low_effort":
            state["help_abuse_count"] = state.get("help_abuse_count", 0) + 1
        else:
            state["help_abuse_count"] = 0

        if state["help_abuse_count"] >= cfg.dean.help_abuse_threshold:
            state["hint_level"] = min(state["hint_level"] + 1, state["max_hints"] + 1)
            state["help_abuse_count"] = 0
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.hint_progress",
                "hint_before": hint_after,
                "hint_after": int(state.get("hint_level", 0)),
                "hint_reason": "help_abuse_threshold",
                "active_hint": active_hint,
            })

        # Teacher drafts one response.
        # Provide Dean QC guidance preflight on first attempt so Teacher is
        # aligned before generation, not only after a rejection.
        state["dean_critique"] = self._teacher_preflight_brief(state)
        draft = teacher.draft_socratic(state)
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
            "retrieved_chunks": state["retrieved_chunks"],
            "topic_confirmed": state.get("topic_confirmed", False),
            "topic_options": state.get("topic_options", []),
            "topic_question": state.get("topic_question", ""),
            "topic_selection": state.get("topic_selection", ""),
            "pending_user_choice": state.get("pending_user_choice", {}),
            "help_abuse_count": state["help_abuse_count"],
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
        chunks = search_textbook(query, self.retriever, top_k=12)
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

    def _lock_anchors_call(self, state: TutorState) -> dict:
        """
        Lock both question and answer anchors immediately after topic-lock retrieval.
        Returns a dict with: locked_question, locked_answer, rationale.
        """
        topic_selection = str(state.get("topic_selection", "") or "").strip()
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
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
                "rationale": "parse_failed",
            }

        locked_question = str(parsed.get("locked_question", "") or "").strip()
        locked_answer_raw = str(parsed.get("locked_answer", "") or "").strip()
        locked_answer, sanitize_action = _sanitize_locked_answer(
            locked_answer_raw,
            state.get("retrieved_chunks", []),
            "",
        )
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
                max_tokens=140,
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
                        "NO cord origins, NO supporting facts — only the target term."
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
        return {
            "locked_question": locked_question,
            "locked_answer": locked_answer,
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
        Fallback: ask Claude directly for the answer when RAG chunks don't yield a locked_answer.
        Uses domain config to frame the expected answer format. Short, cheap call (max_tokens=15).
        Returns a short answer string or "" if unclear.
        """
        question = _latest_student_message(state.get("messages", []))
        if not question:
            return ""
        answer_format = getattr(cfg.domain, "example_answer_format", "1-3 word answer term only")
        domain_name = getattr(cfg.domain, "name", "the subject")
        prompt = (
            f"Student question: \"{question}\"\n\n"
            f"Reply with ONLY the primary {domain_name} concept that is the correct answer.\n"
            f"Format: {answer_format}\n"
            "Maximum 4 words. No parentheses, no qualifiers.\n"
            "If you cannot determine the answer with confidence, reply: unknown"
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=15,
                system=f"You are a precise {domain_name} expert. Reply with the answer term only — 1-4 words maximum.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (resp.content[0].text or "").strip().lower()
            # Strip anything after a parenthesis or comma
            raw = re.split(r"[,(]", raw)[0].strip()
            in_tok = resp.usage.input_tokens
            out_tok = resp.usage.output_tokens
            cost = (in_tok * _PRICE_IN + out_tok * _PRICE_OUT) / 1_000_000
            state["debug"]["api_calls"] += 1
            state["debug"]["input_tokens"] += in_tok
            state["debug"]["output_tokens"] += out_tok
            state["debug"]["cost_usd"] = float(state["debug"].get("cost_usd", 0.0)) + cost
            if raw in ("unknown", "unclear", "uncertain", ""):
                return ""
            if len(raw.split()) > 4:
                return ""
            return raw
        except Exception:
            return ""

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

        # Word-boundary reveal check. Raw substring match (previously used)
        # had two failure modes:
        #   - Single-word anchors ("latissimus") fire on any incidental mention
        #     in a legitimate Socratic probe.
        #   - Multi-word anchors mis-flag descriptive uses ("axillary nerve
        #     territory" flagged when locked = "axillary nerve").
        # Require the locked term to be at least 2 words AND appear as a
        # whole-phrase match (word-bounded).
        if locked and len(locked.split()) >= 2:
            if re.search(rf"\b{re.escape(locked)}\b", lowered):
                reason_codes.append("reveal_risk")

        if _sentence_count(text) > 4:
            reason_codes.append("verbosity")

        if any(prefix in lowered for prefix in _BANNED_FILLER_PREFIXES):
            reason_codes.append("generic_filler")

        # Prevent sycophantic over-affirmation when the student is not fully correct.
        if student_state in {"incorrect", "partial_correct", "question", "low_effort"} and _has_strong_affirmation(text):
            reason_codes.append("sycophancy_risk")

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
            "verbosity": "Cut to at most 4 short sentences.",
            "generic_filler": "Replace generic empathy with a specific reasoning step.",
            "sycophancy_risk": "Do not strongly affirm; acknowledge uncertainty and probe reasoning.",
            "question_repetition": "Use a new angle that is not a near-duplicate of recent questions.",
        }
        primary = reason_codes[0]
        critique = f"Deterministic checks failed: {', '.join(reason_codes)}."
        return {
            "pass": False,
            "critique": critique,
            "leak_detected": "reveal_risk" in reason_codes,
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

    def _teacher_preflight_brief(self, state: TutorState) -> str:
        """
        Non-LLM Dean guidance passed to Teacher before first draft each turn.
        This makes Teacher generation explicitly aware of Dean QC constraints.
        """
        student_state = state.get("student_state") or "unknown"
        hint_level = int(state.get("hint_level", 0))
        hint_plan = state.get("debug", {}).get("hint_plan", [])
        active_hint = ""
        if isinstance(hint_plan, list) and hint_plan and hint_level >= 1:
            idx = min(max(hint_level - 1, 0), len(hint_plan) - 1)
            active_hint = str(hint_plan[idx] or "")
        return (
            "reason_codes=['preflight']\n"
            "rewrite_instruction=Generate one concise Socratic reply that will pass Dean QC.\n"
            "critique=Preflight constraints: exactly one question mark, 2-4 sentences, "
            "no generic filler lead-ins, no answer reveal by naming/elimination, and "
            "one concrete next reasoning step.\n"
            f"context=student_state:{student_state},hint_level:{hint_level},active_hint:{active_hint}"
        )

    def _quality_check_call(self, state: TutorState, teacher_draft: str, phase: str = "tutoring") -> dict:
        """
        Dean's quality check call.
        Checks Teacher's draft against EULER 4 criteria + LeakGuard Level 3.

        Returns:
            dict: {"pass": bool, "critique": str, "leak_detected": bool}
        """
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
            dynamic_prompt = cfg.prompts.dean_quality_check_assessment_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
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
            dynamic_prompt = cfg.prompts.dean_quality_check_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                last_student_message=last_student_msg,
                teacher_draft=teacher_draft,
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
