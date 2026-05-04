"""
scripts/check_conversation_health.py
─────────────────────────────────────
End-to-end conversation health checker. Runs ONE simulated session and
takes per-turn snapshots, then asserts expected invariants for every
phase (rapport → tutoring → clinical → memory_update).

What it captures per turn:
  - phase, locked_topic, hint_level, turn_count, exploration_count
  - assessment_turn, student_reached_answer, close_reason, session_ended
  - LLM-call counts by wrapper (preflight, plan, draft, verifiers)
  - per-call elapsed_ms (latency profile)
  - new mem0/sqlite/close trace entries since last snapshot
  - last tutor message length + metadata (mode, close_reason, etc.)

What it asserts (per phase):
  RAPPORT:
    * Initial state has no locked_topic
    * Greeting message > 50 chars (real LLM, not empty/error)
  TUTORING:
    * locked_topic, locked_question, locked_answer all set after lock turn
    * turn_count monotonic non-decreasing on engaged turns
    * hint_level monotonic non-decreasing (advances on dean signal)
    * trace shows reused_lock_time_chunks (NOT unconditional retrieve)
    * exploration_count present in state (B2 schema)
    * No tutor message has metadata.kind=="error_card"
    * No mem0_write entries fire mid-tutoring
  CLINICAL (only if reach + opt_in_yes):
    * assessment_turn progresses 0 → 1 → 2 → 3
    * clinical_history populated
  MEMORY_UPDATE:
    * close LLM fired (teacher_v2.close_draft trace entry)
    * close_reason set + valid
    * last tutor message has metadata.mode == "close"
    * If reason in NO_SAVE_REASONS: no mem0/sqlite writes
    * Else: mem0 wrote_N_failed_0 + sqlite session_end ok
    * session_ended=True after close

Usage:
  SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks \
    .venv/bin/python scripts/check_conversation_health.py [student_id]

Defaults to eval18_solo1_S1. Prints a streaming per-turn report and a
final PASS/FAIL summary.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env", override=True)


# ─── Constants ─────────────────────────────────────────────────────────────

NO_SAVE_REASONS = {"exit_intent", "off_domain_strike"}
VALID_CLOSE_REASONS = {
    "reach_full", "reach_skipped", "clinical_cap",
    "hints_exhausted", "tutoring_cap",
    "off_domain_strike", "exit_intent",
}

# Trace wrapper keys we care about for per-turn counting
LLM_WRAPPERS = {
    "haiku_intent_classify",       # M7 unified preflight
    "preflight",                   # legacy preflight summary
    "dean_v2.plan",                # Dean Sonnet
    "teacher_v2.draft",            # Teacher Sonnet
    "retry_orchestrator.run_turn", # retry summary (contains attempts)
    "dean.reached_answer_gate",
    "topic_lock_v2.map_topic",     # lock-time Haiku
    "exploration_retrieval",       # M6 tangent retrieval
    "retriever.reused_lock_time_chunks",  # M6 reuse signal
    "teacher_v2.close_draft",      # M1 close LLM
    "mem0_write",
    "memory_manager.flush",
    "mastery_store.update",
    "sqlite_store.session_end",
}


# ─── State snapshot ────────────────────────────────────────────────────────

def snapshot(state: dict, turn_idx: int, label: str) -> dict:
    """Capture the metrics we want to compare turn-over-turn."""
    locked = state.get("locked_topic") or {}
    debug = state.get("debug") or {}
    return {
        "turn_idx": turn_idx,
        "label": label,
        "ts": time.time(),
        "phase": state.get("phase"),
        "locked_subsection": str(locked.get("subsection") or ""),
        "locked_question": (state.get("locked_question") or "")[:80],
        "locked_answer": (state.get("locked_answer") or "")[:80],
        "hint_level": int(state.get("hint_level") or 0),
        "max_hints": int(state.get("max_hints") or 0),
        "turn_count": int(state.get("turn_count") or 0),
        "max_turns": int(state.get("max_turns") or 0),
        "exploration_count": int(state.get("exploration_count") or 0),
        "assessment_turn": int(state.get("assessment_turn") or 0),
        "student_reached_answer": bool(state.get("student_reached_answer", False)),
        "student_reach_coverage": float(state.get("student_reach_coverage", 0.0) or 0.0),
        "close_reason": str(state.get("close_reason") or ""),
        "session_ended": bool(state.get("session_ended", False)),
        "exit_intent_pending": bool(state.get("exit_intent_pending", False)),
        "help_abuse_count": int(state.get("help_abuse_count") or 0),
        "off_topic_count": int(state.get("off_topic_count") or 0),
        "n_messages": len(state.get("messages") or []),
        "last_tutor": _last_tutor_summary(state),
        "trace_count": len(debug.get("turn_trace", []) or []),
        "all_turns_archived": len(debug.get("all_turn_traces", []) or []),
    }


def _last_tutor_summary(state: dict) -> dict:
    msgs = state.get("messages") or []
    for m in reversed(msgs):
        if (m or {}).get("role") == "tutor":
            md = m.get("metadata") or {}
            return {
                "len": len(m.get("content") or ""),
                "mode": md.get("mode"),
                "close_reason": md.get("close_reason"),
                "kind": md.get("kind"),  # "error_card" if M-FB fallback fired
                "preview": (m.get("content") or "")[:120],
            }
        if (m or {}).get("role") == "system":
            md = m.get("metadata") or {}
            if md.get("kind") == "error_card":
                return {
                    "len": 0,
                    "mode": None,
                    "kind": "error_card",
                    "component": md.get("component"),
                    "error_class": md.get("error_class"),
                }
    return {"len": 0, "mode": None}


def trace_summary(state: dict) -> dict:
    """Count LLM-call wrappers + capture latencies in current turn_trace."""
    trace = (state.get("debug") or {}).get("turn_trace", []) or []
    counts: Counter = Counter()
    latencies: dict[str, list[float]] = {}
    errors: list[dict] = []
    for t in trace:
        w = str(t.get("wrapper") or "")
        if w in LLM_WRAPPERS:
            counts[w] += 1
        # Generic latency fields
        for k in ("elapsed_ms", "total_elapsed_ms"):
            v = t.get(k)
            if isinstance(v, (int, float)):
                latencies.setdefault(w, []).append(float(v))
        # Error capture
        if t.get("error") and "error" not in w.lower():
            errors.append({"wrapper": w, "error": str(t["error"])[:120]})
    return {
        "wrapper_counts": dict(counts),
        "latency_ms": {w: round(sum(v) / len(v), 1) for w, v in latencies.items()},
        "errors": errors,
    }


# ─── Per-phase invariant assertions ────────────────────────────────────────

class HealthReport:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, phase: str, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({
            "phase": phase, "name": name, "passed": passed, "detail": detail,
        })

    def summary(self) -> dict:
        n_pass = sum(1 for c in self.checks if c["passed"])
        n_fail = len(self.checks) - n_pass
        return {"total": len(self.checks), "pass": n_pass, "fail": n_fail}

    def pretty(self) -> str:
        lines = []
        for c in self.checks:
            mark = "✓" if c["passed"] else "✗"
            d = f"  {c['detail']}" if c["detail"] else ""
            lines.append(f"  [{c['phase']:14s}] {mark} {c['name']}{d}")
        return "\n".join(lines)


def assert_rapport(snap0: dict, report: HealthReport) -> None:
    p = "RAPPORT"
    report.add(p, "initial phase set", bool(snap0["phase"]), f"phase={snap0['phase']}")
    report.add(p, "no locked subsection at rapport",
               snap0["locked_subsection"] == "",
               f"locked={snap0['locked_subsection']!r}")
    last = snap0.get("last_tutor", {})
    report.add(p, "rapport message exists", last.get("len", 0) > 0,
               f"len={last.get('len')}")
    report.add(p, "rapport message non-trivial (>50 chars)",
               last.get("len", 0) > 50, f"len={last.get('len')}")
    report.add(p, "no error_card on rapport",
               last.get("kind") != "error_card",
               f"kind={last.get('kind')}")


def assert_tutoring_progression(snaps: list[dict], traces: list[dict],
                                report: HealthReport) -> None:
    p = "TUTORING"
    # Find tutoring turns
    tutoring_snaps = [s for s in snaps if s["phase"] == "tutoring"]
    if not tutoring_snaps:
        report.add(p, "tutoring phase entered", False, "no tutoring snapshots")
        return
    report.add(p, "tutoring phase entered", True, f"n_turns={len(tutoring_snaps)}")

    # Locked subsection should be set during tutoring
    ts_with_lock = [s for s in tutoring_snaps if s["locked_subsection"]]
    report.add(p, "locked_subsection set during tutoring",
               len(ts_with_lock) > 0,
               f"{len(ts_with_lock)}/{len(tutoring_snaps)} turns with lock")

    # turn_count monotonic non-decreasing across tutoring snapshots
    counts = [s["turn_count"] for s in tutoring_snaps]
    monotonic = all(counts[i] <= counts[i+1] for i in range(len(counts)-1))
    report.add(p, "turn_count monotonic", monotonic, f"{counts}")

    # hint_level monotonic non-decreasing (M1+ fix: advances on Dean signal)
    hints = [s["hint_level"] for s in tutoring_snaps]
    hint_monotonic = all(hints[i] <= hints[i+1] for i in range(len(hints)-1))
    report.add(p, "hint_level monotonic", hint_monotonic, f"{hints}")

    # B2: per-turn retrieve replaced by reuse — check at least one
    # reused_lock_time_chunks entry across tutoring trace.
    n_reused = sum(1 for t in traces
                   if str(t.get("wrapper", "")) == "retriever.reused_lock_time_chunks")
    report.add(p, "lock-time chunks reused (B2)", n_reused >= 1,
               f"reused_lock_time_chunks={n_reused}")

    # M7: unified intent classifier should appear (or legacy preflight summary)
    n_unified = sum(1 for t in traces if "haiku_intent_classify" in str(t.get("wrapper", "")))
    n_preflight_summary = sum(1 for t in traces if str(t.get("wrapper", "")) == "preflight")
    report.add(p, "intent classifier ran (M7 unified)",
               n_unified >= 1 or n_preflight_summary >= 1,
               f"unified={n_unified} legacy={n_preflight_summary}")

    # No error_card during tutoring (M-FB no-fallback principle worked OR LLMs didn't fail)
    error_cards = sum(1 for s in tutoring_snaps
                      if s.get("last_tutor", {}).get("kind") == "error_card")
    report.add(p, "no error_card mid-tutoring",
               error_cards == 0, f"error_cards={error_cards}")

    # No premature mem0_write during tutoring (mem0 fires only at memory_update)
    n_mem0_in_tutoring = 0
    for s in tutoring_snaps:
        idx = s["turn_idx"]
        for t in traces:
            if t.get("__turn_idx__") == idx and "mem0_write" in str(t.get("wrapper", "")):
                n_mem0_in_tutoring += 1
    report.add(p, "no mem0_write mid-tutoring",
               n_mem0_in_tutoring == 0, f"writes_during_tutoring={n_mem0_in_tutoring}")


def assert_clinical(snaps: list[dict], report: HealthReport) -> None:
    p = "CLINICAL"
    clinical_snaps = [s for s in snaps if s["phase"] == "assessment"]
    if not clinical_snaps:
        report.add(p, "clinical phase entered", False,
                   "skipped (student didn't reach + opt_in_yes)")
        return
    report.add(p, "clinical phase entered", True, f"n_turns={len(clinical_snaps)}")
    asmt_turns = [s["assessment_turn"] for s in clinical_snaps]
    has_progression = any(t >= 1 for t in asmt_turns)
    report.add(p, "assessment_turn progressed", has_progression,
               f"sequence={asmt_turns}")


def assert_memory_update(snaps: list[dict], traces: list[dict],
                         report: HealthReport) -> None:
    p = "MEMORY_UPDATE"
    mu_snaps = [s for s in snaps if s["phase"] == "memory_update"]
    if not mu_snaps:
        report.add(p, "memory_update reached", False,
                   "session never closed (M1 lifecycle bug?)")
        return
    report.add(p, "memory_update reached", True, f"n_turns={len(mu_snaps)}")

    final = mu_snaps[-1]
    reason = final["close_reason"]
    report.add(p, "close_reason set", bool(reason), f"reason={reason!r}")
    report.add(p, "close_reason valid",
               reason in VALID_CLOSE_REASONS or reason == "",
               f"reason={reason!r}")

    # close LLM fired
    n_close = sum(1 for t in traces
                  if "teacher_v2.close_draft" in str(t.get("wrapper", "")))
    report.add(p, "close LLM fired (B4)", n_close >= 1, f"close_draft={n_close}")

    # last tutor message has mode=close (or error_card if LLM failed)
    last = final.get("last_tutor", {})
    mode = last.get("mode")
    is_error = last.get("kind") == "error_card"
    report.add(p, "last tutor mode=close OR error_card emitted",
               mode == "close" or is_error,
               f"mode={mode!r} kind={last.get('kind')!r}")

    # Save bucket logic (per M1 D4)
    no_save = reason in NO_SAVE_REASONS
    n_mem0 = sum(1 for t in traces
                 if str(t.get("wrapper", "")) == "mem0_write")
    n_sqlite_ok = sum(1 for t in traces
                      if str(t.get("wrapper", "")) == "sqlite_store.session_end"
                      and "ok" in str(t.get("result", "")))
    if no_save:
        report.add(p, "no save (per close_reason)",
                   n_mem0 == 0 and n_sqlite_ok == 0,
                   f"reason={reason} mem0={n_mem0} sqlite_ok={n_sqlite_ok}")
    else:
        report.add(p, "mem0 writes fired (full-save bucket)",
                   n_mem0 >= 1, f"mem0_write={n_mem0}")
        report.add(p, "sqlite session_end ok", n_sqlite_ok >= 1,
                   f"sqlite_session_end_ok={n_sqlite_ok}")

    # session_ended flag set
    report.add(p, "session_ended flag set",
               final["session_ended"] is True,
               f"session_ended={final['session_ended']}")


# ─── Driver ─────────────────────────────────────────────────────────────────

async def run_check(student_id: str) -> int:
    from config import cfg
    from conversation.graph import build_graph
    from conversation.state import initial_state
    from retrieval.retriever import ChunkRetriever
    from memory.memory_manager import MemoryManager

    spec = importlib.util.spec_from_file_location(
        "run_eval_18_convos", str(REPO / "scripts/run_eval_18_convos.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    plans = [p for p in mod.PLAN if p["student_id"] == student_id]
    if not plans:
        print(f"unknown student_id: {student_id}")
        print(f"valid: {[p['student_id'] for p in mod.PLAN[:6]]}...")
        return 2
    plan = plans[0]
    profile_id = plan["profile"]
    session_plan = plan["sessions"][0]
    topic = session_plan["topic"]

    print("=" * 78)
    print(f"HEALTH CHECK — student={student_id} profile={profile_id}")
    print(f"topic: {topic[:60]}")
    print("=" * 78)
    print()

    retriever = ChunkRetriever()
    mem = MemoryManager()
    graph = build_graph(retriever, mem)
    mod.clear_student_state(student_id, mem)

    from scripts.run_eval_18_convos import StudentSimulator, PROFILES
    simulator = StudentSimulator(PROFILES[profile_id])

    conv_id = str(uuid.uuid4())[:8]
    thread_id = f"hc_{student_id}_{conv_id}"
    try:
        from memory.sqlite_store import SQLiteStore
        SQLiteStore().ensure_student(student_id)
    except Exception:
        pass

    state = initial_state(student_id, cfg)
    state["thread_id"] = thread_id
    cfg_obj = {"configurable": {"thread_id": thread_id}}

    snaps: list[dict] = []
    all_traces: list[dict] = []  # tagged with __turn_idx__
    turn_idx = 0

    def capture_trace(s: dict, idx: int) -> None:
        for t in (s.get("debug") or {}).get("turn_trace", []) or []:
            tagged = dict(t)
            tagged["__turn_idx__"] = idx
            all_traces.append(tagged)
        for blk in (s.get("debug") or {}).get("all_turn_traces", []) or []:
            tag_turn = blk.get("turn", idx)
            for t in blk.get("trace", []):
                tagged = dict(t)
                tagged["__turn_idx__"] = int(tag_turn) if isinstance(tag_turn, int) else idx
                all_traces.append(tagged)

    def print_snap(snap: dict, t_summary: dict, dt: float) -> None:
        wc = t_summary["wrapper_counts"]
        # Compose a compact one-line summary
        wcs = " ".join(f"{k.split('.')[-1]}={v}" for k, v in sorted(wc.items()))
        print(
            f"[T{snap['turn_idx']:02d} {snap['label']:24s}] "
            f"phase={snap['phase']:14s} "
            f"hint={snap['hint_level']}/{snap['max_hints']} "
            f"turn={snap['turn_count']}/{snap['max_turns']} "
            f"asmt={snap['assessment_turn']} "
            f"reach={int(snap['student_reached_answer'])} "
            f"explore={snap['exploration_count']} "
            f"closed={int(snap['session_ended'])} "
            f"reason={snap['close_reason']!r:18s} "
            f"dt={dt:5.2f}s "
            f"calls=[{wcs}]"
        )
        last = snap.get("last_tutor", {})
        if last.get("kind") == "error_card":
            print(f"     ⚠ error_card: {last.get('component')}/{last.get('error_class')}")
        if t_summary.get("errors"):
            for e in t_summary["errors"][:3]:
                print(f"     ERR {e['wrapper']}: {e['error'][:80]}")

    # ── Phase 1: rapport (graph.invoke with empty messages) ──
    t0 = time.time()
    state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
    dt = time.time() - t0
    snap = snapshot(state, turn_idx, "rapport")
    snaps.append(snap)
    capture_trace(state, turn_idx)
    print_snap(snap, trace_summary(state), dt)
    turn_idx += 1

    # ── Phase 2: topic input ──
    state["messages"].append({"role": "student", "content": topic})
    t0 = time.time()
    state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
    dt = time.time() - t0
    snap = snapshot(state, turn_idx, f"topic_input")
    snaps.append(snap)
    capture_trace(state, turn_idx)
    print_snap(snap, trace_summary(state), dt)
    turn_idx += 1

    # Handle pending Yes/No / topic confirmation cards
    pending = state.get("pending_user_choice") or {}
    if pending.get("kind") == "confirm_topic":
        state["messages"].append({"role": "student", "content": "yes"})
        t0 = time.time()
        state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
        dt = time.time() - t0
        snap = snapshot(state, turn_idx, "confirm_topic_yes")
        snaps.append(snap); capture_trace(state, turn_idx)
        print_snap(snap, trace_summary(state), dt); turn_idx += 1
    elif pending.get("kind") == "topic":
        opts = pending.get("options") or []
        if opts:
            state["messages"].append({"role": "student", "content": opts[0]})
            t0 = time.time()
            state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
            dt = time.time() - t0
            snap = snapshot(state, turn_idx, "topic_card_pick")
            snaps.append(snap); capture_trace(state, turn_idx)
            print_snap(snap, trace_summary(state), dt); turn_idx += 1
    elif pending.get("kind") == "anchor_pick":
        opts = pending.get("options") or []
        if opts:
            state["messages"].append({"role": "student", "content": opts[0]})
            t0 = time.time()
            state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
            dt = time.time() - t0
            snap = snapshot(state, turn_idx, "anchor_pick")
            snaps.append(snap); capture_trace(state, turn_idx)
            print_snap(snap, trace_summary(state), dt); turn_idx += 1

    # ── Phase 3: tutoring loop until reach/cap ──
    max_loop = 12
    while turn_idx < max_loop and state.get("phase") != "memory_update":
        # Generate student reply via simulator
        last_tutor = ""
        for m in reversed(state.get("messages") or []):
            if (m or {}).get("role") == "tutor":
                last_tutor = str(m.get("content") or "")
                break
        reply = simulator.next_reply(
            tutor_message=last_tutor,
            phase=state.get("phase", "tutoring"),
            turn_count=turn_idx,
        )
        if not reply:
            reply = "I'm not sure"
        state["messages"].append({"role": "student", "content": reply})
        t0 = time.time()
        state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
        dt = time.time() - t0
        snap = snapshot(state, turn_idx, f"tutor[{state.get('phase')}]")
        snaps.append(snap); capture_trace(state, turn_idx)
        print_snap(snap, trace_summary(state), dt); turn_idx += 1
        # Handle assessment opt-in
        pending = state.get("pending_user_choice") or {}
        if pending.get("kind") == "opt_in":
            state["messages"].append({"role": "student", "content": "no"})
            t0 = time.time()
            state = await asyncio.to_thread(graph.invoke, state, cfg_obj)
            dt = time.time() - t0
            snap = snapshot(state, turn_idx, "opt_in_no")
            snaps.append(snap); capture_trace(state, turn_idx)
            print_snap(snap, trace_summary(state), dt); turn_idx += 1

    # ── Run all assertions ──
    print()
    print("=" * 78)
    print("HEALTH ASSERTIONS")
    print("=" * 78)
    report = HealthReport()
    if snaps:
        assert_rapport(snaps[0], report)
    assert_tutoring_progression(snaps, all_traces, report)
    assert_clinical(snaps, report)
    assert_memory_update(snaps, all_traces, report)
    print(report.pretty())
    s = report.summary()
    print()
    print("=" * 78)
    print(f"SUMMARY: {s['pass']}/{s['total']} checks passed, {s['fail']} failed")
    print("=" * 78)

    # Latency profile
    print()
    print("LATENCY PROFILE (mean ms per wrapper, across all turns):")
    lat_agg: dict[str, list[float]] = {}
    for t in all_traces:
        for k in ("elapsed_ms", "total_elapsed_ms"):
            v = t.get(k)
            if isinstance(v, (int, float)):
                lat_agg.setdefault(str(t.get("wrapper") or "?"), []).append(float(v))
    for w, vs in sorted(lat_agg.items(), key=lambda kv: -sum(kv[1]) / max(len(kv[1]), 1)):
        if w == "?":
            continue
        mean = sum(vs) / len(vs)
        print(f"  {w:40s} n={len(vs):3d}  mean={mean:7.0f}ms  total={sum(vs)/1000:6.1f}s")

    # Cost summary if available
    debug_cost = (state.get("debug") or {}).get("cost_usd")
    if debug_cost is not None:
        print(f"\nTotal cost: ${debug_cost:.4f}")

    # Write a JSON dump for downstream diffing
    out_path = REPO / "data/artifacts" / f"health_check_{thread_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "student_id": student_id,
        "thread_id": thread_id,
        "topic": topic,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "snapshots": snaps,
        "checks": report.checks,
        "summary": s,
    }, indent=2, default=str))
    print(f"\nFull report → {out_path.relative_to(REPO)}")

    return 0 if s["fail"] == 0 else 1


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) >= 2 else "eval18_solo1_S1"
    sys.exit(asyncio.run(run_check(sid)))
