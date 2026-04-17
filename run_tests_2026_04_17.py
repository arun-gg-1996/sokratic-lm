"""
Temporary test script for split-query preprocessing validation.
Run from: /Users/nidhirajani/Desktop/sokratic-lm
Command:  TRANSFORMERS_OFFLINE=1 python run_tests_2026_04_17.py
"""

import json
import sys

sys.path.insert(0, ".")

from retrieval.retriever import Retriever

r = Retriever()

# ── TEST 1: Morphological variants ──────────────────────────────────────────
print("\n=== TEST 1 — Morphological variants ===")
t1_queries = [
    "What are the fascicles?",
    "What are fasciculi?",
    "What is a fascicle?",
]
for q in t1_queries:
    results = r.retrieve(q)
    print(f"{'✅' if results else '❌'} {q!r}: {len(results)} results")

# ── TEST 2: Informal language ────────────────────────────────────────────────
print("\n=== TEST 2 — Informal language ===")
t2_queries = [
    "that triangle muscle on top of shoulder",
    "nerve that gets hurt when you dislocate shoulder",
    "muscle that lets you raise your arm sideways",
]
for q in t2_queries:
    results = r.retrieve(q)
    print(f"{'✅' if results else '❌'} {q!r}: {len(results)} results")

# ── TEST 3: No semantic drift ────────────────────────────────────────────────
print("\n=== TEST 3 — No semantic drift (critical) ===")
r1 = r.retrieve("fasciculus muscle bundle")
r2 = r.retrieve("fasciculus gracilis spinal cord")
ch1 = set(x["chapter_num"] for x in r1)
ch2 = set(x["chapter_num"] for x in r2)
print(f"Muscle query chapters:     {sorted(ch1)}  (expected Ch10/11)")
print(f"Nerve/spinal query chapters: {sorted(ch2)}  (expected Ch13/14)")
drift_ok = not (ch1 & ch2) or True  # warn if overlap
if ch1 & ch2:
    print(f"⚠️  Chapter overlap detected: {ch1 & ch2} — possible drift")
else:
    print("✅ No chapter overlap — no semantic drift")

# ── TEST 4: Out-of-scope ─────────────────────────────────────────────────────
print("\n=== TEST 4 — Out-of-scope still blocked ===")
t4_queries = [
    "what is the capital of France",
    "how do I write Python code",
]
all_oos_pass = True
for q in t4_queries:
    results = r.retrieve(q)
    ok = results == []
    all_oos_pass = all_oos_pass and ok
    print(f"{'✅' if ok else '❌'} {q!r}: {results if not ok else '[]'}")

# ── TEST 5: 100 Q&A evaluation ───────────────────────────────────────────────
print("\n=== TEST 5 — 100 Q&A evaluation ===")
with open("data/eval/rag_qa.jsonl") as f:
    qa_pairs = [json.loads(line) for line in f if line.strip()]

hits1 = hits3 = hits5 = 0
rr_total = 0.0
no_result_count = 0
total = len(qa_pairs)

for item in qa_pairs:
    query = item["question"]
    gold_chunk_id = item.get("source_chunk_id", item.get("chunk_id", ""))
    r.clear_cache()

    results = r.retrieve(query)

    if not results:
        no_result_count += 1
        continue

    retrieved_ids = [res.get("chunk_id", "") for res in results]
    found_at = None
    for rank, cid in enumerate(retrieved_ids, start=1):
        if cid == gold_chunk_id:
            found_at = rank
            break

    if found_at is not None:
        if found_at <= 1:
            hits1 += 1
        if found_at <= 3:
            hits3 += 1
        if found_at <= 5:
            hits5 += 1
        rr_total += 1.0 / found_at

hit_at_1 = hits1 / total
hit_at_3 = hits3 / total
hit_at_5 = hits5 / total
mrr = rr_total / total

print(f"Total queries:    {total}")
print(f"No result:        {no_result_count}  (was 42, target <20)")
print(f"Hit@1:            {hit_at_1:.3f}")
print(f"Hit@3:            {hit_at_3:.3f}")
print(f"Hit@5:            {hit_at_5:.3f}  (was 0.52, target ≥0.70)")
print(f"MRR:              {mrr:.3f}  (was 0.493, target ≥0.40)")
print()
if hit_at_5 >= 0.70:
    print("✅ Gate 2 PASSED — Hit@5 ≥ 0.70")
else:
    print(f"❌ Gate 2 NOT YET PASSED — Hit@5 = {hit_at_5:.3f} (need 0.70)")

print("\n=== SUMMARY ===")
print(f"Test 1 (morphological): see above")
print(f"Test 2 (informal):      see above")
print(f"Test 3 (no drift):      see above")
print(f"Test 4 (out-of-scope):  {'PASS' if all_oos_pass else 'FAIL'}")
print(f"Test 5 (eval):          Hit@5={hit_at_5:.3f}  MRR={mrr:.3f}  no_result={no_result_count}")
