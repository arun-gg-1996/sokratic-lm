"""
conversation/teacher.py
------------------------
Teacher agent — generates Socratic responses for the student.

Teacher has NO tools and NEVER sees locked_answer (except in draft_clinical during assessment).
Teacher's only job: given retrieved_chunks + hint_level + student_state, ask one
focused question that guides the student toward the concepts in the chunks.

Teacher's prompt wrappers:
  draft_rapport()   — teacher_rapport prompt, called once by rapport_node
  draft_socratic()  — teacher_socratic prompt, called every tutoring turn by dean_node
  draft_clinical()  — teacher_clinical prompt, called by assessment_node when student reached answer

Hint level guidance (Teacher follows, doesn't invent):
  1 = broad question — activate prior knowledge
  2 = narrower question — point toward key concept
  3 = direct push — one final specific question, still no answer given

student_state drives tone (via behavior table in teacher_socratic prompt):
  correct         → affirm briefly, ask next guiding question
  partial_correct → affirm correct part explicitly, probe the gap
  incorrect       → do NOT say they're wrong, ask "how did you arrive at that?"
  question        → answer the clarifying question briefly, redirect to problem
  irrelevant      → redirect back to topic without giving content away
  low_effort      → ask firmly "what part of this are you stuck on?" — do NOT advance

All Anthropic calls update state["debug"]: api_calls, token counts, turn_trace entries.
Uses Anthropic SDK (anthropic.Anthropic()), NOT OpenAI.
"""

import time
from datetime import datetime
import json
import re
import hashlib
import anthropic
from conversation.state import TutorState
from conversation.rendering import render_history
from config import cfg


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
    Build system blocks with stable cacheable prefix.

    Block layout:
      1) role_base + wrapper_delta (cached, stable)
      2) retrieved propositions/chunks (cached after topic-lock)
      3) rendered history (cached append-only prefix)
      4) turn deltas (uncached, volatile)
    """
    blocks: list[dict] = []
    # Haiku 4.5 minimum is ~2048 actual tokens. Our estimate is len/4 and often
    # overshoots vs the tokenizer on structured/repetitive text, so require a comfortable margin.
    cache_min_tokens = 4000
    cached_primary = "\n\n".join(part for part in [role_base, wrapper_delta, chunks, history] if part)

    # Anthropic prompt caching requires a sufficiently large cacheable prefix.
    # If the stable block is still too small, temporarily promote turn_deltas
    # into the cached block so cache writes/reads can happen instead of zeroing out.
    # Prefix matching still reuses the unchanged leading bytes across turns.
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


def _trace_input_hash(system_text: str, messages: list[dict]) -> str:
    payload = system_text + "\n\n" + json.dumps(messages, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class TeacherAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.model = cfg.models.teacher

    def draft_rapport(self, weak_topics: list[dict], state: TutorState | None = None) -> str:
        """
        Generate a personalized greeting for session start. Uses cfg.prompts.teacher_rapport.
        Called once by rapport_node. No student message yet — this is the opening.

        Args:
            weak_topics: List of {topic, difficulty, failure_count} from mem0.
                         Empty list = new student with no history.

        Returns:
            str: Greeting message. If weak_topics non-empty: suggest most-failed topic
                 and ask if student wants to revisit it or pick something new.
                 If empty: warm welcome + invite student to type a topic they want to explore.
        """
        if weak_topics:
            topics_str = "\n".join(
                f"  - {wt['topic']} (failed {wt['failure_count']} times, difficulty: {wt.get('difficulty', 'unknown')})"
                for wt in weak_topics
            )
        else:
            topics_str = "No previous history — new student."

        hour = datetime.now().hour
        if hour < 12:
            tod = "morning"
        elif hour < 17:
            tod = "afternoon"
        else:
            tod = "evening"

        system = cfg.prompts.teacher_rapport.format(
            weak_topics=topics_str, time_of_day=tod, **_domain_prompt_vars()
        )
        return self._call(
            user_msg="Start the session.",
            state=state,
            role_base=getattr(cfg.prompts, "teacher_base", ""),
            wrapper_delta=getattr(cfg.prompts, "teacher_rapport_delta", system),
            wrapper_name="teacher.draft_rapport",
        )

    def draft_topic_engagement(self, state: TutorState) -> dict:
        """
        Generate a scoping question when the student names a broad topic (turn 0).
        Offers 3-4 concrete sub-topic options and asks which to focus on.
        Called by dean.run_turn before normal Socratic tutoring begins.
        """
        chunks = state.get("retrieved_chunks", [])
        chunks_str = _format_chunks(chunks)
        system = cfg.prompts.teacher_topic_engagement.format(
            retrieved_chunks=chunks_str, **_domain_prompt_vars()
        )
        topic = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "student":
                topic = msg.get("content", "")
                break
        raw = self._call(
            user_msg=topic or f"The student has named a {getattr(cfg.domain, 'name', 'domain')} topic.",
            state=state,
            role_base=getattr(cfg.prompts, "teacher_base", ""),
            wrapper_delta=getattr(cfg.prompts, "teacher_topic_engagement_delta", system),
            wrapper_name="teacher.draft_topic_engagement",
        )
        parsed = _extract_json_object(raw)
        question = ""
        options: list[str] = []
        if parsed is not None:
            question = str(parsed.get("question", "") or "").strip()
            raw_options = parsed.get("options", [])
            if isinstance(raw_options, list):
                for opt in raw_options:
                    s = str(opt or "").strip()
                    if s:
                        options.append(s)

        # Normalize and deduplicate options.
        deduped: list[str] = []
        seen: set[str] = set()
        for opt in options:
            key = _normalize_text(opt)
            if key and key not in seen:
                seen.add(key)
                deduped.append(opt)
        options = deduped[:4]

        # If LLM returned fewer than 3 valid options, retry once with an explicit count reminder.
        if len(options) < 3:
            retry_system = cfg.prompts.teacher_topic_engagement.format(
                retrieved_chunks=chunks_str, **_domain_prompt_vars()
            ) + "\n\nIMPORTANT: You must return EXACTLY 3 to 4 distinct options in the 'options' array."
            retry_raw = self._call(
                user_msg=topic or "The student has named a topic.",
                state=state,
                role_base=getattr(cfg.prompts, "teacher_base", ""),
                wrapper_delta=getattr(cfg.prompts, "teacher_topic_engagement_delta", retry_system),
                wrapper_name="teacher.draft_topic_engagement_retry",
            )
            retry_parsed = _extract_json_object(retry_raw)
            if retry_parsed is not None:
                retry_options_raw = retry_parsed.get("options", [])
                retry_options: list[str] = []
                if isinstance(retry_options_raw, list):
                    for opt in retry_options_raw:
                        s = str(opt or "").strip()
                        if s:
                            retry_options.append(s)
                retry_deduped: list[str] = []
                retry_seen: set[str] = set()
                for opt in retry_options:
                    key = _normalize_text(opt)
                    if key and key not in retry_seen:
                        retry_seen.add(key)
                        retry_deduped.append(opt)
                if len(retry_deduped) >= 3:
                    options = retry_deduped[:4]
                    if not question:
                        question = str(retry_parsed.get("question", "") or "").strip()
                    raw = retry_raw

        if not question:
            topic_label = topic.strip() if topic else "this topic"
            question = f"{topic_label} covers several areas. Which angle would you like to start with?"

        # Keep tutor text clean: UI renders option cards separately.
        return {
            "question": question,
            "options": options,
            "message": question,
            "raw": raw,
        }

    def draft_socratic(self, state: TutorState) -> str:
        """
        Generate one Socratic question for the current tutoring turn.
        Uses cfg.prompts.teacher_socratic.

        Receives (via state):
          - retrieved_chunks: textbook passages (answer is somewhere in them)
          - hint_level: 1/2/3 — controls specificity of question
          - student_state: drives tone/behavior (see docstring above)
          - messages: conversation history
          - dean_critique: Dean guidance string (preflight on first attempt, feedback on retry)

        Does NOT receive: locked_answer.

        Returns:
            str: Draft response ending with exactly one question.
                 Not yet approved by Dean — may be rejected and retried.
        """
        chunks = state.get("retrieved_chunks", [])
        chunks_str = _format_chunks(chunks)

        history = state.get("messages", [])
        conversation_history = render_history(history)

        last_student_msg = ""
        for msg in reversed(history):
            if msg.get("role") == "student":
                last_student_msg = msg.get("content", "")
                break

        dean_critique = state.get("dean_critique", "")

        chunks_block = cfg.prompts.teacher_socratic_chunks.format(retrieved_chunks=chunks_str) if chunks else ""
        dynamic = cfg.prompts.teacher_socratic_dynamic.format(
            student_state=state.get("student_state") or "unknown",
            dean_critique=dean_critique,
            hint_level=state.get("hint_level", 1),
            max_hints=state.get("max_hints", 3),
            locked_question=state.get("locked_question", ""),
            conversation_history=conversation_history,
            **_domain_prompt_vars(),
        )

        return self._call(
            role_base=getattr(cfg.prompts, "teacher_base", ""),
            wrapper_delta=getattr(cfg.prompts, "teacher_socratic_delta", cfg.prompts.teacher_socratic_static),
            chunks=chunks_block,
            history=conversation_history,
            turn_deltas=dynamic,
            user_msg=last_student_msg or "Continue.",
            state=state,
            wrapper_name="teacher.draft_socratic",
        )

    def draft_clinical_opt_in(self, state: TutorState) -> str:
        """
        Ask whether the student wants to do an optional clinical application question.
        """
        return self._call(
            role_base=getattr(cfg.prompts, "teacher_base", ""),
            wrapper_delta=getattr(
                cfg.prompts, "teacher_clinical_opt_in_delta", cfg.prompts.teacher_clinical_opt_in_static
            ),
            turn_deltas=cfg.prompts.teacher_clinical_opt_in_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                **_domain_prompt_vars(),
            ),
            user_msg="Ask if the student wants the optional clinical question.",
            state=state,
            wrapper_name="teacher.draft_clinical_opt_in",
        )

    def draft_clinical(self, state: TutorState, dean_critique: str = "") -> str:
        """
        Generate a clinical application question for the assessment phase.
        Only called when student_reached_answer = True.
        """
        chunks_str = _format_chunks(state.get("retrieved_chunks", []))
        return self._call(
            role_base=getattr(cfg.prompts, "teacher_base", ""),
            wrapper_delta=getattr(cfg.prompts, "teacher_clinical_delta", cfg.prompts.teacher_clinical_static),
            chunks=cfg.prompts.teacher_clinical_chunks.format(retrieved_chunks=chunks_str),
            turn_deltas=cfg.prompts.teacher_clinical_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                dean_critique=dean_critique or state.get("dean_critique", ""),
                **_domain_prompt_vars(),
            ),
            user_msg="Ask a clinical application question.",
            state=state,
            wrapper_name="teacher.draft_clinical",
        )

    def _call(
        self,
        user_msg: str,
        state: TutorState | None,
        wrapper_name: str = "",
        role_base: str = "",
        wrapper_delta: str = "",
        chunks: str = "",
        history: str = "",
        turn_deltas: str = "",
    ) -> str:
        """
        Shared Anthropic API call for all Teacher wrappers.
        Updates state["debug"] if state is provided.

        Args:
            user_msg:     The user-role message (last student message or fixed prompt)
            state:        TutorState (for debug tracking). None during rapport (state not yet set).
            wrapper_name: Name of calling wrapper (for turn_trace logging)
        """
        system_blocks = _cached_system(
            _apply_domain_vars(role_base),
            _apply_domain_vars(wrapper_delta),
            _apply_domain_vars(chunks),
            history,
            _apply_domain_vars(turn_deltas),
        )
        system_text = "\n\n---\n\n".join(b.get("text", "") for b in system_blocks)
        system_block_debug = []
        cached_est_tokens = 0
        for idx, block in enumerate(system_blocks, start=1):
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
        messages = [{"role": "user", "content": user_msg}]
        input_hash = _trace_input_hash(system_text, messages)

        t0 = time.time()
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=220,
            system=system_blocks,
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        elapsed = time.time() - t0
        response_text = resp.content[0].text if resp.content else ""

        if state is not None:
            in_tok = resp.usage.input_tokens
            out_tok = resp.usage.output_tokens
            cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            cost = (
                in_tok * 3
                + cache_read * 0.3
                + cache_write * 3.75
                + out_tok * 15
            ) / 1_000_000
            tpt = elapsed / out_tok if out_tok > 0 else 0.0
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
                "system_prompt": system_text,
                "messages_sent": messages,
                "response_text": response_text,
                "tool_calls_made": [],
            })

        return response_text

# --- Formatting helpers ---

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


def _extract_json_object(text: str) -> dict | None:
    txt = (text or "").strip()
    if not txt:
        return None
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # Try fenced JSON or first balanced object.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", txt, flags=re.DOTALL | re.IGNORECASE)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    start = txt.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(txt)):
        ch = txt[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cand = txt[start : i + 1]
                try:
                    obj = json.loads(cand)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def _normalize_text(s: str) -> str:
    return " ".join((s or "").strip().lower().split())
