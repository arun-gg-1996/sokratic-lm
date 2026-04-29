"""
scripts/run_multi_session_test.py
---------------------------------
Targeted-change regression for the cross-session memory cycle.

Design
------
3 students, each runs N sessions back-to-back with the SAME student_id:
  - Student A: 2 sessions (1st return)
  - Student B: 3 sessions (2 reads, growing memory)
  - Student C: 4 sessions (long-tail; tests mem0 recall under accumulation)

Total: 9 sessions vs 18 in the full scaled harness. ~$1.50 / run.

What this catches that 18×1 misses
-----------------------------------
  - Returning-student rapport behavior at small scale
  - Memory-write → memory-read cycle actually closing
  - Whether the LLM follows the "may reference ONE prior topic, no recap, no
    learning-style summary, no answer leak" rules from base.yaml
  - Memory growth pathology: does session 4 still produce a coherent rapport
    when ~20 mem0 facts are visible?

What this does NOT catch
------------------------
  - Statistical patterns across the 6-profile / 18-convo grid (use the full
    harness for that, before shipping a checkpoint)

Usage
-----
  cd /Users/arun-ghontale/UB/NLP/sokratic
  .venv/bin/python scripts/run_multi_session_test.py
  .venv/bin/python scripts/run_multi_session_test.py --no-clear      # accumulate on existing memory
  .venv/bin/python scripts/run_multi_session_test.py --label rapport_v2

Output
------
  data/artifacts/multi_session/<timestamp>_<label>/
    student_A/session_1.json  (rapport greeting + full convo + outcomes)
    student_A/session_2.json
    student_B/session_1..3.json
    student_C/session_1..4.json
    summary.txt   — per-student + per-session table with memory-use audit flags
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

# Patch the Anthropic SDK so we can capture per-call cache stats. Same
# pattern as run_scaled_convos.py.
import anthropic  # noqa: E402

_ALL_API_CALLS: list[dict] = []
import contextvars  # noqa: E402

_current_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "multi_session_current_id", default=""
)


def _patched_messages_create(self, *args, **kwargs):
    resp = self._original_messages_create(*args, **kwargs)
    usage = getattr(resp, "usage", None)
    _ALL_API_CALLS.append({
        "session_id": _current_session.get(),
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_input_tokens": int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
    })
    return resp


anthropic.resources.messages.messages.Messages._original_messages_create = (
    anthropic.resources.messages.messages.Messages.create
)
anthropic.resources.messages.messages.Messages.create = _patched_messages_create

from config import cfg  # noqa: E402
from conversation.graph import build_graph  # noqa: E402
from conversation.state import initial_state  # noqa: E402
from memory.memory_manager import MemoryManager  # noqa: E402
from retrieval.retriever import ChunkRetriever  # noqa: E402
from simulation.profiles import PROFILES  # noqa: E402
from simulation.student_simulator import StudentSimulator  # noqa: E402

MAX_TURNS = 25

# ----------------------------------------------------------------------
# Test plan
# ----------------------------------------------------------------------
# Each student runs N sessions back-to-back. Topics span related areas so
# memory has somewhere to grow without being trivially redundant.

STUDENTS: list[dict] = [
    {
        "label": "A",
        "profile": "S1",      # Strong, concise — short sessions, clean memory
        "topic_chapters": [19, 20],  # Cardiac conduction → systemic veins
    },
    {
        "label": "B",
        "profile": "S3",      # Weak — longer sessions, more memory per session
        "topic_chapters": [12, 13, 14],  # Graded potentials → cerebrum → sensory pathways
    },
    {
        "label": "C",
        "profile": "S4",      # Overconfident — 4 sessions test memory accumulation
        "topic_chapters": [6, 7, 8, 9],  # Skeletal → cranial bones → hip → synovial joints
    },
]


def load_topic_bank() -> dict[int, dict]:
    """Map chapter_num → topic_row (assumes one topic per chapter in v2 bank)."""
    bank_path = ROOT / "data/eval/topic_bank_v2.jsonl"
    by_ch: dict[int, dict] = {}
    for line in open(bank_path):
        t = json.loads(line)
        by_ch[t["chapter_num"]] = t
    return by_ch


# ----------------------------------------------------------------------
# Audit heuristics
# ----------------------------------------------------------------------

# Phrases that signal the LLM is treating a returning session as fresh.
# If session 2+ rapport contains these AND the prior session locked a
# topic, that's a memory-read failure.
_FRESH_MARKERS = [
    "starting fresh",
    "starting our session",
    "getting started",
    "first session",
    "new student",
    "no prior",
]

# Phrases that signal the LLM is recapping past content (which the prompt
# explicitly forbids — at most ONE reference, no list, no summary).
_LIST_MARKERS = [
    "you've covered",
    "we've covered",
    "previously covered",
    "topics covered",
    "you have studied",
]


def _audit_rapport(
    rapport_text: str,
    is_returning: bool,
    prior_topics: list[str],
    prior_locked_answers: list[str],
) -> dict:
    """Heuristic audit of a rapport message."""
    txt = (rapport_text or "").lower()
    flags: list[str] = []

    if is_returning:
        if any(m in txt for m in _FRESH_MARKERS):
            flags.append("fresh_marker_in_returning_session")

        # Did it reference any prior topic? (Soft signal — the prompt makes
        # this OPTIONAL. Tracked for visibility, not a hard fail.)
        referenced_any = False
        for prior in prior_topics:
            for token in prior.lower().split():
                if len(token) >= 5 and token in txt:
                    referenced_any = True
                    break
            if referenced_any:
                break
        if not referenced_any:
            flags.append("did_not_reference_prior_topic")  # informational

        # Multiple-reference / list violation
        if any(m in txt for m in _LIST_MARKERS):
            flags.append("possible_list_recap")

        # Answer leak: prior locked_answer text appearing verbatim in rapport
        for ans in prior_locked_answers:
            ans_norm = (ans or "").strip().lower()
            if len(ans_norm) >= 8 and ans_norm in txt:
                flags.append(f"possible_answer_leak:{ans_norm[:40]!r}")

    return {
        "flags": flags,
        "rapport_length": len(rapport_text or ""),
    }


# ----------------------------------------------------------------------
# Run a single session for a given student_id (no fresh student_id per call)
# ----------------------------------------------------------------------

async def run_one_session(
    student_id: str,
    profile_id: str,
    topic_row: dict,
    session_idx: int,
    graph,
) -> dict:
    """Run one full session reusing the given student_id (so mem0 stays attached)."""
    session_id = f"{student_id}__sess{session_idx}__{uuid.uuid4().hex[:6]}"
    state = initial_state(student_id, cfg)
    thread_config = {"configurable": {"thread_id": session_id}}
    simulator = StudentSimulator(PROFILES[profile_id])

    query = topic_row["queries"][profile_id]
    expected_section = topic_row.get("section_title", "")
    expected_subsection = topic_row.get("subsection_title", "")
    expected_chapter = topic_row.get("chapter_num")

    _tok = _current_session.set(session_id)
    api_before = len(_ALL_API_CALLS)
    t_start = time.time()
    bugs: list[str] = []

    # Phase 1: Rapport — capture the actual greeting text for audit.
    rapport_text = ""
    try:
        state["phase"] = "rapport"
        state = await graph.ainvoke(state, thread_config)
        # Last tutor message after rapport_node = the greeting
        for m in reversed(state.get("messages", [])):
            if m.get("role") == "tutor" and m.get("phase") == "rapport":
                rapport_text = m.get("content", "")
                break
    except Exception as e:
        bugs.append(f"rapport_error: {type(e).__name__}: {e}")

    # Phase 2: student gives topic
    #
    # Two harness-only adjustments to keep the topic-resolution honest:
    #
    #  (a) Clear pending_user_choice. rapport_node seeds initial_suggestions
    #      (topic cards) which become a pending opt_in/topic choice in the
    #      state. In a real UI the user would either click a card or type
    #      free text; in the harness we always type, so we wipe the choice
    #      first so the dean doesn't try to match our query against the
    #      cards and substitute a card label as the student's "real" intent.
    #
    #  (b) Prefix the assigned query with an explicit topic commitment for
    #      returning sessions. The rapport message (especially when memory
    #      is rich) often invites "continue X or pivot?". Without this
    #      preface, the simulated student's terse query can be interpreted
    #      by the dean as agreeing to continue the prior topic. The user-
    #      requested fix: the simulator must commit to its assigned topic
    #      regardless of what the rapport suggested. Production users
    #      drive this themselves; the harness has to fake the commitment.
    #
    # Both fixes are HARNESS-ONLY. Production code paths are unchanged.
    state["pending_user_choice"] = {}
    explicit_query = query
    if session_idx > 1:
        sub = topic_row.get("subsection_title") or ""
        if sub:
            explicit_query = (
                f"Today I want to focus on {sub}, not any topic from "
                f"earlier sessions. {query}"
            )
    try:
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "student", "content": explicit_query, "phase": "topic_input"}
        ]
        state["phase"] = "topic_engagement"
        state = await graph.ainvoke(state, thread_config)
    except Exception as e:
        bugs.append(f"topic_engagement_error: {type(e).__name__}: {e}")

    # Phase 3: tutoring loop
    turn_count = 0
    while turn_count < MAX_TURNS:
        turn_count += 1
        try:
            student_resp = simulator.respond(state)
            if not student_resp:
                break
            state["messages"] = list(state.get("messages", [])) + [
                {"role": "student", "content": student_resp}
            ]
            state = await graph.ainvoke(state, thread_config)
        except Exception as e:
            bugs.append(f"turn_{turn_count}_error: {type(e).__name__}: {e}")
            break
        if state.get("student_reached_answer") and state.get("phase") == "memory_update":
            break
        if state.get("phase") in ("memory_update", "session_end"):
            break

    elapsed = time.time() - t_start
    api_in_session = _ALL_API_CALLS[api_before:]

    locked_answer = state.get("locked_answer", "") or ""
    locked_topic = state.get("locked_topic") or {}
    retrieved = state.get("retrieved_chunks", []) or []

    sec_hit = bool(expected_section) and any(
        (c.get("section_title") or "") == expected_section for c in retrieved
    )
    ch_hit = expected_chapter is not None and any(
        c.get("chapter_num") == expected_chapter for c in retrieved
    )
    if not sec_hit and locked_topic:
        sec_hit = locked_topic.get("section") == expected_section

    record = {
        "session_id": session_id,
        "student_id": student_id,
        "profile_id": profile_id,
        "session_idx": session_idx,
        "topic": {
            "chapter": expected_chapter,
            "section": expected_section,
            "subsection": expected_subsection,
            "query": query,
        },
        "rapport_text": rapport_text,
        "outcome": {
            "topic_confirmed": bool(state.get("topic_confirmed")),
            "locked_answer": locked_answer,
            "reached_answer": bool(state.get("student_reached_answer")),
            "phase_final": state.get("phase"),
            "turn_count": int(state.get("turn_count", turn_count)),
            "on_topic_section": sec_hit,
            "on_topic_chapter": ch_hit,
        },
        "metrics": {
            "wall_seconds": round(elapsed, 1),
            "n_api_calls": len(api_in_session),
            "input_tokens": sum(c["input_tokens"] for c in api_in_session),
            "output_tokens": sum(c["output_tokens"] for c in api_in_session),
            "cache_read": sum(c["cache_read_input_tokens"] for c in api_in_session),
            "cache_write": sum(c["cache_creation_input_tokens"] for c in api_in_session),
        },
        "messages": state.get("messages", []),
        "bugs_noted": bugs,
    }
    _current_session.reset(_tok)
    return record


# ----------------------------------------------------------------------
# Main: run each student's sessions sequentially (within a student;
# students run sequentially too since clear-memory + ordering matters)
# ----------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> None:
    print("Building retriever (chunks)...", flush=True)
    retriever = ChunkRetriever()
    memory_manager = MemoryManager()
    print(f"mem0 available: {memory_manager.persistent.available}", flush=True)

    if args.clear:
        n = memory_manager.clear_namespace()
        print(f"Cleared mem0 namespace (returned {n}).", flush=True)

    graph = build_graph(retriever, memory_manager)
    print("Graph built.", flush=True)

    bank = load_topic_bank()
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = ROOT / f"data/artifacts/multi_session/{stamp}_{args.label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_dir}\n", flush=True)

    all_records: list[dict] = []

    for student in STUDENTS:
        label = student["label"]
        profile = student["profile"]
        chapters = student["topic_chapters"]
        student_id = f"multi_test_{label}_{stamp}"
        student_dir = out_dir / f"student_{label}"
        student_dir.mkdir(exist_ok=True)
        print(
            f"=== Student {label} | profile={profile} | "
            f"{len(chapters)} sessions | id={student_id} ===",
            flush=True,
        )

        prior_topics: list[str] = []
        prior_locked_answers: list[str] = []

        for i, ch_num in enumerate(chapters, start=1):
            topic = bank.get(ch_num)
            if not topic:
                print(f"  [skip] No topic for ch{ch_num}", flush=True)
                continue
            if profile not in (topic.get("queries") or {}):
                print(f"  [skip] Topic ch{ch_num} has no {profile} query", flush=True)
                continue

            print(
                f"\n--- Student {label} session {i}/{len(chapters)} — "
                f"ch{ch_num} {topic.get('subsection_title','')} ---",
                flush=True,
            )

            record = await run_one_session(
                student_id=student_id,
                profile_id=profile,
                topic_row=topic,
                session_idx=i,
                graph=graph,
            )

            # Audit the rapport message against prior session content
            audit = _audit_rapport(
                rapport_text=record["rapport_text"],
                is_returning=(i > 1),
                prior_topics=prior_topics,
                prior_locked_answers=prior_locked_answers,
            )
            record["rapport_audit"] = audit

            # Snapshot mem0 state at end of session
            try:
                mems = memory_manager.load(
                    student_id,
                    query="topics covered, misconceptions, and outcomes",
                )
                record["mem0_count_after_session"] = len(mems)
                record["mem0_snippets"] = [
                    (m.get("memory") or m.get("data") or m.get("text") or "")[:120]
                    for m in (mems or [])[:10]
                ]
            except Exception:
                record["mem0_count_after_session"] = -1
                record["mem0_snippets"] = []

            with open(student_dir / f"session_{i}.json", "w") as f:
                json.dump(record, f, indent=2, default=str)

            outc = record["outcome"]
            print(
                f"  ✓ s{i} turns={outc['turn_count']} wall={record['metrics']['wall_seconds']}s "
                f"sec={outc['on_topic_section']} ch={outc['on_topic_chapter']} "
                f"reached={outc['reached_answer']} mem0_after={record['mem0_count_after_session']} "
                f"audit_flags={audit['flags'] or '[]'}",
                flush=True,
            )

            # Track for next session's audit
            prior_topics.append(topic.get("subsection_title") or topic.get("section_title") or "")
            if record["outcome"]["locked_answer"]:
                prior_locked_answers.append(record["outcome"]["locked_answer"])

            all_records.append(record)

            # Pause for mem0 indexing to settle before next session
            time.sleep(3)

    # Aggregate report
    write_summary(all_records, out_dir)
    print(f"\nDone. Report: {out_dir}/summary.txt", flush=True)


def write_summary(records: list[dict], out_dir: Path) -> None:
    lines: list[str] = []
    lines.append("=" * 90)
    lines.append("MULTI-SESSION MEMORY TEST SUMMARY")
    lines.append("=" * 90)
    lines.append("")

    by_student: dict[str, list[dict]] = {}
    for r in records:
        by_student.setdefault(r["student_id"], []).append(r)

    total_cost = 0.0
    for student_id in sorted(by_student.keys()):
        sessions = sorted(by_student[student_id], key=lambda x: x["session_idx"])
        lines.append(f"Student: {student_id}")
        lines.append(
            f"  profile={sessions[0]['profile_id']}  N_sessions={len(sessions)}"
        )
        lines.append(
            f"  {'sess':>4}  {'ch':>3}  {'turns':>5}  {'wall':>5}  "
            f"{'sec':>3}  {'reach':>5}  {'mem0_after':>10}  flags"
        )
        for r in sessions:
            o = r["outcome"]
            m = r["metrics"]
            flags = ",".join(r["rapport_audit"]["flags"]) or "-"
            lines.append(
                f"  {r['session_idx']:>4}  {r['topic']['chapter']:>3}  "
                f"{o['turn_count']:>5}  {int(m['wall_seconds']):>4}s  "
                f"{'Y' if o['on_topic_section'] else 'n':>3}  "
                f"{'Y' if o['reached_answer'] else 'n':>5}  "
                f"{r['mem0_count_after_session']:>10}  {flags}"
            )
            # Cost estimate (Sonnet 4.5 pricing)
            in_tok = m["input_tokens"]
            out_tok = m["output_tokens"]
            cr = m["cache_read"]
            cw = m["cache_write"]
            cost = (in_tok * 3 + cr * 0.3 + cw * 3.75 + out_tok * 15) / 1_000_000
            total_cost += cost
        lines.append("")
        # Show the rapport messages so the auditor can eyeball returning behavior
        lines.append(f"  Rapport messages:")
        for r in sessions:
            lines.append(f"    s{r['session_idx']}: {r['rapport_text']}")
        lines.append("")

    lines.append(f"Total cost (estimated): ${total_cost:.2f}")
    lines.append("")
    lines.append("Audit flag glossary:")
    lines.append("  fresh_marker_in_returning_session  — LLM may have ignored mem0 read")
    lines.append("  did_not_reference_prior_topic      — informational; reference is OPTIONAL")
    lines.append("  possible_list_recap                — RULE VIOLATION (no list/recap)")
    lines.append("  possible_answer_leak               — RULE VIOLATION (no past-answer reveal)")
    lines.append("")

    text = "\n".join(lines)
    print("\n" + text)
    (out_dir / "summary.txt").write_text(text)
    (out_dir / "summary.json").write_text(
        json.dumps({"records": records}, indent=2, default=str)
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="multi_session", help="Run label")
    ap.add_argument(
        "--no-clear",
        dest="clear",
        action="store_false",
        help="Don't wipe mem0 namespace before run (default: clear)",
    )
    ap.set_defaults(clear=True)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
