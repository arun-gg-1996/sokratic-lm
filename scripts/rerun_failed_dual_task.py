#!/usr/bin/env python3
"""
Re-run dual_task on chunks that didn't successfully produce propositions
in a prior pipeline pass.

Why this exists
The first physics dual_task run had ok=2275, err=1759 because the base
SYSTEM_PROMPT_BODY in ingestion/core/propositions_dual.py is phrased
for anatomy ("preserve every anatomical, physiological, biochemical, or
clinical fact"). Haiku reads physics chunks through that lens and either
returns malformed JSON or empty content. The fix lives in
ingestion/sources/openstax_physics/prompt_overrides.py — but the existing
pipeline re-runs ALL chunks, which would re-spend the $4 we already
paid for the 2275 successful ones. This script identifies just the
failed chunks and re-runs them.

Identification logic
A chunk "needs re-run" if its chunk_id is not the parent_chunk_id of any
proposition in propositions_<source>.jsonl. (The dual_task only writes
propositions for ok=True results.) Empty cleaned_text on a chunk also
counts as a re-run candidate, since base-prompt confusion sometimes
produces empty output that LLM JSON parsing accepts but is useless.

Output
Updates chunks_<source>.jsonl in place (back-fills cleaned text on the
re-run subset) and APPENDS to propositions_<source>.jsonl. Old
propositions are kept; only the failed chunks contribute new ones.

Usage
  SOKRATIC_DOMAIN=physics python scripts/rerun_failed_dual_task.py
  SOKRATIC_DOMAIN=physics python scripts/rerun_failed_dual_task.py --limit 20  # smoke
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
# load .env WITHOUT override so SOKRATIC_DOMAIN from the shell wins. The
# physics ingest needs SOKRATIC_DOMAIN=physics to flip cfg's path resolver,
# and override=True would clobber the shell value if .env happened to set
# an explicit domain.
load_dotenv(ROOT / ".env", override=False)

from config import cfg  # noqa: E402
from conversation.llm_client import (  # noqa: E402
    make_async_anthropic_client,
)
from ingestion.core.cost_tracker import CostTracker  # noqa: E402
from ingestion.core.pipeline import load_source  # noqa: E402
from ingestion.core.propositions_dual import (  # noqa: E402
    DEFAULT_MODEL,
    build_cached_system,
    extract_dual_task,
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-cost", type=float, default=10.0)
    args = ap.parse_args()

    chunks_path = ROOT / cfg.domain_path("chunks")
    props_path = ROOT / cfg.domain_path("propositions")

    print(f"chunks:       {chunks_path}")
    print(f"propositions: {props_path}")

    chunks = [json.loads(l) for l in chunks_path.open()]
    print(f"  total chunks:  {len(chunks)}")

    existing_props: list[dict] = []
    if props_path.exists():
        existing_props = [json.loads(l) for l in props_path.open()]
        print(f"  existing props: {len(existing_props)}")
    else:
        print(f"  no existing props file")

    # Identify chunks that successfully contributed propositions
    success_chunk_ids = {p.get("parent_chunk_id") for p in existing_props}
    print(f"  chunks with at least 1 proposition: {len(success_chunk_ids)}")

    # Failed = chunk_id not in success set
    failed_chunks = [c for c in chunks if c.get("chunk_id") not in success_chunk_ids]
    print(f"  chunks needing re-run: {len(failed_chunks)}")

    if args.limit:
        failed_chunks = failed_chunks[: args.limit]
        print(f"  limited to first {args.limit} for smoke test")

    if not failed_chunks:
        print("nothing to re-run; all chunks already have propositions.")
        return 0

    # Backup before mutating
    bak = props_path.with_suffix(props_path.suffix + ".pre_rerun.bak")
    if props_path.exists():
        shutil.copyfile(props_path, bak)
        print(f"  backup: {bak}")
    chunks_bak = chunks_path.with_suffix(chunks_path.suffix + ".pre_rerun.bak")
    shutil.copyfile(chunks_path, chunks_bak)
    print(f"  backup: {chunks_bak}")

    source = load_source(args.source)
    cached_system = build_cached_system(source.proposition_prompt_suffix)
    print(f"\nsource={args.source}  model={args.model}  concurrency={args.concurrency}")
    print(f"cached_system_size_chars={sum(len(b['text']) for b in cached_system if b.get('type')=='text')}")

    # Bedrock-routed when SOKRATIC_USE_BEDROCK=1 in .env (which is set on
    # this repo's .env), so ingestion uses AWS billing rather than the
    # exhausted direct-Anthropic credit balance.
    client = make_async_anthropic_client()
    sem = asyncio.Semaphore(args.concurrency)
    tracker = CostTracker(model=args.model)
    abort_event = asyncio.Event()

    def record_with_cap(usage: dict) -> float:
        cost = tracker.record(usage)
        if tracker.total_cost > args.max_cost and not abort_event.is_set():
            print(f"\n  COST CAP HIT at ${tracker.total_cost:.4f} > ${args.max_cost} — aborting", flush=True)
            abort_event.set()
        return cost

    # Warmup
    if len(failed_chunks) > 1:
        print("  cache warmup (1 serial call)...")
        warm = await extract_dual_task(
            client, failed_chunks[0], asyncio.Semaphore(1),
            model=args.model, cached_system=cached_system,
            usage_callback=record_with_cap,
        )
        if warm.error:
            print(f"  warmup ERR: {warm.error}")

    print(f"\nre-running {len(failed_chunks)} chunks @ concurrency={args.concurrency}...")
    t0 = time.time()
    tasks = [
        extract_dual_task(
            client, c, sem,
            model=args.model, cached_system=cached_system,
            usage_callback=record_with_cap,
            abort_event=abort_event,
        )
        for c in failed_chunks
    ]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - t0

    n_ok = sum(1 for r in results if r.error is None)
    n_err = len(results) - n_ok
    print(f"\nre-run done in {elapsed:.1f}s  ok={n_ok}  err={n_err}  cost=${tracker.total_cost:.4f}")

    # Sample errors
    err_buckets: dict[str, int] = {}
    for r in results:
        if r.error:
            short = r.error.split(":")[0][:40]
            err_buckets[short] = err_buckets.get(short, 0) + 1
    if err_buckets:
        print("error buckets:")
        for k, v in sorted(err_buckets.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {v:5d}  {k}")

    # Back-fill cleaned text into chunks; collect new propositions
    chunks_by_id = {c["chunk_id"]: c for c in chunks}
    new_props: list[dict] = []
    for r in results:
        if r.error is not None:
            continue
        if r.chunk_id in chunks_by_id and r.cleaned_text:
            chunks_by_id[r.chunk_id]["text"] = r.cleaned_text
        parent = chunks_by_id.get(r.chunk_id, {})
        for p in r.propositions:
            p_full = dict(p)
            for fld in (
                "chapter_num", "chapter_title", "section_num", "section_title",
                "subsection_title", "subsection_id", "page", "chunk_type",
                "sequence_index", "prev_chunk_id", "next_chunk_id",
                "subsection_chunk_count",
            ):
                if fld in parent and fld not in p_full:
                    p_full[fld] = parent[fld]
            new_props.append(p_full)

    # Write merged propositions: keep existing, append new
    merged_props = existing_props + new_props
    print(f"\nwriting {len(merged_props)} propositions ({len(existing_props)} kept + {len(new_props)} new)")
    with props_path.open("w") as f:
        for p in merged_props:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Write chunks back
    with chunks_path.open("w") as f:
        for c in chunks_by_id.values():
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"chunks updated in place")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
