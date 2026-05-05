"""
scripts/check_prelock_flow.py
─────────────────────────────
End-to-end simulation of the My Mastery → Start → Pick-Anchor → Tutor flow.

Mirrors what happens in production when a student clicks Start on a
subsection from My Mastery:

  1. Frontend calls POST /api/session/start with prelocked_topic=<path>.
  2. Backend _apply_prelock fires:
       - sets state.locked_topic
       - retrieves chunks
       - generates 3 anchor question variations via Sonnet
       - stashes pending_user_choice = {kind: "anchor_pick", options, anchor_meta}
  3. Backend graph.invoke runs rapport_node which emits a deterministic
     prelock-aware greeting.
  4. Frontend renders cards. Student picks one (or types a pivot).
  5. Backend graph.invoke (with student message = picked question text):
       - dean_node_v2 entry gate detects pending=anchor_pick
       - run_topic_lock_v2 anchor_pick handler resolves the pick:
           * sets state.locked_question / locked_answer / aliases / full_answer
           * clears pending_user_choice
           * sets topic_just_locked=True
       - dean_node_v2 falls through to tutoring on the SAME invocation:
           * Dean.plan emits a TurnPlan
           * Teacher.draft produces the first Socratic question
       - Final return merges both the anchor-pick state updates AND
         the tutoring response.
  6. Frontend shows the Socratic question.
  7. Subsequent turns engage normally.

This script asserts the invariants for each step and prints a
detailed per-turn dump.

Usage:
  SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks \\
    .venv/bin/python -u scripts/check_prelock_flow.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env", override=True)


# Path to a touched subsection — canonical "Chapter X > Section > Subsection"
# format that the mastery_tree returns. The session.py prelock parser now
# accepts both this and legacy "Ch1|Section|Subsection".
CANONICAL_PATH = (
    "An Introduction to the Human Body > Anatomical Terminology > "
    "Body Cavities and Serous Membranes"
)
STUDENT_ID = "nidhi"  # any student in the students table


# ── Per-step assertion helpers ─────────────────────────────────────────────


class StepReport:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, step: str, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"step": step, "name": name, "passed": passed, "detail": detail})

    def pretty(self) -> None:
        for c in self.checks:
            mark = "✓" if c["passed"] else "✗"
            d = f"  {c['detail']}" if c["detail"] else ""
            print(f"  [{c['step']:18s}] {mark} {c['name']}{d}")
        n_pass = sum(1 for c in self.checks if c["passed"])
        n_fail = len(self.checks) - n_pass
        print()
        print(f"SUMMARY: {n_pass}/{len(self.checks)} passed, {n_fail} failed")
        return n_fail


def _truncate(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


# ── End-to-end driver ──────────────────────────────────────────────────────


async def run() -> int:
    from config import cfg
    from conversation.graph import build_graph
    from conversation.state import initial_state
    from retrieval.retriever import ChunkRetriever
    from memory.memory_manager import MemoryManager
    from backend.api.session import _apply_prelock

    print("=" * 78)
    print("PRELOCK → ANCHOR PICK → TUTORING FLOW SIMULATION")
    print("=" * 78)
    print(f"student_id:    {STUDENT_ID}")
    print(f"prelocked_topic: {CANONICAL_PATH}")
    print()

    # Ensure FK requirement is satisfied (mirrors backend ensure_student).
    try:
        from memory.sqlite_store import SQLiteStore
        SQLiteStore().ensure_student(STUDENT_ID)
    except Exception:
        pass

    retriever = ChunkRetriever()
    mem = MemoryManager()
    graph = build_graph(retriever, mem)

    thread_id = f"prelocktest_{uuid.uuid4().hex[:8]}"
    state = initial_state(STUDENT_ID, cfg)
    state["thread_id"] = thread_id

    cfg_obj = {"configurable": {"thread_id": thread_id}}
    report = StepReport()

    # ── Step 1: simulate _apply_prelock (POST /session/start prelocked path) ──
    print("─" * 78)
    print("STEP 1 — _apply_prelock (= clicking Start in My Mastery)")
    print("─" * 78)
    t0 = time.time()
    try:
        _apply_prelock(state, CANONICAL_PATH)
        prelock_ok = True
        prelock_err = ""
    except Exception as e:
        prelock_ok = False
        prelock_err = f"{type(e).__name__}: {e}"
    dt_prelock = time.time() - t0
    print(f"elapsed: {dt_prelock:.2f}s")

    report.add("step1_prelock", "no exception", prelock_ok, prelock_err)
    locked = state.get("locked_topic") or {}
    report.add(
        "step1_prelock", "locked_topic.path set",
        bool(locked.get("path")),
        f"path={locked.get('path')!r}",
    )
    report.add(
        "step1_prelock", "locked_topic.subsection set",
        bool(locked.get("subsection")),
        f"subsection={locked.get('subsection')!r}",
    )
    pending = state.get("pending_user_choice") or {}
    report.add(
        "step1_prelock", "pending_user_choice.kind == 'anchor_pick'",
        pending.get("kind") == "anchor_pick",
        f"kind={pending.get('kind')!r}",
    )
    options = list(pending.get("options") or [])
    report.add(
        "step1_prelock", "3 anchor variations generated",
        len(options) == 3, f"n_options={len(options)}",
    )
    anchor_meta = pending.get("anchor_meta") or {}
    report.add(
        "step1_prelock", "anchor_meta has full Q/A for each option",
        all(
            isinstance(anchor_meta.get(q), dict)
            and (anchor_meta[q].get("question") and anchor_meta[q].get("answer"))
            for q in options
        ),
        f"meta_keys={list(anchor_meta.keys())[:3]}",
    )
    print(f"\nGenerated anchor questions:")
    for i, q in enumerate(options, start=1):
        meta = anchor_meta.get(q, {})
        print(f"  {i}. Q: {_truncate(q, 100)}")
        print(f"     A: {_truncate(meta.get('answer', ''), 60)}")
    print()

    # ── Step 2: graph.invoke for the rapport turn (no student message yet) ──
    print("─" * 78)
    print("STEP 2 — graph.invoke at session start (rapport phase)")
    print("─" * 78)
    t0 = time.time()
    state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
    dt_rapport = time.time() - t0
    print(f"elapsed: {dt_rapport:.2f}s")
    msgs = state.get("messages") or []
    report.add(
        "step2_rapport", "rapport message added",
        any(m.get("role") == "tutor" for m in msgs),
        f"n_messages={len(msgs)}",
    )
    rapport_msg = next(
        (m for m in msgs if m.get("role") == "tutor"), None
    )
    rapport_content = (rapport_msg or {}).get("content") or ""
    report.add(
        "step2_rapport", "greeting mentions subsection",
        "Body Cavities" in rapport_content,
        f"preview: {_truncate(rapport_content, 80)}",
    )
    report.add(
        "step2_rapport", "phase advanced past rapport",
        state.get("phase") != "rapport",
        f"phase={state.get('phase')}",
    )
    # pending should still be anchor_pick (rapport doesn't consume it)
    report.add(
        "step2_rapport", "pending_user_choice still anchor_pick",
        (state.get("pending_user_choice") or {}).get("kind") == "anchor_pick",
        f"kind={(state.get('pending_user_choice') or {}).get('kind')!r}",
    )
    print()

    # ── Step 3: simulate student clicking the FIRST anchor card ──
    print("─" * 78)
    print("STEP 3 — student picks anchor card #1 (resolves pick + tutoring)")
    print("─" * 78)
    chosen = options[0] if options else ""
    if not chosen:
        report.add("step3_pick", "anchor available to pick", False, "no options")
        report.pretty()
        return 1
    state["messages"].append({"role": "student", "content": chosen})
    n_msgs_before = len(state["messages"])
    t0 = time.time()
    state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
    dt_pick = time.time() - t0
    print(f"elapsed: {dt_pick:.2f}s")
    print(f"picked: {_truncate(chosen, 100)}")

    msgs = state.get("messages") or []
    new_tutor_msgs = [
        m for m in msgs[n_msgs_before:]
        if m.get("role") == "tutor"
    ]
    n_new_tutor = len(new_tutor_msgs)

    # ── State invariants after the pick ──
    report.add(
        "step3_pick", "locked_question populated (handler ran)",
        bool(state.get("locked_question")),
        f"q={_truncate(state.get('locked_question') or '', 80)}",
    )
    report.add(
        "step3_pick", "locked_answer populated",
        bool(state.get("locked_answer")),
        f"a={state.get('locked_answer')!r}",
    )
    report.add(
        "step3_pick", "pending_user_choice cleared",
        not (state.get("pending_user_choice") or {}).get("kind"),
        f"pending={state.get('pending_user_choice')!r}",
    )
    report.add(
        "step3_pick", "topic_just_locked stamped",
        bool(state.get("topic_just_locked")),
        f"flag={state.get('topic_just_locked')!r}",
    )

    # ── Tutoring fired on the SAME invocation ──
    report.add(
        "step3_pick", "tutor message produced same invocation (Dean+Teacher fired)",
        n_new_tutor >= 1,
        f"new_tutor_msgs={n_new_tutor}",
    )
    if n_new_tutor >= 1:
        last_tutor = new_tutor_msgs[-1]
        meta = last_tutor.get("metadata") or {}
        content = last_tutor.get("content") or ""
        is_error_card = meta.get("kind") == "error_card"
        report.add(
            "step3_pick", "tutor message is real Socratic question (not error card)",
            not is_error_card and len(content.strip()) > 30,
            f"len={len(content)} mode={meta.get('mode')!r} kind={meta.get('kind')!r}",
        )
        report.add(
            "step3_pick", "tutor message != rapport replay",
            content.strip() != rapport_content.strip(),
            "different=" + str(content.strip() != rapport_content.strip()),
        )
        # Verify it doesn't leak the locked answer
        locked_a_lower = (state.get("locked_answer") or "").lower()
        if locked_a_lower:
            report.add(
                "step3_pick", "no obvious answer leak",
                locked_a_lower not in content.lower(),
                f"answer={locked_a_lower!r}",
            )
        print(f"\nFinal tutor message ({len(content)} chars):")
        print(f"  {_truncate(content, 240)}")
    print()

    # ── Step 4: simulate one engaged follow-up turn to verify tutoring loop works ──
    print("─" * 78)
    print("STEP 4 — student engages with a guess (tutoring loop)")
    print("─" * 78)
    student_attempt = "I think they help reduce friction between organs?"
    state["messages"].append({"role": "student", "content": student_attempt})
    n_msgs_before = len(state["messages"])
    t0 = time.time()
    state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
    dt_engage = time.time() - t0
    print(f"elapsed: {dt_engage:.2f}s")
    print(f"student: {student_attempt}")

    msgs = state.get("messages") or []
    new_tutor_msgs = [
        m for m in msgs[n_msgs_before:]
        if m.get("role") == "tutor"
    ]
    report.add(
        "step4_engage", "tutor responded to engagement",
        len(new_tutor_msgs) >= 1,
        f"new_tutor_msgs={len(new_tutor_msgs)}",
    )
    if new_tutor_msgs:
        last = new_tutor_msgs[-1]
        meta = last.get("metadata") or {}
        is_error = meta.get("kind") == "error_card"
        report.add(
            "step4_engage", "no error_card on engagement",
            not is_error,
            f"kind={meta.get('kind')!r}",
        )
        # Reach gate may fire (the answer matches "reduce friction" alias)
        reached = bool(state.get("student_reached_answer"))
        report.add(
            "step4_engage", "reach gate evaluated (True or False, but ran)",
            "student_reached_answer" in state,
            f"reached={reached} coverage={state.get('student_reach_coverage')}",
        )
        print(f"\nTutor response ({len(last.get('content') or '')} chars):")
        print(f"  {_truncate(last.get('content') or '', 240)}")
        print(f"reach={reached} phase={state.get('phase')} hint={state.get('hint_level')}/{state.get('max_hints')}")
    print()

    # ── Final report ──
    print("=" * 78)
    print("ASSERTION SUMMARY")
    print("=" * 78)
    n_fail = report.pretty()
    print()
    print(
        f"timings — prelock={dt_prelock:.1f}s  rapport={dt_rapport:.1f}s  "
        f"pick={dt_pick:.1f}s  engage={dt_engage:.1f}s  "
        f"total={(dt_prelock + dt_rapport + dt_pick + dt_engage):.1f}s"
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
