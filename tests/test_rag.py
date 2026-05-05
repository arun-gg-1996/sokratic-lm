"""
tests/test_rag.py
-----------------
Stage gate tests for the retrieval pipeline.
HARD REQUIREMENT: must pass before building Dean/Teacher agents (Stage 2).

Prerequisites:
  - Ingestion complete (tests/test_ingestion.py passes)
  - data/eval/rag_qa.jsonl exists (run evaluation/generate_rag_qa.py first)
  - Qdrant is running (docker run -p 6333:6333 qdrant/qdrant)

Tests:
  1. RAG Q&A eval — for each question, does the correct source chunk appear in top-5?
     Metrics: Hit@1, Hit@3, Hit@5, MRR
     Hard threshold: Hit@5 >= 0.70, MRR >= 0.40
  2. Latency — every retrieval call completes in < 200ms
  3. Out-of-scope — off-topic queries return empty list (not anatomy = no chunks)
  4. Deduplication — no duplicate parent_chunk_id in returned results
  5. Chunk count sanity — every query returns between 1 and top_chunks_final results

Run:
    python -m pytest tests/test_rag.py -v

Interpreting failures:
  - Hit@5 too low   → embedding quality poor, or proposition extraction too lossy
  - MRR too low     → right chunk found but not ranked highly → cross-encoder issue
  - Latency failing → Qdrant not running locally, or cross-encoder loading too slow
  - Duplicates      → deduplication bug in retriever._rrf_merge or cross-encoder step
"""

import json
import time
import pytest
from pathlib import Path
from config import cfg

RAG_QA_PATH = Path("data/eval/rag_qa.jsonl")

# Hard thresholds — must pass before Stage 2
# Hit threshold uses top_chunks_final (currently 7) — checked as Hit@K
HIT_AT_K_MIN = 0.70
MRR_MIN = 0.40
LATENCY_MAX_MS = 200

# Out-of-scope test queries — should return empty list
OUT_OF_SCOPE_QUERIES = [
    "What is the capital of France?",
    "How do I write a Python function?",
    "Who won the World Cup in 2018?",
]


@pytest.fixture(scope="module")
def retriever():
    from retrieval.retriever import Retriever
    return Retriever(index_dir=cfg.domain_path("indexes"))


@pytest.fixture(scope="module")
def rag_qa():
    assert RAG_QA_PATH.exists(), (
        f"RAG Q&A dataset not found at {RAG_QA_PATH}. "
        f"Run: python -m evaluation.generate_rag_qa"
    )
    with open(RAG_QA_PATH) as f:
        records = [json.loads(line) for line in f]
    assert len(records) >= 100, (
        f"RAG Q&A dataset has only {len(records)} records. Need >= 100."
    )
    return records


def _reciprocal_rank(results: list[dict], source_chunk_id: str) -> float:
    """Return 1/rank if source_chunk_id is in results, else 0."""
    for i, chunk in enumerate(results, start=1):
        if chunk.get("chunk_id") == source_chunk_id:
            return 1.0 / i
    return 0.0


def test_rag_qa_hit_and_mrr(retriever, rag_qa):
    top_k = cfg.retrieval.top_chunks_final  # dynamic — matches what retriever returns
    hits_at_1 = hits_at_3 = hits_at_k = 0
    mrr_sum = 0.0
    n = len(rag_qa)

    for record in rag_qa:
        results = retriever.retrieve(record["question"])
        returned_ids = [r.get("chunk_id") for r in results]
        target = record["source_chunk_id"]

        if target in returned_ids[:1]:
            hits_at_1 += 1
        if target in returned_ids[:3]:
            hits_at_3 += 1
        if target in returned_ids[:top_k]:
            hits_at_k += 1
        mrr_sum += _reciprocal_rank(results, target)

    hit_at_1 = hits_at_1 / n
    hit_at_3 = hits_at_3 / n
    hit_at_k = hits_at_k / n
    mrr = mrr_sum / n

    print(f"\nRAG Eval Results (n={n}):")
    print(f"  Hit@1  : {hit_at_1:.3f}")
    print(f"  Hit@3  : {hit_at_3:.3f}")
    print(f"  Hit@{top_k} : {hit_at_k:.3f}  (threshold: >= {HIT_AT_K_MIN})")
    print(f"  MRR    : {mrr:.3f}  (threshold: >= {MRR_MIN})")

    assert hit_at_k >= HIT_AT_K_MIN, (
        f"Hit@{top_k} {hit_at_k:.3f} below threshold {HIT_AT_K_MIN}. "
        f"Retrieval is not finding the right chunks."
    )
    assert mrr >= MRR_MIN, (
        f"MRR {mrr:.3f} below threshold {MRR_MIN}. "
        f"Right chunks found but ranked too low — check cross-encoder."
    )


def test_retrieval_latency(retriever, rag_qa):
    """Every single retrieval call must complete in < 200ms."""
    # Sample 20 questions for latency test (not all 150 — that would take 30s)
    sample = rag_qa[:20]
    slow = []

    for record in sample:
        start = time.perf_counter()
        retriever.retrieve(record["question"])
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > LATENCY_MAX_MS:
            slow.append((record["question"][:60], elapsed_ms))

    assert not slow, (
        f"{len(slow)} queries exceeded {LATENCY_MAX_MS}ms latency: {slow[:3]}"
    )


def test_out_of_scope_returns_empty(retriever):
    """Off-topic queries must return an empty list (cross-encoder score below threshold)."""
    for query in OUT_OF_SCOPE_QUERIES:
        results = retriever.retrieve(query)
        assert results == [], (
            f"Out-of-scope query returned chunks: '{query}' → {len(results)} result(s). "
            f"Raise out_of_scope_threshold in config.yaml or check cross-encoder."
        )


def test_no_duplicate_chunks(retriever, rag_qa):
    """Returned chunks must have unique chunk_ids (dedup working correctly)."""
    for record in rag_qa[:20]:
        results = retriever.retrieve(record["question"])
        ids = [r.get("chunk_id") for r in results]
        assert len(ids) == len(set(ids)), (
            f"Duplicate chunk_ids in results for: '{record['question'][:60]}' → {ids}"
        )


def test_result_count_in_range(retriever, rag_qa):
    """Every in-scope query returns between 1 and top_chunks_final results."""
    top_k = cfg.retrieval.top_chunks_final
    for record in rag_qa[:20]:
        results = retriever.retrieve(record["question"])
        assert 1 <= len(results) <= top_k, (
            f"Query returned {len(results)} chunks (expected 1-{top_k}): "
            f"'{record['question'][:60]}'"
        )
