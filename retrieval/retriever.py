"""
retrieval/retriever.py
----------------------
Hybrid retrieval pipeline. Called at query time (every student message).

Full flow:
  1. Embed query with text-embedding-3-small
  2. Qdrant top-k dense search (filtered by domain) + BM25 top-k keyword search run in parallel
  3. Merge results with Reciprocal Rank Fusion (RRF, k=60)
  4. Deduplicate: multiple propositions may share a parent_chunk_id —
     keep only the highest-ranked proposition per parent chunk
     (parent_chunk_text is in the Qdrant payload — no separate lookup needed)
  5. Cross-encoder (ms-marco-MiniLM-L-6-v2) scores (query, parent_chunk) pairs
  6. If max cross-encoder score < out_of_scope_threshold → return empty list (out of scope)
  7. Return top_chunks_final chunks sorted by cross-encoder score, each with metadata

Target latency: < 200ms total.

All thresholds and k values come from cfg.retrieval.
No FAISS — Qdrant handles all dense vector search.
"""

from config import cfg


class Retriever:
    def __init__(self, index_dir: str):
        """
        Load FAISS index, BM25, metadata, and cross-encoder from disk.

        Args:
            index_dir: Directory containing faiss.index, bm25.pkl, index_meta.jsonl
        """
        # TODO: call ingestion.index.load_indexes(index_dir)
        # TODO: load cross-encoder with sentence_transformers.CrossEncoder
        raise NotImplementedError

    def retrieve(self, query: str) -> list[dict]:
        """
        Run the full hybrid retrieval pipeline for a student query.

        Args:
            query: The student's message or question.

        Returns:
            List of up to top_chunks_final chunk dicts, each:
            {
              "text": str,
              "chapter_title": str,
              "section_title": str,
              "subsection_title": str | None,
              "page": int,
              "score": float   <- cross-encoder score
            }
            Returns [] if all scores are below out_of_scope_threshold.
        """
        # TODO: embed query
        # TODO: faiss search (top-k)
        # TODO: bm25 search (top-k)
        # TODO: rrf_merge(faiss_results, bm25_results)
        # TODO: expand propositions to parent chunks
        # TODO: deduplicate by parent_chunk_id
        # TODO: cross-encoder rerank
        # TODO: out-of-scope check
        raise NotImplementedError

    def _rrf_merge(self, list_a: list, list_b: list, k: int = 60) -> list:
        """
        Merge two ranked lists using Reciprocal Rank Fusion.
        Score for item i in list of rank r = 1 / (k + r).
        Items appearing in both lists get scores summed.
        """
        # TODO: implement RRF
        raise NotImplementedError
