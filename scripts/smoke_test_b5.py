"""
scripts/smoke_test_b5.py
------------------------
Throwaway smoke test for the dual-task module (B.5) on 10 real chunks.
Run before committing to the formal B.8 pilot to surface prompt-level issues.

Picks 10 stratified chunks (mix of noise patterns + clean + short + long),
runs run_dual_task_batch() against the live Sonnet 4.5 API, and prints:
  - Side-by-side original vs cleaned text
  - Propositions per chunk
  - JSON parse rate
  - Cache hit rate (read vs creation tokens) — verifies the cached system
    block is actually being reused starting at call 2
  - Total token usage and approximate cost

No artifacts written to disk; pure smoke test.

Run:
  source .venv/bin/activate
  python scripts/smoke_test_b5.py
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

from ingestion.core.propositions_dual import (  # noqa: E402
    DEFAULT_MODEL, run_dual_task_batch,
)


CHUNKS_PATH = ROOT / "data/processed/chunks_openstax_anatomy.jsonl"

# Sonnet 4.5 pricing (per 1M tokens, USD).
PRICE_INPUT = 3.00
PRICE_OUTPUT = 15.00
PRICE_CACHE_WRITE = 3.75   # 1.25× input
PRICE_CACHE_READ = 0.30    # 0.10× input


def pick_stratified_chunks() -> list[dict]:
    """Pick 10 chunks: 3 with known noise patterns, 4 mixed, 3 clean."""
    random.seed(7)
    rows = []
    with CHUNKS_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            if r.get("element_type") == "paragraph":
                rows.append(r)

    def has_url(t: str) -> bool:
        return "http" in t.lower() or "openstax.org" in t.lower()

    def has_il(t: str) -> bool:
        return "INTERACTIVE LINK" in t.upper()

    def has_lo(t: str) -> bool:
        return "LEARNING OBJECTIVES" in t.upper() or "by the end of this section" in t.lower()

    noise_url = [r for r in rows if has_url(r["text"])]
    noise_il = [r for r in rows if has_il(r["text"]) and not has_url(r["text"])]
    noise_lo = [r for r in rows if has_lo(r["text"]) and not has_url(r["text"]) and not has_il(r["text"])]
    short = [r for r in rows if 200 <= len(r["text"]) < 600 and not has_url(r["text"])
             and not has_il(r["text"]) and not has_lo(r["text"])]
    long_ = [r for r in rows if 1500 <= len(r["text"]) <= 2500 and not has_url(r["text"])
             and not has_il(r["text"]) and not has_lo(r["text"])]
    medium = [r for r in rows if 800 <= len(r["text"]) < 1500 and not has_url(r["text"])
              and not has_il(r["text"]) and not has_lo(r["text"])]

    picks: list[dict] = []
    seen_ids: set[str] = set()

    def take(bucket, n):
        random.shuffle(bucket)
        added = 0
        for r in bucket:
            if r["chunk_id"] in seen_ids:
                continue
            picks.append(r)
            seen_ids.add(r["chunk_id"])
            added += 1
            if added == n:
                return

    take(noise_url, 2)        # 2 with URL noise
    take(noise_il, 1)         # 1 with INTERACTIVE LINK
    take(noise_lo, 1)         # 1 with LEARNING OBJECTIVES
    take(short, 2)            # 2 short clean
    take(medium, 2)           # 2 medium clean
    take(long_, 2)            # 2 long clean
    return picks


async def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Anthropic model to test (default: %(default)s)")
    ap.add_argument("--concurrency", type=int, default=10)
    args = ap.parse_args()

    # Re-derive pricing for the chosen model so the cost report is correct
    # whether we test Sonnet or Haiku.
    from ingestion.core.cost_tracker import get_pricing
    p = get_pricing(args.model)
    global PRICE_INPUT, PRICE_OUTPUT, PRICE_CACHE_WRITE, PRICE_CACHE_READ
    PRICE_INPUT = p["input_tokens"]
    PRICE_OUTPUT = p["output_tokens"]
    PRICE_CACHE_WRITE = p["cache_creation_input_tokens"]
    PRICE_CACHE_READ = p["cache_read_input_tokens"]

    picks = pick_stratified_chunks()
    print(f"Picked {len(picks)} chunks (stratified)")
    for i, c in enumerate(picks, 1):
        text = c["text"]
        flags = []
        if "http" in text.lower():
            flags.append("URL")
        if "INTERACTIVE LINK" in text.upper():
            flags.append("IL")
        if "LEARNING OBJECTIVES" in text.upper() or "by the end of this section" in text.lower():
            flags.append("LO")
        flags_str = "+".join(flags) if flags else "clean"
        print(f"  {i:>2}. ch{c.get('chapter_num'):>2} p{c.get('page'):>4} "
              f"len={len(text):>4} [{flags_str:>5}] {c.get('section_title','')[:55]}")

    print(f"\nRunning dual-task batch on {len(picks)} chunks "
          f"(model={args.model}, concurrency={args.concurrency})...")

    usage_log: list[dict] = []
    progress_log: list[tuple[int, int]] = []

    t0 = time.time()
    results = await run_dual_task_batch(
        picks,
        model=args.model,
        concurrency=args.concurrency,
        usage_callback=usage_log.append,
        progress_callback=lambda d, t: progress_log.append((d, t)),
    )
    elapsed = time.time() - t0

    # ── Per-chunk side-by-side report ────────────────────────────────────────
    print("\n" + "=" * 90)
    print("PER-CHUNK RESULTS")
    print("=" * 90)
    for i, (chunk, result) in enumerate(zip(picks, results), 1):
        original = chunk["text"]
        ratio = (len(result.cleaned_text) / max(len(original), 1)) * 100 if result.cleaned_text else 0
        print(f"\n--- chunk {i}/{len(picks)} (chunk_id={chunk['chunk_id'][:8]}, page {chunk.get('page')}) ---")
        if result.error:
            print(f"  ERROR: {result.error}")
            print(f"  ORIGINAL ({len(original)} chars): {original[:160]}...")
            continue

        # Show first 200 chars of original and cleaned for diff visibility
        print(f"  ORIGINAL  ({len(original):>4} chars): {original[:180].strip()}{'...' if len(original) > 180 else ''}")
        print(f"  CLEANED   ({len(result.cleaned_text):>4} chars, {ratio:.0f}% of orig): "
              f"{result.cleaned_text[:180].strip()}{'...' if len(result.cleaned_text) > 180 else ''}")
        print(f"  PROPS ({len(result.propositions)}):")
        for j, p in enumerate(result.propositions[:5], 1):
            print(f"    {j}. {p['text'][:130]}")
        if len(result.propositions) > 5:
            print(f"    ... +{len(result.propositions) - 5} more")

    # ── Aggregate metrics ────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("AGGREGATE METRICS")
    print("=" * 90)
    n = len(results)
    n_ok = sum(1 for r in results if r.error is None)
    n_err = n - n_ok
    parse_rate = (n_ok / n) * 100 if n else 0

    avg_props = (sum(len(r.propositions) for r in results if r.error is None)
                 / max(n_ok, 1))
    avg_orig = sum(len(c["text"]) for c in picks) / n
    avg_cleaned = sum(len(r.cleaned_text) for r in results if r.error is None) / max(n_ok, 1)
    avg_clean_ratio = (avg_cleaned / avg_orig) * 100

    total_in = sum(u["input_tokens"] for u in usage_log)
    total_out = sum(u["output_tokens"] for u in usage_log)
    total_cw = sum(u["cache_creation_input_tokens"] for u in usage_log)
    total_cr = sum(u["cache_read_input_tokens"] for u in usage_log)

    print(f"  parse rate:       {n_ok}/{n} = {parse_rate:.0f}%   (errors: {n_err})")
    print(f"  avg props/chunk:  {avg_props:.1f}")
    print(f"  avg cleaned len:  {avg_cleaned:.0f} chars  ({avg_clean_ratio:.0f}% of original)")
    print(f"  wall time:        {elapsed:.1f}s")
    print()
    print(f"  TOKENS")
    print(f"    input:          {total_in:>6}")
    print(f"    output:         {total_out:>6}")
    print(f"    cache write:    {total_cw:>6}  (paid 1.25x — first call to write the cached prefix)")
    print(f"    cache read:     {total_cr:>6}  (paid 0.10x — every call after the first)")
    print()

    cost = (total_in * PRICE_INPUT
            + total_out * PRICE_OUTPUT
            + total_cw * PRICE_CACHE_WRITE
            + total_cr * PRICE_CACHE_READ) / 1_000_000
    cost_uncached = ((total_in + total_cw + total_cr) * PRICE_INPUT
                     + total_out * PRICE_OUTPUT) / 1_000_000

    print(f"  COST")
    print(f"    actual:         ${cost:.4f}")
    print(f"    if uncached:    ${cost_uncached:.4f}  ({(cost_uncached - cost) / max(cost_uncached, 1e-9) * 100:.0f}% saved by cache)")
    print(f"    extrapolated to full corpus (~2766 chunks): ${cost / n * 2766:.2f}")

    # Cache verification
    print()
    if total_cr > 0:
        print(f"  ✓ cache HIT detected (cache_read tokens > 0). Caching is working.")
    elif total_cw > 0 and n > 1:
        print(f"  ⚠  cache_write happened but no cache_read across {n} calls. "
              f"This is unexpected — caching should activate after call 1.")
    else:
        print(f"  - no cache activity. Possibly cache prefix below 1024-token threshold.")

    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
