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
import contextvars
import json
import re
import hashlib
from typing import Callable, Optional
import anthropic
from conversation.state import TutorState
from conversation.rendering import render_history
from config import cfg


# D.6a: streaming hook. When set (typically by the WS handler before
# graph.invoke), draft_socratic streams tokens via Anthropic's
# streaming API and invokes this callback for each text delta. The
# callback is responsible for forwarding deltas to whatever sink the
# caller wants (WebSocket, event log, stdout). When None (the default
# for non-WS callers like the eval harness), draft_socratic uses the
# non-streaming API exactly as before — full backward compat.
#
# A contextvar (not a thread/module global) so concurrent sessions
# under asyncio.gather don't bleed callbacks across each other. The
# WS handler sets it on its own task; other tasks see None.
_stream_callback: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("teacher_stream_callback", default=None)
)


def set_stream_callback(cb: Optional[Callable[[str], None]]) -> contextvars.Token:
    """Install a per-task streaming callback for draft_socratic. Returns
    a token; pass it to reset_stream_callback() to remove the callback.

    The callback is invoked from inside draft_socratic with each text
    delta as the LLM streams. After the stream completes draft_socratic
    returns the full aggregated text exactly as the non-streaming path
    would; the callback is purely additive — no behavior changes for
    callers that don't read tokens via the callback.
    """
    return _stream_callback.set(cb)


def reset_stream_callback(token: contextvars.Token) -> None:
    _stream_callback.reset(token)


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
    Multi-block cache layout — see conversation/dean.py:_cached_system for
    the full rationale. Mirror of that function so teacher and dean share
    identical caching semantics.

    Layout:
      Block 1 [CACHED-if-≥4000-tokens]: role_base + wrapper_delta + chunks  (stable)
      Block 2 [CACHED-if-≥4000-tokens]: history                              (append-only)
      Block 3 UNCACHED:                turn_deltas                          (per-turn)

    Pre-fix behavior (until 2026-04-29): role+wrapper+chunks+history were
    joined into one cached block, and history grew turn-over-turn → cache
    prefix changed every turn → 0% cache hit rate. Fix splits history out
    of the stable block so Block 1's prefix bytes stay constant across the
    session.
    """
    blocks: list[dict] = []
    # See dean.py:_cached_system for rationale on the 1500 threshold.
    cache_min_tokens = 1500

    stable = "\n\n".join(part for part in [role_base, wrapper_delta, chunks] if part)

    if stable:
        b1: dict = {"type": "text", "text": stable}
        if _estimate_tokens(stable) >= cache_min_tokens:
            b1["cache_control"] = {"type": "ephemeral"}
        blocks.append(b1)

    if history:
        b2: dict = {"type": "text", "text": history}
        if _estimate_tokens(history) >= cache_min_tokens:
            b2["cache_control"] = {"type": "ephemeral"}
        blocks.append(b2)

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

    def draft_rapport(
        self,
        weak_topics: list[dict],
        state: TutorState | None = None,
        past_session_memories: list[dict] | None = None,
        client_hour: int | None = None,
    ) -> str:
        """
        Generate a personalized greeting for session start. Uses cfg.prompts.teacher_rapport.
        Called once by rapport_node. No student message yet — this is the opening.

        Args:
            weak_topics: legacy field kept for backward compat with existing
                         callers. Empty list = new student. Currently unused
                         in the rapport prompt; superseded by
                         `past_session_memories` (mem0-driven).
            past_session_memories: list of mem0 result dicts (each with a
                         'memory' or 'data' field carrying the natural-
                         language fact). When non-empty, the rapport
                         prompt may reference one prior topic. Empty / None
                         → fresh-session behavior.
            client_hour: 0-23 hour from the user's local clock, sent by the
                         frontend on session start (D.6b-5). Used to pick
                         the morning/afternoon/evening greeting. Falls back
                         to server-side datetime.now().hour if not provided
                         (preserves backward compat for callers that don't
                         pass it). Server-time fallback is wrong for
                         deployments where the server isn't in the user's
                         tz, hence the explicit client_hour parameter.

        Returns:
            str: Greeting message. If past_session_memories non-empty:
                 may reference one specific past topic. If empty: warm
                 welcome + invite student to type a topic.
        """
        # Format past session memories for the prompt. mem0 atomizes our
        # structured strings into individual facts, so the most natural
        # representation is a bulleted list of those facts. Cap at 8
        # entries so the prompt stays compact.
        past_str = "No previous history — new student."
        if past_session_memories:
            bullets: list[str] = []
            for m in past_session_memories[:8]:
                if isinstance(m, dict):
                    text = (
                        m.get("memory")
                        or m.get("data")
                        or m.get("text")
                        or ""
                    )
                else:
                    text = str(m)
                text = str(text).strip()
                if text:
                    bullets.append(f"  - {text}")
            if bullets:
                past_str = "\n".join(bullets)

        # Use client_hour when present (D.6b-5). Server fallback only fires
        # when the call site forgot to pass it — the frontend's startSession
        # always supplies it now.
        hour = (
            int(client_hour)
            if client_hour is not None and 0 <= int(client_hour) <= 23
            else datetime.now().hour
        )
        if hour < 12:
            tod = "morning"
        elif hour < 17:
            tod = "afternoon"
        else:
            tod = "evening"

        # Resolve delta template — DOES NOT receive time_of_day. The TOD
        # has been moved to the uncached turn_deltas block (D.6b-5) so the
        # cached prefix stays stable across hour rollovers; otherwise every
        # 60-minute boundary would invalidate the rapport cache. The
        # wrapper_delta now references "{time_of_day}" as a literal label
        # (escaped {{ }} in the YAML) and the LLM resolves the actual word
        # from the runtime context block we inject as turn_deltas.
        delta_template = getattr(
            cfg.prompts,
            "teacher_rapport_delta",
            cfg.prompts.teacher_rapport,
        )
        wrapper_delta_filled = delta_template.format(
            past_session_memories=past_str,
            **_domain_prompt_vars(),
        )
        runtime_context = (
            "Runtime context for this session:\n"
            f"  time_of_day: {tod}\n"
            "Use the corresponding 'Good morning' / 'Good afternoon' / "
            "'Good evening' as the first words of your reply, exactly."
        )
        return self._call(
            user_msg="Start the session.",
            state=state,
            role_base=getattr(cfg.prompts, "teacher_base", ""),
            wrapper_delta=wrapper_delta_filled,
            turn_deltas=runtime_context,
            wrapper_name="teacher.draft_rapport",
        )

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

        # B'.1 — pull Dean's pre-planned hint for the current hint_level so the
        # teacher follows the global hint progression instead of re-deriving
        # one from scratch each turn. hint_plan is a list[str] populated once
        # by Dean's _hint_plan_call at topic-lock time and stored in
        # state["debug"]["hint_plan"]. Empty fallback is fine — the teacher
        # has the chunks and can still produce a question without the plan.
        hint_plan_active = ""
        debug = state.get("debug")
        if isinstance(debug, dict):
            hint_plan = debug.get("hint_plan") or []
            if isinstance(hint_plan, list) and hint_plan:
                hint_level_int = max(int(state.get("hint_level", 1) or 1), 1)
                idx = min(hint_level_int - 1, len(hint_plan) - 1)
                hint_plan_active = str(hint_plan[idx])

        chunks_block = cfg.prompts.teacher_socratic_chunks.format(retrieved_chunks=chunks_str) if chunks else ""
        dynamic = cfg.prompts.teacher_socratic_dynamic.format(
            student_state=state.get("student_state") or "unknown",
            dean_critique=dean_critique,
            hint_level=state.get("hint_level", 1),
            max_hints=state.get("max_hints", 3),
            locked_question=state.get("locked_question", ""),
            hint_plan_active=hint_plan_active,
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
        # D.6a: stream when a callback is installed AND this is the
        # tutoring-loop draft (the perceived-latency hot path). Other
        # wrappers (rapport, clinical) skip streaming because they're
        # called once at session boundaries — non-streaming + the cache
        # is the right shape there.
        cb = _stream_callback.get()
        should_stream = (
            cb is not None and wrapper_name == "teacher.draft_socratic"
        )

        if should_stream:
            chunks_collected: list[str] = []
            in_tok = 0
            out_tok = 0
            cache_read = 0
            cache_write = 0
            with self.client.messages.stream(
                model=self.model,
                max_tokens=220,
                system=system_blocks,
                messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            ) as stream:
                for text in stream.text_stream:
                    if not text:
                        continue
                    chunks_collected.append(text)
                    try:
                        cb(text)
                    except Exception:
                        # Callback failure must not break the LLM call.
                        pass
                final_message = stream.get_final_message()
            response_text = "".join(chunks_collected)
            usage = getattr(final_message, "usage", None)
            if usage is not None:
                in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            # Bind to the same names the non-streaming branch produces
            # below so the debug-update code path stays unified.
            class _UsageShim:
                pass
            _shim = _UsageShim()
            _shim.input_tokens = in_tok
            _shim.output_tokens = out_tok
            _shim.cache_read_input_tokens = cache_read
            _shim.cache_creation_input_tokens = cache_write
            class _RespShim:
                pass
            resp = _RespShim()
            resp.usage = _shim
        else:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=220,
                system=system_blocks,
                messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            response_text = resp.content[0].text if resp.content else ""
        elapsed = time.time() - t0

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
