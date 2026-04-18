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
import anthropic
from conversation.state import TutorState
from config import cfg


def _cached_system(static: str, chunks: str = "", dynamic: str = "") -> list:
    # Merge static + chunks into ONE cached block so the combined size exceeds
    # Anthropic's per-block minimum (1024 tok Sonnet / 2048 tok Haiku).
    # Two separate small blocks never write cache entries.
    combined = static + ("\n\n" + chunks if chunks else "")
    blocks = [{"type": "text", "text": combined, "cache_control": {"type": "ephemeral"}}]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def _cache_suffix_message(messages: list) -> list:
    """
    Add cache_control to the final message payload block so Anthropic can cache
    the prefix and only bill the changing suffix on subsequent calls.
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

        system = cfg.prompts.teacher_rapport.format(weak_topics=topics_str, time_of_day=tod)
        return self._call(
            user_msg="Start the session.",
            state=state,
            system=system,
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
        system = cfg.prompts.teacher_topic_engagement.format(retrieved_chunks=chunks_str)
        topic = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "student":
                topic = msg.get("content", "")
                break
        raw = self._call(
            user_msg=topic or "The student has named an anatomy topic.",
            state=state,
            system=system,
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

        # Normalize and enforce 3-4 distinct options.
        deduped: list[str] = []
        seen = set()
        for opt in options:
            key = _normalize_text(opt)
            if key and key not in seen:
                seen.add(key)
                deduped.append(opt)
        options = deduped[:4]

        if not question:
            topic_label = topic.strip() if topic else "this anatomy area"
            question = (
                f"{topic_label} is a great focus. Choose one specific angle to start with."
            )

        if len(options) < 3:
            topic_label = topic.strip() if topic else "this topic"
            options = [
                f"{topic_label}: primary function and movement role",
                f"{topic_label}: innervation and key pathways",
                f"{topic_label}: clinical exam findings and OT relevance",
                f"{topic_label}: common injury patterns and functional impact",
            ][:4]

        # Keep tutor text clean: UI renders option cards separately.
        message = question

        return {
            "question": question,
            "options": options,
            "message": message.strip(),
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
        max_history = int(getattr(cfg.session, "prompt_history_messages", 12))
        if max_history > 0:
            history = history[-max_history:]
        conversation_history = _format_messages(history)

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
            conversation_history=conversation_history,
        )

        return self._call(
            static=cfg.prompts.teacher_socratic_static,
            chunks=chunks_block,
            dynamic=dynamic,
            user_msg=last_student_msg or "Continue.",
            state=state,
            wrapper_name="teacher.draft_socratic",
        )

    def draft_clinical_opt_in(self, state: TutorState) -> str:
        """
        Ask whether the student wants to do an optional clinical application question.
        """
        return self._call(
            static=cfg.prompts.teacher_clinical_opt_in_static,
            dynamic=cfg.prompts.teacher_clinical_opt_in_dynamic.format(
                locked_answer=state.get("locked_answer", "")
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
            static=cfg.prompts.teacher_clinical_static,
            chunks=cfg.prompts.teacher_clinical_chunks.format(retrieved_chunks=chunks_str),
            dynamic=cfg.prompts.teacher_clinical_dynamic.format(
                locked_answer=state.get("locked_answer", ""),
                dean_critique=dean_critique or state.get("dean_critique", ""),
            ),
            user_msg="Ask a clinical application question.",
            state=state,
            wrapper_name="teacher.draft_clinical",
        )

    def _call(self, user_msg: str, state: TutorState | None,
              wrapper_name: str = "", system: str = "", static: str = "",
              chunks: str = "", dynamic: str = "") -> str:
        """
        Shared Anthropic API call for all Teacher wrappers.
        Updates state["debug"] if state is provided.

        Args:
            user_msg:     The user-role message (last student message or fixed prompt)
            state:        TutorState (for debug tracking). None during rapport (state not yet set).
            wrapper_name: Name of calling wrapper (for turn_trace logging)
        """
        if static:
            system_blocks = _cached_system(static, chunks, dynamic)
            system_text = static + ("\n\n" + chunks if chunks else "") + ("\n\n---\n\n" + dynamic if dynamic else "")
        else:
            system_blocks = _cached_system(system)
            system_text = system

        messages = _cache_suffix_message([{"role": "user", "content": user_msg}])

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
                "elapsed_s": round(elapsed, 2),
                "in_tok": in_tok,
                "out_tok": out_tok,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "cost_usd": round(cost, 5),
                "s_per_tok": round(tpt, 4),
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


def _format_messages(messages: list[dict]) -> str:
    if not messages:
        return "(no conversation yet)"
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


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
