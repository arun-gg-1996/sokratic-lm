"""
scripts/compare_topic_resolvers.py
──────────────────────────────────
Side-by-side comparison of the legacy 3-stage topic resolver
(retrieval/topic_matcher.TopicMatcher.match — RapidFuzz-driven) and the
new L9 single-Haiku-call resolver (retrieval/topic_mapper_llm.map_topic).

Goal: produce evidence to justify (or block) the dean.py wire-over in
track 2.3. The deciding question is "does L9 agree with the legacy
resolver on the cases it's tuned for, AND handle the cases where the
legacy resolver fails?"

Reports for each query:
  * legacy: tier + top match path + score
  * L9:     verdict + top match path + confidence + route_decision
  * agreement: same path / different path / one is empty

Usage:
  .venv/bin/python scripts/compare_topic_resolvers.py [--queries-file FILE.txt]
                                                       [--save report.json]

Default fixture set covers:
  * exact-match queries the legacy resolver should ace
  * abbreviations the legacy can't expand without aliases
  * vague queries that should surface multiple options
  * off-topic queries that should refuse
  * paraphrased questions where rank-vote tends to drift

Costs ~$0.10 + 6 cached calls × ~$0.04 = ~$0.30 for the default run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from conversation.llm_client import make_anthropic_client, resolve_model  # noqa: E402
from retrieval.topic_mapper_llm import map_topic, clear_caches  # noqa: E402
from retrieval.topic_matcher import get_topic_matcher  # noqa: E402

DEFAULT_QUERIES = [
    # Exact-match (legacy should ace these)
    "SA node",
    "rotator cuff",
    "what is glycolysis",
    "ATP and muscle contraction",
    # Abbreviations (legacy needs aliases; L9 should generalize)
    "ADH",
    "CN VII",
    "GFR",
    # Vague single-word (legacy fuzzy gives noise; L9 should refuse / borderline)
    "muscle",
    "joints",
    "brain",
    # Paraphrased questions where rank-vote drifted historically
    "What part of the heart starts the heartbeat?",
    "What are the structural and functional distinctions between the superior and inferior venae cavae?",
    # Clinical syndromes (need anatomy mapping)
    "wrist drop",
    "ulnar nerve injuries at the elbow",
    # Off-topic (both should refuse)
    "what is the capital of France",
    "vaping policy",
]


def normalize_legacy_path_to_canonical(legacy_path: str) -> str:
    """'Ch20|Section|Subsection' → 'Full Title > Section > Subsection' for
    side-by-side comparison with L9 output."""
    from memory.sqlite_store import normalize_subsection_path
    return normalize_subsection_path(legacy_path)


def run_legacy(query: str) -> dict:
    matcher = get_topic_matcher()
    t0 = time.time()
    try:
        result = matcher.match(query)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "elapsed_ms": int((time.time() - t0) * 1000)}
    elapsed_ms = int((time.time() - t0) * 1000)
    top = result.matches[0] if result.matches else None
    return {
        "tier": result.tier,
        "n_matches": len(result.matches),
        "top_path": normalize_legacy_path_to_canonical(top.path) if top else "",
        "top_path_legacy": top.path if top else "",
        "top_score": float(top.score) if top else 0.0,
        "elapsed_ms": elapsed_ms,
    }


def run_l9(query: str, *, client, model) -> dict:
    t0 = time.time()
    r = map_topic(query, client=client, model=model)
    elapsed_ms = int((time.time() - t0) * 1000)
    top = r.best_match()
    return {
        "verdict": r.verdict,
        "confidence": round(r.confidence, 3),
        "route": r.route_decision(),
        "n_matches": len(r.top_matches),
        "top_path": top.path if top else "",
        "top_confidence": round(top.confidence, 3) if top else 0.0,
        "rationale": top.rationale if top else "",
        "elapsed_ms": elapsed_ms,
        "tokens_in": r.input_tokens,
        "tokens_cache_create": r.cache_creation_tokens,
        "tokens_cache_read": r.cache_read_tokens,
        "tokens_out": r.output_tokens,
    }


def classify_agreement(legacy: dict, l9: dict) -> str:
    """Categorize the comparison: same / different / both refused / disagree on refuse."""
    legacy_path = legacy.get("top_path", "")
    l9_path = l9.get("top_path", "")
    legacy_refused = (
        legacy.get("tier") == "none"
        or legacy.get("top_score", 0) < 50
    )
    l9_refused = l9.get("route") == "refuse_with_starter_cards"

    if legacy_refused and l9_refused:
        return "both_refused"
    if legacy_refused and not l9_refused:
        return "legacy_refused_l9_resolved"
    if l9_refused and not legacy_refused:
        return "l9_refused_legacy_resolved"
    if legacy_path == l9_path and legacy_path:
        return "agree_same_path"
    return "disagree_different_paths"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries-file", type=Path, default=None,
                    help="Optional: file with one query per line (overrides default fixture).")
    ap.add_argument("--save", type=Path, default=None,
                    help="Save full JSON report to this path.")
    args = ap.parse_args()

    queries = (
        [q.strip() for q in args.queries_file.read_text().splitlines() if q.strip()]
        if args.queries_file
        else list(DEFAULT_QUERIES)
    )

    clear_caches()
    client = make_anthropic_client()
    model = resolve_model("claude-haiku-4-5-20251001")
    print(f"Comparing {len(queries)} queries against legacy + L9 (model={model})\n", flush=True)

    rows: list[dict] = []
    for q in queries:
        legacy = run_legacy(q)
        l9 = run_l9(q, client=client, model=model)
        agreement = classify_agreement(legacy, l9)
        rows.append({"query": q, "legacy": legacy, "l9": l9, "agreement": agreement})
        print(f"{q!r}", flush=True)
        print(f"  legacy:  tier={legacy.get('tier','?')} score={legacy.get('top_score',0):.0f} "
              f"path={legacy.get('top_path','(none)')[:80]!r}", flush=True)
        print(f"  L9:      {l9.get('verdict','?'):>10s} conf={l9.get('confidence',0):.2f} "
              f"route={l9.get('route','?'):<25s} path={l9.get('top_path','(none)')[:80]!r}", flush=True)
        print(f"  AGREEMENT: {agreement}", flush=True)
        print(flush=True)

    # Aggregate
    from collections import Counter
    agreement_counts = Counter(r["agreement"] for r in rows)
    print("=" * 70, flush=True)
    print("Aggregate agreement breakdown:", flush=True)
    for k, n in sorted(agreement_counts.items(), key=lambda x: -x[1]):
        pct = 100 * n / len(rows)
        print(f"  {n:3d} ({pct:5.1f}%)  {k}", flush=True)

    total_in = sum(r["l9"].get("tokens_in", 0) for r in rows)
    total_create = sum(r["l9"].get("tokens_cache_create", 0) for r in rows)
    total_read = sum(r["l9"].get("tokens_cache_read", 0) for r in rows)
    total_out = sum(r["l9"].get("tokens_out", 0) for r in rows)
    cost = (total_in + total_create * 1.25 + total_read * 0.10 + total_out * 5) / 1_000_000
    print(f"\nL9 token totals: in={total_in} cache_create={total_create} "
          f"cache_read={total_read} out={total_out}", flush=True)
    print(f"Approx Bedrock cost: ${cost:.4f} for {len(rows)} L9 calls", flush=True)

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps({
            "queries": queries,
            "model": model,
            "rows": rows,
            "agreement_counts": dict(agreement_counts),
            "cost_estimate": cost,
        }, indent=2))
        print(f"\nFull report saved to {args.save}", flush=True)


if __name__ == "__main__":
    main()
