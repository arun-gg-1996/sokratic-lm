"""
scripts/rerun_failed_chunks.py
------------------------------
Targeted fix-up pass for chunks that errored during B.9.

Loads chunks_<source>.jsonl + propositions_<source>.jsonl, identifies the
chunks with no propositions (errored or intentionally-empty), and re-runs
the dual-task on just those chunks with the bumped max_output_tokens=4096.

Outputs:
  - APPENDS new propositions to data/processed/propositions_<source>.jsonl
  - APPENDS cleaned_text back-fills to chunks (rewrites the chunks file
    with cleaned text where dual-task succeeded this time)
  - EMBEDS the new propositions and upserts them to the existing Qdrant
    collection (no --fresh; existing 54k points stay).

Run:
  python scripts/rerun_failed_chunks.py [--source openstax_anatomy]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

# Force line-buffered stdout — same fix the orchestrator uses.
try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass

from ingestion.core.cost_tracker import (  # noqa: E402
    CostTracker, EmbeddingCostTracker, MultiTracker, make_progress_printer,
)
from ingestion.core.propositions_dual import (  # noqa: E402
    DEFAULT_MODEL, build_cached_system, run_dual_task_batch,
)
from ingestion.core.qdrant import (  # noqa: E402
    PROMPT_VERSION_DEFAULT,
    enrich_chunks_with_subsection_id,
    enrich_chunks_with_window_nav,
    iter_payload_records,
)
from ingestion.core.pipeline import warmup_cache  # noqa: E402


def find_failed_chunks(chunks_path: Path, props_path: Path) -> tuple[list[dict], list[dict]]:
    """Return (failed_chunks, all_chunks_list).
    A "failed" chunk is one with NO entry in propositions_<source>.jsonl.
    Both real errors and intentional empties end up here — we re-run both,
    accepting that intentional empties will return empty again cheaply."""
    chunks = [json.loads(l) for l in chunks_path.open()]
    parents = set()
    for line in props_path.open():
        p = json.loads(line)
        parents.add(p.get("parent_chunk_id"))
    failed = [c for c in chunks if c["chunk_id"] not in parents]
    return failed, chunks


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="openstax_anatomy")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None,
                    help="cap re-run at first N failed chunks (for testing)")
    args = ap.parse_args()

    chunks_path = ROOT / f"data/processed/chunks_{args.source}.jsonl"
    props_path = ROOT / f"data/processed/propositions_{args.source}.jsonl"
    bm25_path = ROOT / f"data/indexes/bm25_{args.source}.pkl"

    failed, all_chunks = find_failed_chunks(chunks_path, props_path)
    print(f"\n=== Fix-up pass: {args.source} ===")
    print(f"  Total chunks:        {len(all_chunks):,}")
    print(f"  Existing propositions: {sum(1 for _ in props_path.open()):,}")
    print(f"  Failed/empty chunks: {len(failed):,}")

    if args.limit:
        failed = failed[: args.limit]
        print(f"  Limited to first {len(failed)}")

    if not failed:
        print("  Nothing to re-run.")
        return 0

    # Load source's prompt suffix (anatomy uses none in v1).
    import importlib
    overrides_mod = importlib.import_module(
        f"ingestion.sources.{args.source}.prompt_overrides"
    )
    suffix = getattr(overrides_mod, "PROPOSITION_PROMPT_SUFFIX", "") or ""

    # ── Run dual-task on failed chunks ─────────────────────────────────────
    tracker = CostTracker(model=DEFAULT_MODEL)
    cached_system = build_cached_system(suffix)

    print(f"\n  [pipeline] cache warmup...")
    await warmup_cache(failed, model=DEFAULT_MODEL,
                       cached_system=cached_system, tracker=tracker)

    print(f"  [pipeline] dual-task on {len(failed)} chunks "
          f"@ concurrency={args.concurrency}, model={DEFAULT_MODEL}, "
          f"max_output_tokens=4096")
    t0 = time.time()
    results = await run_dual_task_batch(
        failed,
        model=DEFAULT_MODEL,
        extra_system_suffix=suffix,
        concurrency=args.concurrency,
        usage_callback=tracker.record,
        progress_callback=make_progress_printer(
            tracker, print_every=max(args.concurrency, 20),
        ),
    )
    elapsed = time.time() - t0

    n_ok = sum(1 for r in results if r.error is None)
    n_err = len(results) - n_ok
    n_with_props = sum(1 for r in results if r.error is None and r.propositions)
    n_intentional_empty = sum(
        1 for r in results if r.error is None and not r.propositions
    )
    print(f"\n  Dual-task elapsed: {elapsed:.1f}s")
    print(f"  ok={n_ok}, err={n_err}, "
          f"with_propositions={n_with_props}, intentional_empty={n_intentional_empty}")
    print(f"  total_cost=${tracker.total_cost:.4f}, "
          f"cache_hit_rate={tracker.cache_hit_rate * 100:.1f}%")

    # ── Back-fill cleaned text into chunks; collect new propositions ──────
    chunks_by_id = {c["chunk_id"]: c for c in all_chunks}
    new_propositions: list[dict] = []
    for r in results:
        if r.error is not None:
            continue
        parent = chunks_by_id.get(r.chunk_id, {})
        if r.cleaned_text:
            parent["text"] = r.cleaned_text
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
            new_propositions.append(p_full)

    print(f"  New propositions extracted: {len(new_propositions):,}")

    # ── Persist ────────────────────────────────────────────────────────────
    # Rewrite chunks file (with backfilled cleaned_text)
    with chunks_path.open("w") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"  Updated {chunks_path.name}")

    # Append new propositions to existing file
    with props_path.open("a") as f:
        for p in new_propositions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"  Appended {len(new_propositions)} propositions to {props_path.name}")

    if not new_propositions:
        print("\n  No new propositions to embed/upsert. Done.")
        return 0

    # ── Embed + upsert ─────────────────────────────────────────────────────
    from openai import OpenAI
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    from config import cfg
    from ingestion.core.index import build_bm25_only

    embed_tracker = EmbeddingCostTracker(model="text-embedding-3-large")
    openai_client = OpenAI()
    qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
    collection = cfg.memory.kb_collection

    # Rebuild BM25 from the FULL propositions file (existing + new).
    print(f"\n  Rebuilding BM25 from full propositions corpus...")
    full_props = [json.loads(l) for l in props_path.open()]
    build_bm25_only(full_props, bm25_path=str(bm25_path))

    print(f"  Embedding + upserting {len(new_propositions)} new points to {collection}...")
    EMBED_BATCH = 100
    UPSERT_BATCH = 200
    points_buffer: list[PointStruct] = []
    upserted = 0

    for i in range(0, len(new_propositions), EMBED_BATCH):
        batch = new_propositions[i : i + EMBED_BATCH]
        texts = [p["text"] for p in batch]
        resp = openai_client.embeddings.create(model="text-embedding-3-large", input=texts)
        embed_tracker.record(getattr(resp.usage, "total_tokens", 0) or 0)
        vectors = [d.embedding for d in resp.data]

        for prop, vec, (_pid, payload) in zip(
            batch, vectors,
            iter_payload_records(batch, chunks_by_id,
                                 textbook_id=args.source,
                                 prompt_version=PROMPT_VERSION_DEFAULT),
        ):
            points_buffer.append(PointStruct(
                id=prop["proposition_id"], vector=vec, payload=payload,
            ))
            if len(points_buffer) >= UPSERT_BATCH:
                qdrant.upsert(collection_name=collection, points=points_buffer)
                upserted += len(points_buffer)
                points_buffer = []
        if (i // EMBED_BATCH) % 5 == 0:
            print(f"    embedded {min(i + EMBED_BATCH, len(new_propositions))}/{len(new_propositions)}")

    if points_buffer:
        qdrant.upsert(collection_name=collection, points=points_buffer)
        upserted += len(points_buffer)

    multi = MultiTracker()
    multi.add(tracker)
    multi.add(embed_tracker)
    print()
    print(multi.summary())
    print(f"\n  Upserted {upserted} new points → {collection}")
    print(f"  Total Qdrant points now: existing 54,045 + {upserted} = {54045 + upserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
