"""
scripts/verify_bedrock_cache.py
================================
BLOCK 0 — verify Bedrock honors multi-block cache_control before we
commit to a 4-tier prompt cache architecture.

Tests three scenarios:
  T1: Single cache_control block (current sokratic pattern)
  T2: Two cache_control blocks (proposed REAL-Q7 minimum)
  T3: Three cache_control blocks (proposed REAL-Q7 typical)

Each test makes 2 sequential calls. Reports cache_read_input_tokens
on call 2 to confirm cache actually hit.

Run:
    cd /Users/arun-ghontale/UB/NLP/sokratic
    python scripts/verify_bedrock_cache.py

Expected (if Bedrock supports multi-block):
  T1: call 2 cache_read_input_tokens > 0 (single-block works today)
  T2: call 2 cache_read_input_tokens > 0 (multi-block works)
  T3: call 2 cache_read_input_tokens > 0 (full multi-block works)

If T2 or T3 fail → fall back to single-block in BLOCK 3, structure
prompt to maximize the static prefix.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from conversation.llm_client import make_anthropic_client, resolve_model  # noqa: E402

# Cache requires ≥1024 tokens per block on Anthropic Direct, ~2048 on Bedrock.
# Use very padded text to ensure each block exceeds the threshold.
PADDING = "This is a static prompt block used to test prompt caching behavior on Bedrock. " * 80
# ~ 80 reps × 14 words = 1120 tokens, comfortably over the threshold.

STATIC_BLOCK_1 = f"BLOCK_1_STATIC: {PADDING}"
STATIC_BLOCK_2 = f"BLOCK_2_STATIC: {PADDING}"
STATIC_BLOCK_3 = f"BLOCK_3_STATIC: {PADDING}"


def call(client, model: str, blocks: list[dict]) -> dict:
    """Make a Bedrock call with the given content blocks. Return usage."""
    resp = client.messages.create(
        model=model,
        max_tokens=50,
        temperature=0.0,
        messages=[{"role": "user", "content": blocks}],
    )
    usage = resp.usage
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def test_n_blocks(client, model: str, n_cache_blocks: int) -> dict:
    """Run a test with N cached blocks + 1 variable block.

    Make 2 calls, return delta usage on call 2 (which should hit cache).
    """
    static_blocks = [STATIC_BLOCK_1, STATIC_BLOCK_2, STATIC_BLOCK_3][:n_cache_blocks]
    blocks_call_1: list[dict] = []
    for sb in static_blocks:
        blocks_call_1.append({
            "type": "text",
            "text": sb,
            "cache_control": {"type": "ephemeral"},
        })
    blocks_call_1.append({"type": "text", "text": "Variable text A. Reply with one short word."})

    blocks_call_2: list[dict] = []
    for sb in static_blocks:
        blocks_call_2.append({
            "type": "text",
            "text": sb,
            "cache_control": {"type": "ephemeral"},
        })
    blocks_call_2.append({"type": "text", "text": "Variable text B. Reply with one short word."})

    print(f"\n--- Test: {n_cache_blocks} cache_control blocks ---")
    print(f"  Call 1 (cold)...")
    u1 = call(client, model, blocks_call_1)
    print(f"    input={u1['input_tokens']}, cache_create={u1['cache_creation_input_tokens']}, cache_read={u1['cache_read_input_tokens']}")

    print(f"  Call 2 (warm — cache should hit)...")
    u2 = call(client, model, blocks_call_2)
    print(f"    input={u2['input_tokens']}, cache_create={u2['cache_creation_input_tokens']}, cache_read={u2['cache_read_input_tokens']}")

    success = u2['cache_read_input_tokens'] > 0
    expected_min_read = 1000 * n_cache_blocks  # rough — each block is ~1100 tokens
    print(f"  → cache_read_input_tokens on call 2: {u2['cache_read_input_tokens']}")
    print(f"  → expected: > {expected_min_read} (≥1 cached block read)")
    print(f"  → SUCCESS: {success}")
    return {"n_blocks": n_cache_blocks, "success": success, "cache_read_call2": u2['cache_read_input_tokens']}


def main():
    client = make_anthropic_client()
    # Use the Sonnet model the system uses for Teacher
    from config import cfg
    model = resolve_model(cfg.models.teacher)
    print(f"Model: {model}")

    results = []
    for n in (1, 2, 3):
        try:
            r = test_n_blocks(client, model, n)
            results.append(r)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            results.append({"n_blocks": n, "success": False, "error": str(e)})

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        mark = "✓" if r.get("success") else "✗"
        print(f"  {mark} {r['n_blocks']}-block cache: cache_read={r.get('cache_read_call2', 'N/A')}")

    # Decide BLOCK 3 strategy
    multi_works = all(r.get("success") for r in results if r["n_blocks"] >= 2)
    print()
    if multi_works:
        print("→ Bedrock supports multi-block cache_control.")
        print("→ BLOCK 3 strategy: ship the full 4-tier cache architecture.")
    else:
        print("→ Bedrock does NOT support multi-block cache_control reliably.")
        print("→ BLOCK 3 strategy: single-block, maximize static prefix.")


if __name__ == "__main__":
    main()
