"""
scripts/run_scaled_convos.py
----------------------------
Scaled e2e harness for measuring conversation quality + latency + cost.

Why this exists
---------------
The original `run_final_convos.py` runs 6 hardcoded conversations — useful
as a smoke test, useless for tracking iteration impact. This harness:

  - Loads a topic bank (`data/eval/topic_bank_v1.jsonl`) — 30 textbook-
    answerable topics with profile-specific student phrasings.
  - Stratifies: 3 seeds × 6 profiles = 18 conversations per run (configurable).
  - Captures per-call cache stats by patching the Anthropic SDK (same trick
    used by cache_smoke_test.py).
  - Aggregates an at-a-glance report: per-profile success rates, per-turn
    latency p50/p95, cache hit rate, total cost.

Output
------
  data/artifacts/scaled_convo/<timestamp>/
    convo_<profile>_<seed>_<conv_id>.json     — per-convo full state + turns
    summary.json                               — aggregate metrics
    summary.txt                                — human-readable report

Cost
----
  18 conversations × ~$0.30 average = ~$5–7
  Wall: ~30–45 min

Usage
-----
  cd /Users/arun-ghontale/UB/NLP/sokratic
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_scaled_convos.py
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_scaled_convos.py --seeds 1   # smoke
  SOKRATIC_RETRIEVER=chunks .venv/bin/python scripts/run_scaled_convos.py --label cache_fix_v1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

# Patch the Anthropic SDK BEFORE importing dean/teacher so cache stats land
# on every call. Same trick as scripts/cache_smoke_test.py.
import anthropic  # noqa: E402

_ALL_API_CALLS: list[dict] = []
# contextvars: async-safe per-task storage. Each parallel conversation gets
# its own value, so cache-stat attribution works correctly even when
# `asyncio.gather` is running multiple conversations concurrently.
import contextvars  # noqa: E402
_current_convo: contextvars.ContextVar[str] = contextvars.ContextVar(
    "scaled_convos_current_id", default=""
)


def _patched_messages_create(self, *args, **kwargs):
    resp = self._original_messages_create(*args, **kwargs)
    usage = getattr(resp, "usage", None)
    # list.append is atomic under the GIL — safe even under thread-pool
    # scheduling that LangGraph uses to run sync nodes from async ainvoke.
    _ALL_API_CALLS.append({
        "convo_id": _current_convo.get(),
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    })
    return resp


def _install_patch():
    from anthropic.resources.messages.messages import Messages
    if not hasattr(Messages, "_original_messages_create"):
        Messages._original_messages_create = Messages.create
        Messages.create = _patched_messages_create


_install_patch()

# Now import the rest.
from config import cfg  # noqa: E402
from conversation.state import initial_state  # noqa: E402
from conversation.graph import build_graph  # noqa: E402
from memory.memory_manager import MemoryManager  # noqa: E402
from simulation.profiles import PROFILES  # noqa: E402
from simulation.student_simulator import StudentSimulator  # noqa: E402

TOPIC_BANK_PATH = ROOT / "data/eval/topic_bank_v2.jsonl"  # v2: anatomy-only (Ch3-Ch28)
PROFILES_TO_RUN = ["S1", "S2", "S3", "S4", "S5", "S6"]
MAX_TURNS_PER_CONVO = 25  # safety cap


def load_topic_bank(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path)]


def pick_topics_for_profile(bank: list[dict], profile: str, n: int, rng: random.Random) -> list[dict]:
    """Pick `n` distinct topics from the bank that have a phrasing for this profile."""
    eligible = [t for t in bank if (t.get("queries") or {}).get(profile)]
    rng.shuffle(eligible)
    return eligible[:n]


async def run_one_convo(
    profile_id: str,
    topic_row: dict,
    seed_idx: int,
    graph,
    out_dir: Path,
) -> dict:
    """Run one conversation; return its summary record."""
    conv_id = str(uuid.uuid4())[:8]
    student_id = f"{profile_id}_seed{seed_idx}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_config = {"configurable": {"thread_id": conv_id}}
    simulator = StudentSimulator(PROFILES[profile_id])

    query = topic_row["queries"][profile_id]
    expected_section = topic_row.get("section_title", "")
    expected_subsection = topic_row.get("subsection_title", "")
    expected_chapter = topic_row.get("chapter_num")

    # Tag every API call inside this conversation with its conv_id.
    # contextvars.set returns a token we MUST reset to keep parallel
    # conversations isolated — without reset, a later sibling task could
    # inherit this convo's id.
    _convo_token = _current_convo.set(conv_id)
    api_calls_before = len(_ALL_API_CALLS)
    t_start = time.time()

    turns_log = []
    bugs_noted = []

    # --- Phase 1: Rapport ---
    try:
        state["phase"] = "rapport"
        state = await graph.ainvoke(state, thread_config)
    except Exception as e:
        bugs_noted.append(f"rapport_error: {type(e).__name__}: {e}")

    # --- Phase 2: Topic input ---
    try:
        state["messages"] = list(state.get("messages", [])) + [
            {"role": "student", "content": query, "phase": "topic_input"}
        ]
        state["phase"] = "topic_engagement"
        state = await graph.ainvoke(state, thread_config)
    except Exception as e:
        bugs_noted.append(f"topic_engagement_error: {type(e).__name__}: {e}")

    # --- Phase 3: Tutoring loop ---
    turn_count = 0
    while turn_count < MAX_TURNS_PER_CONVO:
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
            bugs_noted.append(f"turn_{turn_count}_error: {type(e).__name__}: {e}")
            break
        if state.get("student_reached_answer") and state.get("phase") == "memory_update":
            break
        if state.get("phase") in ("memory_update", "session_end"):
            break

    elapsed = time.time() - t_start
    api_calls_in_convo = _ALL_API_CALLS[api_calls_before:]

    # Aggregate cache + cost stats for this convo
    total_input = sum(c["input_tokens"] for c in api_calls_in_convo)
    total_output = sum(c["output_tokens"] for c in api_calls_in_convo)
    total_cache_read = sum(c["cache_read_input_tokens"] for c in api_calls_in_convo)
    total_cache_write = sum(c["cache_creation_input_tokens"] for c in api_calls_in_convo)

    # Score: did retrieval ground the conversation in the right area?
    # We score "on_topic" based on whether ANY of the retrieved chunks the
    # tutor was working from carry the expected (chapter, section,
    # subsection). State["locked_topic"] is set by the dean but isn't
    # always retained on the LangGraph state at session end (depends on
    # what the most-recent node returned), so retrieved_chunks is the
    # more reliable proxy.
    locked_answer = state.get("locked_answer", "") or ""
    locked_topic = state.get("locked_topic") or {}
    retrieved = state.get("retrieved_chunks", []) or []

    def _on_topic(chunks: list[dict]) -> tuple[bool, bool, bool]:
        """Returns (subsection_match, section_match, chapter_match)."""
        if not chunks:
            return (False, False, False)
        sub = bool(expected_subsection) and any(
            (c.get("subsection_title") or "") == expected_subsection for c in chunks
        )
        sec = bool(expected_section) and any(
            (c.get("section_title") or "") == expected_section for c in chunks
        )
        ch = expected_chapter is not None and any(
            c.get("chapter_num") == expected_chapter for c in chunks
        )
        return (sub, sec, ch)

    sub_hit, sec_hit, ch_hit = _on_topic(retrieved)
    # locked_topic still useful when present — fall back to it if retrieved
    # was emptied at session end.
    if not (sub_hit or sec_hit or ch_hit) and locked_topic:
        if locked_topic.get("subsection") == expected_subsection:
            sub_hit = True
        if locked_topic.get("section") == expected_section:
            sec_hit = True
    on_topic_section = sub_hit or sec_hit
    on_topic_chapter = sub_hit or sec_hit or ch_hit

    record = {
        "conv_id": conv_id,
        "profile_id": profile_id,
        "seed_idx": seed_idx,
        "topic_id": topic_row.get("topic_id"),
        "expected_section": expected_section,
        "expected_subsection": expected_subsection,
        "expected_chapter": expected_chapter,
        "query": query,
        "outcome": {
            "topic_confirmed": bool(state.get("topic_confirmed")),
            "locked_answer": locked_answer,
            "reached_answer": bool(state.get("student_reached_answer")),
            "phase_final": state.get("phase"),
            "turn_count": int(state.get("turn_count", turn_count)),
            "on_topic_section": on_topic_section,
            "on_topic_chapter": on_topic_chapter,
            "locked_topic_path": (
                f"{locked_topic.get('chapter','')}|"
                f"{locked_topic.get('section','')}|"
                f"{locked_topic.get('subsection','')}"
                if locked_topic else ""
            ),
        },
        "metrics": {
            "wall_seconds": round(elapsed, 1),
            "n_api_calls": len(api_calls_in_convo),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_input_tokens": total_cache_read,
            "cache_creation_input_tokens": total_cache_write,
            "cache_hit_ratio": round(
                total_cache_read / max(total_input + total_cache_read, 1), 3
            ),
        },
        "messages": state.get("messages", []),
        "bugs_noted": bugs_noted,
    }

    # Save per-convo JSON
    out_path = out_dir / f"convo_{profile_id}_seed{seed_idx}_{conv_id}.json"
    with open(out_path, "w") as f:
        json.dump(record, f, indent=2, default=str)

    print(
        f"  ✓ {profile_id}/seed{seed_idx} "
        f"sec_hit={on_topic_section} ch_hit={on_topic_chapter} reached={record['outcome']['reached_answer']} "
        f"turns={record['outcome']['turn_count']} wall={elapsed:.0f}s "
        f"calls={len(api_calls_in_convo)} cache_hit={record['metrics']['cache_hit_ratio']*100:.0f}% "
        f"locked={(locked_answer or '<empty>')[:30]}",
        flush=True,
    )
    # Reset the contextvar so a sibling task in the same parent context
    # doesn't accidentally inherit this convo's id.
    _current_convo.reset(_convo_token)
    return record


def aggregate_report(records: list[dict], out_dir: Path, run_label: str) -> None:
    """Build summary.json and summary.txt."""
    n = len(records)

    def pct(num, den):
        return f"{(num / max(den, 1)) * 100:5.1f}%"

    # By profile
    by_profile: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_profile[r["profile_id"]].append(r)

    overall = {
        "n_convos": n,
        "topic_confirmed_rate": sum(1 for r in records if r["outcome"]["topic_confirmed"]) / max(n, 1),
        "reached_answer_rate": sum(1 for r in records if r["outcome"]["reached_answer"]) / max(n, 1),
        "on_topic_section_rate": sum(1 for r in records if r["outcome"]["on_topic_section"]) / max(n, 1),
        "on_topic_chapter_rate": sum(1 for r in records if r["outcome"]["on_topic_chapter"]) / max(n, 1),
        "locked_answer_present_rate": sum(
            1 for r in records if r["outcome"]["locked_answer"]
        ) / max(n, 1),
        "avg_turns": sum(r["outcome"]["turn_count"] for r in records) / max(n, 1),
        "avg_wall_secs": sum(r["metrics"]["wall_seconds"] for r in records) / max(n, 1),
        "total_input_tokens": sum(r["metrics"]["input_tokens"] for r in records),
        "total_output_tokens": sum(r["metrics"]["output_tokens"] for r in records),
        "total_cache_read": sum(r["metrics"]["cache_read_input_tokens"] for r in records),
        "total_cache_write": sum(r["metrics"]["cache_creation_input_tokens"] for r in records),
        "overall_cache_hit_ratio": (
            sum(r["metrics"]["cache_read_input_tokens"] for r in records)
            / max(
                sum(
                    r["metrics"]["input_tokens"] + r["metrics"]["cache_read_input_tokens"]
                    for r in records
                ),
                1,
            )
        ),
    }

    # Build text report
    lines: list[str] = []
    lines.append("=" * 88)
    lines.append(f"SCALED E2E SUMMARY — {run_label}")
    lines.append(f"Run: {out_dir.name}   |   Convos: {n}")
    lines.append("=" * 88)
    lines.append("")
    lines.append("Overall:")
    lines.append(f"  topic_confirmed:     {pct(overall['topic_confirmed_rate'] * n, n)}")
    lines.append(f"  on_topic (section):  {pct(overall['on_topic_section_rate'] * n, n)}")
    lines.append(f"  on_topic (chapter):  {pct(overall['on_topic_chapter_rate'] * n, n)}")
    lines.append(f"  locked_answer set:   {pct(overall['locked_answer_present_rate'] * n, n)}")
    lines.append(f"  reached_answer:      {pct(overall['reached_answer_rate'] * n, n)}")
    lines.append(f"  avg turns/convo:     {overall['avg_turns']:.1f}")
    lines.append(f"  avg wall/convo:      {overall['avg_wall_secs']:.1f} s")
    lines.append(f"  total input toks:    {overall['total_input_tokens']:,}")
    lines.append(f"  total output toks:   {overall['total_output_tokens']:,}")
    lines.append(f"  total cache_read:    {overall['total_cache_read']:,}")
    lines.append(f"  total cache_write:   {overall['total_cache_write']:,}")
    lines.append(f"  cache hit ratio:     {overall['overall_cache_hit_ratio']*100:.1f}%")
    lines.append("")
    lines.append("Per profile:")
    lines.append(f"  {'profile':<10} {'n':>3} {'topic✓':>8} {'sec_hit':>8} {'ch_hit':>7} {'locked✓':>9} {'reached':>8} {'turns':>6} {'wall_s':>7}")
    for prof in PROFILES_TO_RUN:
        rs = by_profile.get(prof, [])
        if not rs:
            continue
        nn = len(rs)
        tc = sum(1 for r in rs if r["outcome"]["topic_confirmed"])
        ot_sec = sum(1 for r in rs if r["outcome"]["on_topic_section"])
        ot_ch = sum(1 for r in rs if r["outcome"]["on_topic_chapter"])
        la = sum(1 for r in rs if r["outcome"]["locked_answer"])
        ra = sum(1 for r in rs if r["outcome"]["reached_answer"])
        avg_t = sum(r["outcome"]["turn_count"] for r in rs) / nn
        avg_w = sum(r["metrics"]["wall_seconds"] for r in rs) / nn
        lines.append(
            f"  {prof:<10} {nn:>3} {pct(tc, nn):>8} {pct(ot_sec, nn):>8} {pct(ot_ch, nn):>7} "
            f"{pct(la, nn):>9} {pct(ra, nn):>8} {avg_t:>6.1f} {avg_w:>7.1f}"
        )
    lines.append("")
    lines.append("Failures (no chapter hit AND no locked_answer):")
    for r in records:
        if not r["outcome"]["on_topic_chapter"] and not r["outcome"]["locked_answer"]:
            lines.append(
                f"  [{r['profile_id']}/seed{r['seed_idx']}] {r['query'][:80]}"
            )
            lines.append(
                f"     expected: ch{r.get('expected_chapter')} | sec={r.get('expected_section', '')!r}"
            )
            lines.append(
                f"     got_path: {r['outcome'].get('locked_topic_path', '')[:80]}"
            )

    text_report = "\n".join(lines)
    print("\n" + text_report)

    # Save
    with open(out_dir / "summary.json", "w") as f:
        json.dump(
            {"overall": overall, "by_profile_n": {p: len(by_profile.get(p, [])) for p in PROFILES_TO_RUN}, "records": records},
            f,
            indent=2,
            default=str,
        )
    with open(out_dir / "summary.txt", "w") as f:
        f.write(text_report)
    print(f"\nReport saved → {out_dir.relative_to(ROOT)}/summary.{{json,txt}}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3,
                    help="Conversations per profile (default: 3)")
    ap.add_argument("--profiles", default=None,
                    help="Comma-separated profile IDs to run (default: all S1-S6)")
    ap.add_argument("--label", default="run",
                    help="Tag for this run (used in output dir name)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for topic selection")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="Number of conversations to run concurrently "
                         "(default: 3; 1 = serial). Higher values get rate-"
                         "limited by Anthropic API.")
    ap.add_argument("--clear-memory", action="store_true",
                    help="Wipe the mem0 namespace before this run. Use for "
                         "clean baseline measurements so prior session "
                         "memories don't bleed into the metrics.")
    args = ap.parse_args()

    profiles = [p.strip() for p in (args.profiles or ",".join(PROFILES_TO_RUN)).split(",") if p.strip()]

    bank = load_topic_bank(TOPIC_BANK_PATH)
    print(f"Loaded {len(bank)} topics from {TOPIC_BANK_PATH.relative_to(ROOT)}", flush=True)

    rng = random.Random(args.seed)

    # Build retriever + graph once
    retriever_kind = os.environ.get("SOKRATIC_RETRIEVER", "chunks").strip().lower()
    if retriever_kind == "chunks":
        from retrieval.retriever import ChunkRetriever as Retriever
    else:
        from retrieval.retriever import Retriever  # type: ignore

    print(f"Building retriever ({retriever_kind})...", flush=True)
    try:
        retriever = Retriever()
    except Exception as e:
        print(f"Retriever load FAILED: {e}", flush=True)
        return

    memory_manager = MemoryManager()
    if args.clear_memory:
        n = memory_manager.clear_namespace()
        print(f"Cleared mem0 namespace (returned {n}).", flush=True)
    print(f"MemoryManager status: {memory_manager.last_flush_status}", flush=True)
    graph = build_graph(retriever, memory_manager)
    print("Graph built.", flush=True)

    # Output dir
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = ROOT / f"data/artifacts/scaled_convo/{stamp}_{args.label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Plan: per-profile, pick `args.seeds` topics
    plan: list[tuple[str, dict, int]] = []
    for prof in profiles:
        topics = pick_topics_for_profile(bank, prof, args.seeds, rng)
        for i, t in enumerate(topics):
            plan.append((prof, t, i))

    print(f"\nPlan: {len(plan)} conversations across {len(profiles)} profiles "
          f"× {args.seeds} seeds.\n", flush=True)

    # Parallelize via asyncio.gather + Semaphore. Each conversation runs in
    # its own asyncio task; the semaphore caps in-flight conversations to
    # `args.concurrency`. Each task gets its own contextvar value (set inside
    # run_one_convo) so cache-stat attribution stays correct.
    #
    # Why this gives real parallelism even though dean/teacher use sync
    # `anthropic.Anthropic`: LangGraph's `ainvoke` schedules sync nodes onto
    # an executor thread pool. Multiple awaiting `ainvoke` calls run their
    # nodes on different threads. Network I/O (Anthropic + OpenAI + Qdrant)
    # is the actual time spent, and that I/O happens concurrently across
    # threads.
    #
    # Concurrency limits:
    #   - Anthropic Sonnet 4.5 Tier-1 ≈ 50 RPM. Each convo emits ~30-70
    #     calls over ~2 min ≈ 15-35 RPM peak. Safe up to ~3 concurrent.
    #   - OpenAI text-embedding-3-large: large rate budget; not a bottleneck.
    #   - Qdrant: local; not a bottleneck.
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))

    async def _run_with_sem(prof, topic, seed_idx, k):
        async with sem:
            print(f"\n--- [{k:>2}/{len(plan)}] {prof}/seed{seed_idx} — "
                  f"{topic.get('subsection_title', '')} ---", flush=True)
            return await run_one_convo(prof, topic, seed_idx, graph, out_dir)

    print(f"Running with concurrency={args.concurrency}.\n", flush=True)
    t_run_start = time.time()
    tasks = [
        _run_with_sem(prof, topic, seed_idx, k)
        for k, (prof, topic, seed_idx) in enumerate(plan, 1)
    ]
    records = await asyncio.gather(*tasks, return_exceptions=False)

    print(f"\n{'='*60}\nALL CONVOS DONE in {int(time.time()-t_run_start)}s.\n",
          flush=True)
    aggregate_report(list(records), out_dir, run_label=args.label)


if __name__ == "__main__":
    asyncio.run(main())
