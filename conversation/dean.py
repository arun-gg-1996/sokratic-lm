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
from difflib import SequenceMatcher
from pathlib import Path
import anthropic
from conversation.state import TutorState
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
_UNCERTAINTY_STRONG_MARKERS = (
    "not sure",
    "unsure",
    "not fully sure",
    "not fully confident",
    "not confident",
    "uncertain",
    "maybe",
    "might",
    "could be",
    "i guess",
    "i'm guessing",
    "mixing up",
    "fuzzy",
)
_UNCERTAINTY_WEAK_MARKERS = (
    "i think",
)
_CONFIDENCE_MARKERS = (
    "the answer is",
    "it is",
    "it's",
    "definitely",
    "i am sure",
    "i'm sure",
    "confident",
    "certain",
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
_COMMON_NERVE_GUESSES = (
    "axillary nerve",
    "musculocutaneous nerve",
    "median nerve",
    "ulnar nerve",
    "radial nerve",
    "suprascapular nerve",
    "long thoracic nerve",
    "thoracodorsal nerve",
    "subscapular nerve",
    "accessory nerve",
    "supraclavicular nerve",
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


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _heuristic_confidence(message: str, student_state: str, locked_answer: str) -> float:
    """
    Deterministic confidence estimate used as fallback and light calibration.
    """
    base_by_state = {
        "correct": 0.82,
        "partial_correct": 0.56,
        "question": 0.42,
        "incorrect": 0.24,
        "irrelevant": 0.10,
        "low_effort": 0.05,
    }
    score = base_by_state.get(student_state, 0.35)
    txt = _normalize_text(message)
    locked = _normalize_text(locked_answer)
    strong_uncertain = any(m in txt for m in _UNCERTAINTY_STRONG_MARKERS)
    weak_uncertain = any(m in txt for m in _UNCERTAINTY_WEAK_MARKERS)
    confident = any(m in txt for m in _CONFIDENCE_MARKERS)

    if strong_uncertain:
        score -= 0.20
    elif weak_uncertain:
        score -= 0.08

    if confident:
        score += 0.06
    if locked and locked in txt and student_state == "correct":
        score += 0.04

    return round(_clamp01(score), 3)


def _cached_system(static: str, chunks: str = "", dynamic: str = "") -> list:
    """
    Two-block system prompt for optimal caching:
    - Block 1 (static instructions + chunks, merged): cache_control
      Merging is critical — each block must INDIVIDUALLY meet Anthropic's minimum
      token threshold (1024 for Sonnet, 2048 for Haiku). A ~300-token static block
      alone never writes a cache entry. Merging static + ~750-token chunks block
      pushes the combined block well past the threshold.
    - Block 2 (dynamic state + conversation): no cache_control — changes every turn
    """
    combined = static + ("\n\n" + chunks if chunks else "")
    blocks = [{"type": "text", "text": combined, "cache_control": {"type": "ephemeral"}}]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def _cache_suffix_message(messages: list) -> list:
    """
    Add cache_control to the final message payload block so Anthropic can cache
    the prompt prefix and bill only the new suffix on subsequent turns.
    """
    if not messages:
        return messages

    result = [dict(m) if isinstance(m, dict) else m for m in messages]
    idx = len(result) - 1
    if not isinstance(result[idx], dict):
        return result

    msg = dict(result[idx])
    content = msg.get("content", "")
    if isinstance(content, str):
        msg["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list) and content:
        new_content = []
        for i, block in enumerate(content):
            if isinstance(block, dict):
                b = dict(block)
                if (
                    i == len(content) - 1
                    and b.get("type") == "text"
                    and "cache_control" not in b
                ):
                    b["cache_control"] = {"type": "ephemeral"}
                new_content.append(b)
            else:
                new_content.append(block)
        msg["content"] = new_content

    result[idx] = msg
    return result


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
        "elapsed_s": round(elapsed, 2),
        "in_tok": in_tok,
        "out_tok": out_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": round(cost, 5),
        "s_per_tok": round(tpt, 4),
        # Full content for debug UI
        "system_prompt": system_text,
        "messages_sent": messages_sent,
        "response_text": response_text,
        "tool_calls_made": tool_calls,
    })
    return resp


def _normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
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


def _is_repetitive_question(new_question: str, prior_questions: list[str], threshold: float = 0.9) -> bool:
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


def _is_uncertain_correct_answer(message: str, locked_answer: str) -> bool:
    txt = _normalize_text(message)
    locked = _normalize_text(locked_answer)
    if not txt or not locked or locked not in txt:
        return False

    strong = any(m in txt for m in _UNCERTAINTY_STRONG_MARKERS)
    weak = any(m in txt for m in _UNCERTAINTY_WEAK_MARKERS)
    confident = any(m in txt for m in _CONFIDENCE_MARKERS)
    return strong or (weak and not confident)


def _contains_explicit_wrong_nerve_guess(message: str, locked_answer: str) -> bool:
    """
    Detect answer attempts that name a non-locked nerve.
    Used to prevent wrong guesses from being mislabeled as question/partial_correct.
    """
    txt = _normalize_text(message)
    locked = _normalize_text(locked_answer)
    if not txt or not locked:
        return False
    if locked in txt:
        return False
    if "nerve" not in txt:
        return False

    # Pure clarification questions should remain "question".
    clarification_markers = (
        "which nerve",
        "what nerve",
        "can you explain",
        "what do you mean",
        "could you clarify",
    )
    if any(m in txt for m in clarification_markers):
        return False

    guesses = set()
    for name in _COMMON_NERVE_GUESSES:
        if name in txt:
            guesses.add(name)
    for match in re.findall(r"\b([a-z]+(?:\s+[a-z]+){0,2}\s+nerve)\b", txt):
        guesses.add(match.strip())

    guesses = {g for g in guesses if locked not in g}
    return bool(guesses)


def _latest_student_message(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "student":
            return str(msg.get("content", "")).strip()
    return ""


def _extract_locked_answer_from_chunks(chunks: list[dict]) -> str:
    """
    Lightweight deterministic lock extraction from retrieved chunks.
    """
    patterns = [
        r"\binnervated by the ([a-zA-Z0-9\-\s]+?)(?:\s*\(|[.,;])",
        r"\bthe answer is ([a-zA-Z0-9\-\s]+?)(?:[.,;]|$)",
    ]
    for chunk in chunks[:3]:
        text = str(chunk.get("text", "") or "")
        for pat in patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                answer = m.group(1).strip().lower()
                answer = re.sub(r"\s+", " ", answer)
                if answer:
                    return answer
    return ""


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
    }
    return txt in low_effort_set


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
    return len(tokens) < 3


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
    latest_norm = _normalize_text(latest)
    if topic and latest and latest_norm not in _RETRIEVAL_LOW_SIGNAL and len(latest.split()) >= 3:
        # Keep topic anchor but preserve fresh student intent/detail.
        candidate = f"{topic}. {latest}"

    cleaned = _clean_retrieval_query(candidate)
    if _is_ambiguous_retrieval_query(cleaned):
        fallback = _clean_retrieval_query(topic)
        if fallback and not _is_ambiguous_retrieval_query(fallback):
            return fallback
        return ""
    return cleaned


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
        if not state.get("topic_confirmed", False):
            messages = list(state.get("messages", []))
            topic_options = list(state.get("topic_options", []))

            if not topic_options:
                # Retrieve context once to generate strong scoping options.
                query = _clean_retrieval_query(_latest_student_message(messages))
                if query:
                    chunks = search_textbook(query, self.retriever)
                    state["retrieved_chunks"] = chunks
                    state["debug"]["turn_trace"].append({
                        "wrapper": "dean.python_retrieval",
                        "result": f"{len(chunks)} chunks returned | query={query}",
                    })
                scoped = teacher.draft_topic_engagement(state)
                scoped_q = str(scoped.get("question", "") or "").strip()
                topic_options = list(scoped.get("options", []) or [])
                scoped_msg = str(scoped.get("message", "") or "").strip()
                if not scoped_msg:
                    scoped_msg = scoped_q
                messages.append({"role": "tutor", "content": scoped_msg})
                return {
                    "messages": messages,
                    "topic_confirmed": False,
                    "topic_options": topic_options,
                    "topic_question": scoped_q,
                    "topic_selection": "",
                    "retrieved_chunks": [],  # force fresh retrieval once topic is selected
                    "locked_answer": "",
                    "student_state": None,
                    "debug": state["debug"],
                }

            # Options already shown: require explicit selection.
            latest_student = _latest_student_message(messages)
            selected = _match_topic_selection(latest_student, topic_options)
            if not selected:
                # Allow free-text custom topic selection (non-low-effort) in place of card choice.
                if latest_student and not _is_low_effort_topic_reply(latest_student):
                    selected = latest_student.strip()

            if not selected:
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
            state["retrieved_chunks"] = []
            state["locked_answer"] = ""

        self._ensure_retrieval_and_lock(state)

        eval_result = self._setup_call(state)
        try:
            parsed_hint_level = int(eval_result.get("hint_level", state.get("hint_level", 1)))
        except (TypeError, ValueError):
            parsed_hint_level = int(state.get("hint_level", 1))
        eval_result = {
            "student_state": eval_result.get("student_state", "irrelevant"),
            "student_reached_answer": bool(eval_result.get("student_reached_answer", False)),
            "confidence_score": eval_result.get("confidence_score", 0.0),
            "hint_level": parsed_hint_level,
            "locked_answer": eval_result.get("locked_answer", state.get("locked_answer", "")),
            "search_needed": bool(eval_result.get("search_needed", False)),
            "critique": eval_result.get("critique", ""),
        }

        latest_student_msg = _latest_student_message(state.get("messages", []))
        locked_for_turn = str(eval_result.get("locked_answer") or state.get("locked_answer", ""))
        if (
            eval_result.get("student_state") in {"question", "partial_correct"}
            and _contains_explicit_wrong_nerve_guess(latest_student_msg, locked_for_turn)
        ):
            eval_result["student_state"] = "incorrect"
            eval_result["student_reached_answer"] = False
            state["debug"]["turn_trace"].append({
                "wrapper": "dean.classification_override",
                "result": "forced_incorrect_for_explicit_wrong_guess",
            })

        confidence_score = self._compute_student_confidence(state, eval_result)
        eval_result["confidence_score"] = confidence_score
        reached_threshold = float(getattr(getattr(cfg, "thresholds", object()), "reached_answer_confidence", 0.72))
        eval_result["student_reached_answer"] = (
            eval_result["student_state"] == "correct" and confidence_score >= reached_threshold
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
        })

        # Hint progression:
        # - increment only for "incorrect"
        # - allow max_hints + 1 to signal "hints exhausted" routing in after_dean
        if eval_result["student_state"] == "incorrect":
            current_hint = int(state.get("hint_level", 1))
            if current_hint >= state["max_hints"]:
                next_hint = state["max_hints"] + 1
            else:
                next_hint = current_hint + 1
            state["hint_level"] = min(next_hint, state["max_hints"] + 1)

        if state["student_reached_answer"]:
            return {
                "student_state": state["student_state"],
                "student_reached_answer": True,
                "student_answer_confidence": state["student_answer_confidence"],
                "student_mastery_confidence": state["student_mastery_confidence"],
                "confidence_samples": state["confidence_samples"],
                "locked_answer": state["locked_answer"],
                "retrieved_chunks": state["retrieved_chunks"],
                "topic_confirmed": state.get("topic_confirmed", False),
                "topic_options": state.get("topic_options", []),
                "topic_question": state.get("topic_question", ""),
                "topic_selection": state.get("topic_selection", ""),
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
            "locked_answer": state["locked_answer"],
            "retrieved_chunks": state["retrieved_chunks"],
            "topic_confirmed": state.get("topic_confirmed", False),
            "topic_options": state.get("topic_options", []),
            "topic_question": state.get("topic_question", ""),
            "topic_selection": state.get("topic_selection", ""),
            "help_abuse_count": state["help_abuse_count"],
            "dean_retry_count": 0,
            "dean_critique": "",
            "debug": state["debug"],
        }

    def _compute_student_confidence(self, state: TutorState, eval_result: dict) -> float:
        """
        Compute per-turn answer confidence without changing the categorical label.
        Uses model score when provided, with deterministic calibration fallback.
        """
        student_state = str(eval_result.get("student_state", "")).strip().lower()
        latest_student = _latest_student_message(state.get("messages", []))
        locked_answer = str(eval_result.get("locked_answer") or state.get("locked_answer", "")).strip()
        heuristic = _heuristic_confidence(latest_student, student_state, locked_answer)

        raw = eval_result.get("confidence_score", None)
        try:
            model_score = None if raw is None else _clamp01(float(raw))
        except (TypeError, ValueError):
            model_score = None

        if model_score is None:
            return heuristic

        blended = (0.7 * model_score) + (0.3 * heuristic)
        if _is_uncertain_correct_answer(latest_student, locked_answer) and student_state == "correct":
            blended = min(blended, 0.69)
        return round(_clamp01(blended), 3)

    def _ensure_retrieval_and_lock(self, state: TutorState) -> None:
        """
        Python-side fast path:
        - Retrieve chunks once when missing
        - Lock answer deterministically when possible
        This removes Dean tool-loop round trips on first turn.
        """
        chunks = state.get("retrieved_chunks", [])
        if not chunks:
            query = _build_retrieval_query(state)
            if query:
                chunks = search_textbook(query, self.retriever)
                state["retrieved_chunks"] = chunks
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.python_retrieval",
                    "result": f"{len(chunks)} chunks returned | query={query}",
                })
            else:
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.retrieval_query_guard",
                    "result": "skipped_retrieval_due_to_ambiguous_query",
                })

        if not state.get("locked_answer"):
            locked = _extract_locked_answer_from_chunks(state.get("retrieved_chunks", []))
            if locked:
                state["locked_answer"] = locked
                state["debug"]["turn_trace"].append({
                    "wrapper": "dean.python_lock_answer",
                    "result": f"locked_answer={locked}",
                })

    def _setup_call(self, state: TutorState) -> dict:
        """
        Dean setup classification call (single-call, no tool loop).
        Retrieval and answer locking are done in Python before this call.

        Returns:
            dict with student_state, student_reached_answer, hint_level,
                  locked_answer, search_needed, critique
        """
        history = state.get("messages", [])
        max_history = int(getattr(cfg.dean, "setup_history_messages", 8))
        if max_history > 0:
            history = history[-max_history:]
        conversation_history = _format_messages(history)
        static_prompt = (
            cfg.prompts.dean_setup_classify_static
            if hasattr(cfg.prompts, "dean_setup_classify_static")
            else cfg.prompts.dean_setup_static
        )
        dynamic_prompt = (
            cfg.prompts.dean_setup_classify_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                hint_level=state.get("hint_level", 1),
                turn_count=state.get("turn_count", 0),
                conversation_history=conversation_history,
            )
            if hasattr(cfg.prompts, "dean_setup_classify_dynamic")
            else cfg.prompts.dean_setup_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                hint_level=state.get("hint_level", 1),
                turn_count=state.get("turn_count", 0),
                chunks_available=bool(state.get("retrieved_chunks", [])),
                conversation_history=conversation_history,
            )
        )

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        resp = _timed_create(
            self.client, state, "dean._setup_call",
            model=self.model,
            temperature=0,
            max_tokens=220,
            system=_cached_system(static_prompt, chunks=chunks_str, dynamic=dynamic_prompt),
            messages=_cache_suffix_message([{"role": "user", "content": "Classify this turn and return JSON."}]),
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
            hint_level = int(parsed.get("hint_level", state.get("hint_level", 1)))
        except (TypeError, ValueError):
            hint_level = int(state.get("hint_level", 1))

        locked = str(parsed.get("locked_answer", state.get("locked_answer", "")) or "").strip().lower()
        if not locked:
            locked = state.get("locked_answer", "")

        result = {
            "student_state": student_state,
            "student_reached_answer": bool(parsed.get("student_reached_answer", False)),
            "confidence_score": parsed.get("confidence_score", None),
            "hint_level": hint_level,
            "locked_answer": locked,
            "search_needed": False,
            "critique": str(parsed.get("critique", "") or ""),
        }
        state["debug"]["turn_trace"].append({
            "wrapper": "dean._setup_call",
            "result": f"student_state={result['student_state']}, hint={result['hint_level']}",
        })
        return result

    def _setup_local_fallback(self, state: TutorState) -> dict:
        """
        Local classification fallback when Dean JSON parsing fails.
        """
        msg = _latest_student_message(state.get("messages", []))
        txt = (msg or "").strip().lower()
        locked = (state.get("locked_answer", "") or "").lower()

        low_effort_set = {"", "idk", "i don't know", "i dont know", "don't know", "dont know", "help"}
        if txt in low_effort_set:
            student_state = "low_effort"
        elif locked and locked in txt:
            student_state = "correct"
        elif "?" in txt:
            student_state = "question"
        elif any(k in txt for k in ("brachial plexus", "c5", "c6", "deltoid", "shoulder")):
            student_state = "partial_correct"
        else:
            student_state = "incorrect"

        result = {
            "student_state": student_state,
            "student_reached_answer": student_state == "correct",
            "confidence_score": _heuristic_confidence(msg, student_state, locked),
            "hint_level": int(state.get("hint_level", 1)),
            "locked_answer": state.get("locked_answer", ""),
            "search_needed": False,
            "critique": "Dean setup parse fallback used.",
        }
        state["debug"]["turn_trace"].append({
            "wrapper": "dean._setup_local_fallback",
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

        if locked and locked in lowered:
            reason_codes.append("reveal_risk")

        if _sentence_count(text) > 4:
            reason_codes.append("verbosity")

        if any(prefix in lowered for prefix in _BANNED_FILLER_PREFIXES):
            reason_codes.append("generic_filler")

        # Prevent sycophantic over-affirmation when the student is not fully correct.
        if student_state in {"incorrect", "partial_correct", "question", "low_effort"} and _has_strong_affirmation(text):
            reason_codes.append("sycophancy_risk")

        prior_questions = _recent_tutor_questions(state.get("messages", []), limit=3)
        if _is_repetitive_question(_extract_question_text(text), prior_questions):
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
        hint_level = int(state.get("hint_level", 1))
        return (
            "reason_codes=['preflight']\n"
            "rewrite_instruction=Generate one concise Socratic reply that will pass Dean QC.\n"
            "critique=Preflight constraints: exactly one question mark, 2-4 sentences, "
            "no generic filler lead-ins, no answer reveal by naming/elimination, and "
            "one concrete next reasoning step.\n"
            f"context=student_state:{student_state},hint_level:{hint_level}"
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
        history = state.get("messages", [])
        max_history = int(getattr(cfg.dean, "qc_history_messages", 6))
        if max_history > 0:
            history = history[-max_history:]
        conversation_history = _format_messages(history)

        if phase == "assessment" and hasattr(cfg.prompts, "dean_quality_check_assessment_static"):
            static_prompt = cfg.prompts.dean_quality_check_assessment_static
            dynamic_prompt = cfg.prompts.dean_quality_check_assessment_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                last_student_message=last_student_msg,
                teacher_draft=teacher_draft,
                conversation_history=conversation_history,
            )
        else:
            static_prompt = cfg.prompts.dean_quality_check_static
            dynamic_prompt = cfg.prompts.dean_quality_check_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                last_student_message=last_student_msg,
                teacher_draft=teacher_draft,
                conversation_history=conversation_history,
            )

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        resp = _timed_create(
            self.client, state, "dean._quality_check_call",
            model=self.model,
            temperature=0,
            max_tokens=420,
            system=_cached_system(static_prompt, chunks=chunks_str, dynamic=dynamic_prompt),
            messages=_cache_suffix_message([{"role": "user", "content": "Evaluate this Teacher draft."}]),
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
        history = state.get("messages", [])
        max_history = int(getattr(cfg.session, "prompt_history_messages", 12))
        if max_history > 0:
            history = history[-max_history:]
        conversation_history = _format_messages(history)
        student_msg = _latest_student_message(state.get("messages", []))
        last_tutor_msg = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "tutor":
                last_tutor_msg = str(msg.get("content", "") or "").strip()
                break

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        static_prompt = getattr(
            cfg.prompts,
            "dean_clinical_turn_static",
            (
                "You are the Dean evaluating a student's OT clinical reasoning response. "
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
        )

        resp = _timed_create(
            self.client,
            state,
            "dean._clinical_turn_call",
            model=self.model,
            temperature=0,
            max_tokens=360,
            system=_cached_system(static_prompt, chunks=chunks_str, dynamic=dynamic_prompt),
            messages=_cache_suffix_message([{"role": "user", "content": "Evaluate this clinical response and return JSON."}]),
        )

        text = (resp.content[0].text or "").strip()
        parsed = _extract_json_object(text)
        if parsed is None:
            return self._clinical_turn_local_fallback(state, student_msg)

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

        return {
            "student_state": student_state,
            "confidence_score": round(confidence, 3),
            "pass": passed,
            "feedback_message": feedback,
        }

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

    def _assessment_call(self, state: TutorState) -> str:
        """
        Dean's assessment call.

        If student_reached_answer and assessment_turn == 2 (has clinical response):
          → mastery summary
        If not student_reached_answer:
          → reveal message with textbook quote

        Returns:
            str: message to append to state["messages"]
        """
        reached = state.get("student_reached_answer", False)
        outcome = "reached_answer" if reached else "did_not_reach_answer"

        # Get clinical response (latest student answer during clinical path)
        clinical_response = ""
        if reached and state.get("clinical_opt_in") is True:
            for msg in reversed(state.get("messages", [])):
                if msg.get("role") == "student":
                    clinical_response = msg.get("content", "")
                    break

        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        history = state.get("messages", [])
        max_history = int(getattr(cfg.session, "prompt_history_messages", 12))
        if max_history > 0:
            history = history[-max_history:]
        conversation_history = _format_messages(history)

        clinical_opt_in = state.get("clinical_opt_in")
        opt_in_label = (
            "completed_clinical_question" if clinical_opt_in is True
            else "skipped_clinical_question" if clinical_opt_in is False
            else "unknown"
        )

        static_prompt = (
            cfg.prompts.dean_assessment_static
            if hasattr(cfg.prompts, "dean_assessment_static")
            else cfg.prompts.dean_assessment
        )
        dynamic_prompt = (
            cfg.prompts.dean_assessment_dynamic.format(
                outcome=outcome,
                locked_answer=state.get("locked_answer", ""),
                clinical_opt_in=opt_in_label,
                clinical_response=clinical_response or "(none)",
                core_mastery_tier=state.get("core_mastery_tier", "not_assessed"),
                clinical_mastery_tier=state.get("clinical_mastery_tier", "not_assessed"),
                mastery_tier=state.get("mastery_tier", "not_assessed"),
                clinical_turn_count=state.get("clinical_turn_count", 0),
                clinical_max_turns=state.get("clinical_max_turns", 3),
                clinical_completed=state.get("clinical_completed", False),
                clinical_history=json.dumps(state.get("clinical_history", [])),
                conversation_history=conversation_history,
            )
            if hasattr(cfg.prompts, "dean_assessment_dynamic")
            else cfg.prompts.dean_assessment.format(
                outcome=outcome,
                locked_answer=state.get("locked_answer", ""),
                retrieved_chunks=chunks_str,
                clinical_response=clinical_response,
                conversation_history=conversation_history,
            )
        )

        chunks_block = (
            cfg.prompts.dean_assessment_chunks.format(retrieved_chunks=chunks_str)
            if hasattr(cfg.prompts, "dean_assessment_chunks")
            else (cfg.prompts.dean_setup_chunks.format(retrieved_chunks=chunks_str) if chunks_str else "")
        )

        resp = _timed_create(
            self.client, state, "dean._assessment_call",
            model=self.model,
            max_tokens=512,
            system=_cached_system(
                static_prompt,
                chunks_block,
                dynamic_prompt,
            ),
            messages=_cache_suffix_message([{"role": "user", "content": "Write the assessment message."}]),
        )
        return resp.content[0].text

    def _assessment_clinical_fallback(self, state: TutorState) -> str:
        """
        Dean-written fallback clinical question if Teacher fails quality twice.
        """
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        system = (
            "You are an OT anatomy tutor writing a single clinical application question. "
            "Do not restate the answer. Ask exactly one clinically grounded question about "
            "functional impact, exam finding, or intervention planning. 2-3 sentences max."
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
            system=_cached_system(system),
            messages=_cache_suffix_message([{"role": "user", "content": user_msg}]),
        )
        return resp.content[0].text

    def _assessment_clinical_followup_fallback(self, state: TutorState) -> str:
        """
        Dean-written fallback coaching follow-up for clinical multi-turn loops.
        Must include what was right, what to correct, and one follow-up question.
        """
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        student_msg = _latest_student_message(state.get("messages", []))
        system = (
            "You are an OT anatomy tutor giving corrective clinical coaching. "
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
            system=_cached_system(system),
            messages=_cache_suffix_message([{"role": "user", "content": user_msg}]),
        )
        return resp.content[0].text

    def _memory_summary_call(self, state: TutorState) -> str:
        """
        Dean's memory summary call.
        Generates a natural language session summary for mem0.

        Returns:
            str: summary text passed to memory_manager.flush()
        """
        weak_topics = state.get("weak_topics", [])
        weak_str = "; ".join(
            f"{wt['topic']} (failed {wt.get('failure_count', 1)} times)"
            for wt in weak_topics
        ) or "none"

        if state.get("student_reached_answer"):
            tier = str(state.get("mastery_tier", "not_assessed"))
            ans = state.get("locked_answer", "")
            if tier in {"developing", "needs_review"}:
                mastered = f"partial mastery: {ans} ({tier})"
            else:
                mastered = ans
        else:
            mastered = "none"

        # Topics covered = topic from first student message
        topics_covered = ""
        for msg in state.get("messages", []):
            if msg.get("role") == "student":
                topics_covered = msg.get("content", "")[:80]
                break

        system = cfg.prompts.dean_memory_summary.format(
            topics_covered=topics_covered or "unknown",
            mastered_topics=mastered,
            weak_topics=weak_str,
            hint_levels=f"final hint level: {state.get('hint_level', 1)}",
            mastery_tier=state.get("mastery_tier", "not_assessed"),
            core_mastery_tier=state.get("core_mastery_tier", "not_assessed"),
            clinical_mastery_tier=state.get("clinical_mastery_tier", "not_assessed"),
            turn_count=state.get("turn_count", 0),
        )

        resp = _timed_create(
            self.client, state, "dean._memory_summary_call",
            model=self.model,
            max_tokens=256,
            system=_cached_system(system),
            messages=_cache_suffix_message([{"role": "user", "content": "Write the session memory summary."}]),
        )
        return resp.content[0].text

    def _dean_fallback(self, state: TutorState) -> str:
        """Dean writes directly when Teacher fails twice. Generic safe Socratic nudge."""
        history = _format_messages(state.get("messages", []))
        system = (
            "You are a Socratic anatomy tutor. The student is working through a problem. "
            "Do NOT give the answer or any specific anatomical facts. "
            "Ask one open-ended question that gets them thinking about what they already know. "
            "2 sentences max. Must end with a question."
        )
        resp = _timed_create(
            self.client, state, "dean.fallback",
            model=self.model,
            max_tokens=128,
            system=_cached_system(system),
            messages=_cache_suffix_message(
                [{"role": "user", "content": f"Conversation so far:\n{history}\n\nWrite a safe fallback question."}]
            ),
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
        section = chunk.get("section_title", "")
        subsection = chunk.get("subsection_title", "")
        location = " > ".join(filter(None, [section, subsection]))
        parts.append(f"[{i}] {location}\n{chunk.get('text', '')}")
    return "\n\n".join(parts)


def _format_messages(messages: list[dict]) -> str:
    if not messages:
        return "(no conversation yet)"
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        parts.append(f"{role}: {content}")
    return "\n".join(parts)
