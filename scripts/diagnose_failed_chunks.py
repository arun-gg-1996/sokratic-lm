"""
scripts/diagnose_failed_chunks.py
---------------------------------
Diagnose why ~18% of chunks errored during the B.9 dual-task run.

The pipeline's stage_dual_task counts errors but doesn't write them to
disk — the DualTaskResult.error field stays in memory and is lost on
process exit. This script:

  1. Loads chunks_<source>.jsonl and propositions_<source>.jsonl.
  2. Identifies chunks with NO propositions (failed extraction).
  3. Picks N stratified samples from the failed set (default 20).
  4. Re-runs dual-task on each, INCLUDING the raw model response text
     when JSON parsing fails (so we can see exactly what Haiku returned).
  5. Categorizes the failure mode (empty response / malformed JSON /
     missing fields / "no content" disclaimer / API error / etc.).

After running this, we'll know whether to fix the parser, prompt, or
retry strategy.

Run:
  python scripts/diagnose_failed_chunks.py [--source openstax_anatomy] [--n 20]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

from anthropic import AsyncAnthropic  # noqa: E402

from ingestion.core.propositions_dual import (  # noqa: E402
    DEFAULT_MODEL,
    build_cached_system,
    parse_response,
)


def categorize(text: str, parse_err: str | None) -> str:
    """Bucket the model's response into one of a handful of failure modes."""
    if not text:
        return "empty_response"
    t = text.strip()
    if parse_err and "json parse error" in parse_err:
        return "malformed_json"
    if parse_err == "cleaned_text missing or not a string":
        return "missing_cleaned_text_field"
    if parse_err == "propositions missing or not a list":
        return "missing_propositions_field"
    # Look for prose disclaimers from Haiku
    if re.search(r"(?i)\bno (substantive |medical |factual )?content\b", t):
        return "no_content_disclaimer"
    if re.search(r"(?i)\bcannot (extract|process|parse)\b", t):
        return "model_refusal"
    if re.search(r"(?i)\bno propositions?\b", t):
        return "no_propositions_disclaimer"
    return "unclassified"


async def diagnose_one(client, chunk: dict, cached_system) -> dict:
    """Single chunk; returns diagnostic dict including raw model response."""
    chunk_id = chunk.get("chunk_id", "")
    chunk_text = (chunk.get("text") or "").strip()

    if not chunk_text:
        return {
            "chunk_id": chunk_id, "input_chars": 0,
            "category": "empty_input", "raw_response": "",
            "parse_err": "empty chunk text", "input_text": "",
        }

    try:
        resp = await client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            temperature=0,
            system=cached_system,
            messages=[{"role": "user", "content": chunk_text}],
        )
    except Exception as e:
        return {
            "chunk_id": chunk_id, "input_chars": len(chunk_text),
            "category": "api_error", "raw_response": "",
            "parse_err": f"{type(e).__name__}: {e}",
            "input_text": chunk_text[:200],
        }

    raw = "".join(getattr(b, "text", "") for b in (resp.content or []))
    cleaned, props, parse_err = parse_response(raw)
    cat = categorize(raw, parse_err)
    if not parse_err and (cleaned == "" and len(props) == 0):
        cat = "empty_result_intentional"  # correctly returned empty per Example 5

    return {
        "chunk_id": chunk_id,
        "input_chars": len(chunk_text),
        "category": cat,
        "raw_response": raw[:500],
        "parse_err": parse_err,
        "input_text": chunk_text[:200],
        "n_propositions": len(props) if not parse_err else 0,
    }


def find_failed_chunks(source: str = "openstax_anatomy") -> list[dict]:
    chunks_path = ROOT / f"data/processed/chunks_{source}.jsonl"
    props_path = ROOT / f"data/processed/propositions_{source}.jsonl"

    chunks = [json.loads(l) for l in chunks_path.open()]
    parents = set()
    for line in props_path.open():
        p = json.loads(line)
        parents.add(p.get("parent_chunk_id"))

    failed = [c for c in chunks if c["chunk_id"] not in parents]
    return failed


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="openstax_anatomy")
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()

    failed = find_failed_chunks(args.source)
    print(f"Found {len(failed)} chunks with no propositions in propositions_{args.source}.jsonl")
    print(f"Sampling {min(args.n, len(failed))} for diagnosis...\n")

    random.seed(7)
    samples = random.sample(failed, min(args.n, len(failed)))

    client = AsyncAnthropic()
    cached = build_cached_system()
    sem = asyncio.Semaphore(5)

    async def _run(c):
        async with sem:
            return await diagnose_one(client, c, cached)

    results = await asyncio.gather(*[_run(c) for c in samples])

    # Tally categories
    from collections import Counter
    cats = Counter(r["category"] for r in results)
    print("=" * 80)
    print("CATEGORY BREAKDOWN")
    print("=" * 80)
    for c, n in cats.most_common():
        print(f"  {c:<35} {n}")

    print("\n" + "=" * 80)
    print("PER-SAMPLE DETAIL")
    print("=" * 80)
    for r in results:
        print(f"\n--- {r['chunk_id'][:8]} ({r['input_chars']} chars) "
              f"=> {r['category']} ===")
        print(f"  INPUT:    {r['input_text']}")
        print(f"  RESPONSE: {r['raw_response'][:300]}")
        if r["parse_err"]:
            print(f"  PARSE_ERR: {r['parse_err']}")

    # Save full results for audit
    out = ROOT / "data/artifacts/failed_chunks_diagnostic.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nFull diagnostic written to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
