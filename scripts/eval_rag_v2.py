"""
scripts/eval_rag_v2.py
-----------------------
A/B test: run the same eval set under v2 (cleaner: no alias dict, HyDE fires
broadly) to compare against the v1.5 baseline.

This script runs the eval TWICE — once with aliases off + HyDE broad (v2), once
with aliases on + HyDE gated (v1.5) — using the same pipeline but toggling
config keys at runtime.

Usage:
  .venv/bin/python scripts/eval_rag_v2.py
"""
import json
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.retriever import Retriever, _HYDE_CACHE
from config import cfg

ROOT = Path(__file__).parent.parent
SRC = ROOT / "data/eval/rag_qa_expanded.jsonl"
OUT_DIR = ROOT / "data/eval"


def load_jsonl(path):
    return [json.loads(l) for l in open(path)]


def score_row(chunks, row):
    q_type = row.get("question_type", "factual")
    style = row.get("question_style", "original")
    if q_type == "ood_negative":
        return {"hit": len(chunks) == 0, "rank": 0, "category": "ood_negative", "style": style}
    if row.get("expected_keyword"):
        kw = row["expected_keyword"].lower()
        for i, c in enumerate(chunks, 1):
            if kw in c.get("text", "").lower():
                return {"hit": True, "rank": i, "category": style, "style": style}
        return {"hit": False, "rank": -1, "category": style, "style": style}
    target = row.get("source_chunk_id") or row.get("derived_from_chunk_id")
    for i, c in enumerate(chunks, 1):
        if c.get("chunk_id") == target:
            return {"hit": True, "rank": i, "category": style, "style": style}
    return {"hit": False, "rank": -1, "category": style, "style": style}


def run_one_config(label: str, rows: list, retriever: Retriever) -> dict:
    per_cat = defaultdict(list)
    latencies = []
    _HYDE_CACHE.clear()
    for i, row in enumerate(rows, 1):
        t0 = time.time()
        try:
            chunks = retriever.retrieve(row["question"])
        except Exception as e:
            chunks = []
        latencies.append(int((time.time() - t0) * 1000))
        per_cat[score_row(chunks, row)["category"]].append(score_row(chunks, row))
        if i % 50 == 0:
            print(f"  [{label}] {i}/{len(rows)}")

    overall = [r for cat, res in per_cat.items() if cat != "ood_negative" for r in res]
    ood = per_cat.get("ood_negative", [])

    def hit_at(res, k):
        return sum(1 for r in res if r["hit"] and 1 <= r["rank"] <= k) / max(len(res), 1)

    def mrr(res):
        return sum(1.0 / r["rank"] for r in res if r["hit"] and r["rank"] > 0) / max(len(res), 1)

    return {
        "label": label,
        "n_queries": len(rows),
        "retrieval_n": len(overall),
        "hit_at_1": hit_at(overall, 1),
        "hit_at_3": hit_at(overall, 3),
        "hit_at_5": hit_at(overall, 5),
        "hit_at_7": hit_at(overall, 7),
        "mrr": mrr(overall),
        "ood_correct_empty": sum(1 for r in ood if r["hit"]) / max(len(ood), 1),
        "per_category_hit_at_5": {
            cat: (sum(1 for r in res if r["hit"]) / max(len(res), 1))
            if cat == "ood_negative"
            else hit_at(res, 5)
            for cat, res in per_cat.items()
        },
        "per_category_n": {cat: len(res) for cat, res in per_cat.items()},
        "latency_p50": sorted(latencies)[len(latencies) // 2],
        "latency_p95": sorted(latencies)[int(len(latencies) * 0.95)],
    }


def main():
    rows = load_jsonl(SRC)
    retriever = Retriever()
    print(f"Loaded {len(rows)} queries. Running A/B eval...\n")

    # --- Config A: v1.5 (aliases on, HyDE gated at 0.65) ---
    cfg.retrieval.aliases_enabled = True
    cfg.retrieval.hyde_weak_cosine_threshold = 0.65
    print("=== V1.5: aliases ON, HyDE gate=0.65 ===")
    t = time.time()
    v15 = run_one_config("v1.5", rows, retriever)
    v15["total_secs"] = int(time.time() - t)

    # --- Config B: v2 (aliases off, HyDE fires on cos < 0.80) ---
    cfg.retrieval.aliases_enabled = False
    cfg.retrieval.hyde_weak_cosine_threshold = 0.80
    print("\n=== V2: aliases OFF, HyDE gate=0.80 (broader) ===")
    t = time.time()
    v2 = run_one_config("v2", rows, retriever)
    v2["total_secs"] = int(time.time() - t)

    # --- Compare side-by-side ---
    print("\n" + "=" * 80)
    print("COMPARISON")
    print("=" * 80)
    print(f"\n{'metric':30} {'v1.5 (dict+gated HyDE)':>25} {'v2 (no-dict, broad HyDE)':>28}")
    for metric in ["retrieval_n", "hit_at_1", "hit_at_3", "hit_at_5", "hit_at_7",
                   "mrr", "ood_correct_empty", "latency_p50", "latency_p95", "total_secs"]:
        a, b = v15[metric], v2[metric]
        fmt = "{:.3f}" if isinstance(a, float) else "{}"
        print(f"  {metric:28} {fmt.format(a):>25} {fmt.format(b):>28}")

    print(f"\n{'category':30} {'v1.5 hit@5':>14} {'v2 hit@5':>12}  (N)")
    all_cats = sorted(set(v15["per_category_hit_at_5"]) | set(v2["per_category_hit_at_5"]))
    for c in all_cats:
        a = v15["per_category_hit_at_5"].get(c, 0)
        b = v2["per_category_hit_at_5"].get(c, 0)
        n = v15["per_category_n"].get(c, v2["per_category_n"].get(c, 0))
        delta = b - a
        arrow = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "·")
        print(f"  {c:28} {a:>14.3f} {b:>12.3f}  ({n:>3})  {arrow} {delta:+.3f}")

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = OUT_DIR / f"eval_ab_{stamp}.json"
    with open(out, "w") as f:
        json.dump({"v1_5": v15, "v2": v2}, f, indent=2)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
