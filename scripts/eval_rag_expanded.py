"""
scripts/eval_rag_expanded.py
------------------------------
Run the full RAG eval on data/eval/rag_qa_expanded.jsonl (231 queries across 5
categories) and report:
  - Hit@K, MRR — per category + overall
  - OOD precision (% of OOD queries that correctly return empty)
  - Latency p50 / p95

Ground-truth types:
  1. source_chunk_id present → exact chunk match
  2. expected_keyword present → any retrieved chunk contains the keyword
  3. question_type = "ood_negative" → correct iff no chunks returned

Usage:
  .venv/bin/python scripts/eval_rag_expanded.py [--save]
"""
import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.retriever import Retriever
from tools.mcp_tools import search_textbook


ROOT = Path(__file__).parent.parent
SRC = ROOT / "data/eval/rag_qa_expanded.jsonl"
OUT_DIR = ROOT / "data/eval"


def load_jsonl(path):
    return [json.loads(l) for l in open(path)]


def evaluate_row(chunks: list[dict], row: dict) -> dict:
    """Score one query. Returns dict with {hit, mrr_rank, category, ...}."""
    q_type = row.get("question_type", "factual")
    style = row.get("question_style", "original")

    # OOD negative: correct iff empty return
    if q_type == "ood_negative":
        ood_correct = len(chunks) == 0
        return {
            "hit": bool(ood_correct),
            "rank": 0 if ood_correct else -1,
            "category": "ood_negative",
            "style": style,
            "n_returned": len(chunks),
        }

    # Keyword match
    if row.get("expected_keyword"):
        kw = row["expected_keyword"].lower()
        for rank, c in enumerate(chunks, start=1):
            if kw in c.get("text", "").lower():
                return {"hit": True, "rank": rank, "category": style, "style": style}
        return {"hit": False, "rank": -1, "category": style, "style": style}

    # Chunk-id match (original + conversational + misspelling)
    target = row.get("source_chunk_id") or row.get("derived_from_chunk_id")
    if not target:
        return {"hit": False, "rank": -1, "category": style, "style": style}
    for rank, c in enumerate(chunks, start=1):
        if c.get("chunk_id") == target:
            return {"hit": True, "rank": rank, "category": style, "style": style}
    return {"hit": False, "rank": -1, "category": style, "style": style}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="Persist results JSON")
    ap.add_argument("--label", default="current", help="Label for saved results")
    args = ap.parse_args()

    retriever = Retriever()
    rows = load_jsonl(SRC)
    print(f"Loaded {len(rows)} queries.")

    per_cat = defaultdict(list)
    latencies = []

    for i, row in enumerate(rows, start=1):
        t0 = time.time()
        try:
            chunks = search_textbook(row["question"], retriever)
        except Exception as e:
            print(f"  err on {row['question'][:50]!r}: {e}")
            chunks = []
        ms = int((time.time() - t0) * 1000)
        latencies.append(ms)

        result = evaluate_row(chunks, row)
        per_cat[result["category"]].append(result)
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} done")

    # Aggregate
    print("\n" + "=" * 70)
    print(f"EVALUATION — {len(rows)} queries")
    print("=" * 70)

    overall_retrieval = []
    ood_results = []
    for cat, results in per_cat.items():
        if cat == "ood_negative":
            ood_results = results
        else:
            overall_retrieval.extend(results)

    def hit_at(results, k):
        return sum(1 for r in results if r["hit"] and 1 <= r["rank"] <= k) / max(len(results), 1)

    def mrr(results):
        reciprocal = [1.0 / r["rank"] for r in results if r["hit"] and r["rank"] > 0]
        return sum(reciprocal) / max(len(results), 1)

    print(f"\nRETRIEVAL metrics (excluding OOD), N = {len(overall_retrieval)}")
    for k in [1, 3, 5, 7]:
        h = hit_at(overall_retrieval, k)
        print(f"  Hit@{k}:  {h:.3f}  ({int(h*len(overall_retrieval))}/{len(overall_retrieval)})")
    print(f"  MRR:     {mrr(overall_retrieval):.3f}")

    print(f"\nOOD precision, N = {len(ood_results)}")
    ood_correct = sum(1 for r in ood_results if r["hit"])
    print(f"  correct_empty: {ood_correct}/{len(ood_results)} = {ood_correct/max(len(ood_results),1):.3f}")

    print(f"\nPer-category Hit@5:")
    for cat in sorted(per_cat.keys()):
        res = per_cat[cat]
        if cat == "ood_negative":
            h = sum(1 for r in res if r["hit"]) / max(len(res), 1)
            label = "correct_empty"
        else:
            h = hit_at(res, 5)
            label = "hit@5"
        print(f"  {cat:25} {label:>14}={h:.3f}  (N={len(res)})")

    print(f"\nLatency:")
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"  p50={p50} ms   p95={p95} ms   max={max(latencies)} ms")

    if args.save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        out_path = OUT_DIR / f"eval_results_{args.label}_{stamp}.json"
        payload = {
            "label": args.label,
            "timestamp": stamp,
            "n_queries": len(rows),
            "overall_retrieval_n": len(overall_retrieval),
            "overall": {
                f"hit_at_{k}": hit_at(overall_retrieval, k) for k in [1, 3, 5, 7]
            },
            "overall_mrr": mrr(overall_retrieval),
            "ood_precision": ood_correct / max(len(ood_results), 1),
            "per_category": {
                cat: {
                    "n": len(res),
                    "hit_at_5": (sum(1 for r in res if r["hit"]) / max(len(res), 1))
                    if cat == "ood_negative"
                    else hit_at(res, 5),
                }
                for cat, res in per_cat.items()
            },
            "latency_ms": {"p50": p50, "p95": p95, "max": max(latencies)},
        }
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
