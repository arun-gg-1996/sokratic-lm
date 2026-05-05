"""
scripts/run_eval_chain.py
─────────────────────────
Single-chain driver. Runs ONE student_id from the 18-convo plan
(all sessions in that chain, sequentially) in a fresh process.

Used to sidestep the in-process concurrency issue: launch 4 of these
in parallel from the shell, each gets its own ChunkRetriever +
in-process state. No shared CrossEncoder, no thread-safety surprise.

Usage:
  .venv/bin/python scripts/run_eval_chain.py <student_id>

Outputs to data/artifacts/eval_run_18/<student_id>_session{N}.json
(same paths as run_eval_18_convos.py so the existing scorer/aggregator
just works on the combined output).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env", override=True)


async def main(student_id: str) -> int:
    from config import cfg                                    # noqa: E402
    from conversation.graph import build_graph                # noqa: E402
    from retrieval.retriever import ChunkRetriever            # noqa: E402
    from memory.memory_manager import MemoryManager           # noqa: E402

    # Reuse the 18-convo plan + helpers — no need to duplicate
    spec = importlib.util.spec_from_file_location(
        "run_eval_18_convos", str(REPO / "scripts/run_eval_18_convos.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    plans = [p for p in mod.PLAN if p["student_id"] == student_id]
    if not plans:
        print(f"[chain] unknown student_id: {student_id!r}", flush=True)
        print(f"[chain] valid: {[p['student_id'] for p in mod.PLAN]}", flush=True)
        return 2
    plan = plans[0]

    print(f"[chain] {student_id} — profile={plan['profile']} sessions={len(plan['sessions'])}", flush=True)

    retriever = ChunkRetriever()
    mem = MemoryManager()
    graph = build_graph(retriever, mem)

    # Clear per-student state once at the top so session#1 starts clean
    mod.clear_student_state(student_id, mem)

    out_dir = REPO / "data/artifacts/eval_run_18"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, session in enumerate(plan["sessions"], start=1):
        topic = session["topic"]
        print(f"[chain] {student_id} session {i}/{len(plan['sessions'])} — topic: {topic[:60]}", flush=True)
        try:
            res = await mod.run_one_session(
                student_id=student_id,
                profile_id=plan["profile"],
                topic=topic,
                session_index=i,
                graph=graph,
            )
        except Exception as e:
            print(f"[chain] {student_id} session {i} EXC {type(e).__name__}: {e}", flush=True)
            continue

        o = res["outcome"]
        cost = res.get("debug_summary", {}).get("cost_usd", 0)
        print(
            f"[chain] {student_id} s{i} DONE — "
            f"reached={o['reached_answer']} phase={o['phase_final']} "
            f"turns={o['turn_count']} cost=${cost:.4f}",
            flush=True,
        )

        out_path = out_dir / f"{student_id}_session{i}.json"
        out_path.write_text(json.dumps(res, indent=2, default=str))

    print(f"[chain] {student_id} chain complete", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: run_eval_chain.py <student_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
