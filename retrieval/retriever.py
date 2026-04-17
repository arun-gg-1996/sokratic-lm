"""
retrieval/retriever.py
----------------------
Clean hybrid retrieval pipeline:
1) Query embedding (text-embedding-3-large)
2) Qdrant dense search (domain filter only)
3) BM25 sparse search (stemmed query tokens)
4) RRF merge (k=60)
5) Expand to unique parent chunks
6) Cross-encoder reranking
7) Out-of-scope check (max CE < -3.0 -> [])
8) Return top-5 chunks
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sentence_transformers import CrossEncoder

from config import cfg
from ingestion.index import load_bm25, stem_tokenize

load_dotenv(".env")


class Retriever:
    def __init__(self, index_dir: str | None = None):
        self.index_dir = index_dir
        self.embed_model = cfg.models.embeddings
        self.collection = cfg.memory.kb_collection

        self.openai = OpenAI()
        self.qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
        self.bm25, self.bm25_props = load_bm25(cfg.paths.bm25_ot)
        self.cross_encoder = CrossEncoder(cfg.models.cross_encoder)

    def _embed_query(self, query: str) -> list[float]:
        resp = self.openai.embeddings.create(model=self.embed_model, input=[query])
        return resp.data[0].embedding

    def _qdrant_search(self, query_vector: list[float], top_k: int, domain: str) -> list[dict]:
        qfilter = Filter(must=[FieldCondition(key="domain", match=MatchValue(value=domain))])

        if hasattr(self.qdrant, "query_points"):
            resp = self.qdrant.query_points(
                collection_name=self.collection,
                query=query_vector,
                query_filter=qfilter,
                limit=top_k,
                with_payload=True,
            )
            points = list(getattr(resp, "points", []))
        else:
            points = self.qdrant.search(
                collection_name=self.collection,
                query_vector=query_vector,
                query_filter=qfilter,
                limit=top_k,
            )

        hits: list[dict] = []
        for rank, p in enumerate(points, start=1):
            payload = p.payload or {}
            pid = payload.get("proposition_id") or str(getattr(p, "id", rank))
            hits.append(
                {
                    "proposition_id": str(pid),
                    "text": payload.get("text", ""),
                    "parent_chunk_id": payload.get("parent_chunk_id", ""),
                    "parent_chunk_text": payload.get("parent_chunk_text", ""),
                    "chapter_num": payload.get("chapter_num", 0),
                    "chapter_title": payload.get("chapter_title", ""),
                    "section_num": payload.get("section_num", ""),
                    "section_title": payload.get("section_title", ""),
                    "subsection_title": payload.get("subsection_title", ""),
                    "page": payload.get("page", 0),
                    "element_type": payload.get("element_type", "paragraph"),
                    "domain": payload.get("domain", domain),
                    "image_filename": payload.get("image_filename", ""),
                    "_qdrant_rank": rank,
                    "_qdrant_score": float(getattr(p, "score", 0.0)),
                }
            )
        return hits

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        tokens = stem_tokenize(query)
        scores = self.bm25.get_scores(tokens)
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

        hits: list[dict] = []
        for rank, idx in enumerate(top_idx, start=1):
            payload = self.bm25_props[idx]
            pid = payload.get("proposition_id") or f"bm25-{idx}"
            hits.append(
                {
                    "proposition_id": str(pid),
                    "text": payload.get("text", ""),
                    "parent_chunk_id": payload.get("parent_chunk_id", ""),
                    "parent_chunk_text": payload.get("parent_chunk_text", ""),
                    "chapter_num": payload.get("chapter_num", 0),
                    "chapter_title": payload.get("chapter_title", ""),
                    "section_num": payload.get("section_num", ""),
                    "section_title": payload.get("section_title", ""),
                    "subsection_title": payload.get("subsection_title", ""),
                    "page": payload.get("page", 0),
                    "element_type": payload.get("element_type", "paragraph"),
                    "domain": payload.get("domain", "ot"),
                    "image_filename": payload.get("image_filename", ""),
                    "_bm25_rank": rank,
                    "_bm25_score": float(scores[idx]),
                }
            )
        return hits

    @staticmethod
    def _rrf_merge(list_a: list[dict], list_b: list[dict], k: int = 60) -> list[dict]:
        scores: dict[str, float] = {}
        merged_payload: dict[str, dict] = {}

        for rank, item in enumerate(list_a, start=1):
            pid = str(item.get("proposition_id") or f"a-{rank}")
            scores[pid] = scores.get(pid, 0.0) + (1.0 / (k + rank))
            merged_payload[pid] = dict(item)

        for rank, item in enumerate(list_b, start=1):
            pid = str(item.get("proposition_id") or f"b-{rank}")
            scores[pid] = scores.get(pid, 0.0) + (1.0 / (k + rank))
            if pid in merged_payload:
                if not merged_payload[pid].get("parent_chunk_text") and item.get("parent_chunk_text"):
                    merged_payload[pid]["parent_chunk_text"] = item.get("parent_chunk_text", "")
            else:
                merged_payload[pid] = dict(item)

        merged: list[dict] = []
        for pid in sorted(scores, key=lambda x: scores[x], reverse=True):
            item = dict(merged_payload[pid])
            item["rrf_score"] = float(scores[pid])
            merged.append(item)
        return merged

    @staticmethod
    def _expand_to_parent_chunks(merged_props: list[dict]) -> list[dict]:
        by_parent: dict[str, dict] = {}

        for prop in merged_props:
            parent_id = prop.get("parent_chunk_id")
            if not parent_id:
                continue

            prev = by_parent.get(parent_id)
            if prev is None or float(prop.get("rrf_score", 0.0)) > float(prev.get("rrf_score", 0.0)):
                by_parent[parent_id] = {
                    "chunk_id": parent_id,
                    "text": prop.get("parent_chunk_text", ""),
                    "rrf_score": float(prop.get("rrf_score", 0.0)),
                    "chapter_num": prop.get("chapter_num", 0),
                    "chapter_title": prop.get("chapter_title", ""),
                    "section_title": prop.get("section_title", ""),
                    "subsection_title": prop.get("subsection_title", ""),
                    "page": prop.get("page", 0),
                    "element_type": prop.get("element_type", "paragraph"),
                    "image_filename": prop.get("image_filename", ""),
                }

        parent_chunks = list(by_parent.values())
        parent_chunks.sort(key=lambda x: float(x.get("rrf_score", 0.0)), reverse=True)
        return parent_chunks

    def _cross_encoder_rerank(self, query: str, parent_chunks: list[dict]) -> list[dict]:
        if not parent_chunks:
            return []
        pairs = [(query, c.get("text", "")) for c in parent_chunks]
        ce_scores = self.cross_encoder.predict(pairs)

        ranked: list[dict] = []
        for chunk, score in zip(parent_chunks, ce_scores):
            row = dict(chunk)
            row["score"] = float(score)
            ranked.append(row)

        ranked.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        return ranked

    def clear_cache(self) -> None:
        # kept for compatibility with evaluation scripts
        return None

    def retrieve(self, query: str, domain: str = "ot") -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []

        q_top_k = int(cfg.retrieval.qdrant_top_k)
        b_top_k = int(cfg.retrieval.bm25_top_k)
        final_k = int(cfg.retrieval.top_chunks_final)

        query_vec = self._embed_query(query)
        qdrant_hits = self._qdrant_search(query_vec, top_k=q_top_k, domain=domain)
        bm25_hits = self._bm25_search(query, top_k=b_top_k)

        merged = self._rrf_merge(qdrant_hits, bm25_hits, k=int(cfg.retrieval.rrf_k))
        parent_chunks = self._expand_to_parent_chunks(merged)
        reranked = self._cross_encoder_rerank(query, parent_chunks)

        if not reranked:
            return []

        max_score = max(float(r.get("score", 0.0)) for r in reranked)
        if max_score < -3.0:
            return []

        final_results = reranked[:final_k]
        payload: list[dict[str, Any]] = []
        for r in final_results:
            payload.append(
                {
                    "chunk_id": r.get("chunk_id", ""),
                    "text": r.get("text", ""),
                    "score": float(r.get("score", 0.0)),
                    "chapter_num": r.get("chapter_num", 0),
                    "chapter_title": r.get("chapter_title", ""),
                    "section_title": r.get("section_title", ""),
                    "subsection_title": r.get("subsection_title", ""),
                    "page": r.get("page", 0),
                    "element_type": r.get("element_type", "paragraph"),
                    "image_filename": r.get("image_filename", ""),
                }
            )
        return payload
