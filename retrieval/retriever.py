"""
retrieval/retriever.py
----------------------
Clean hybrid retrieval pipeline:
1) Query embedding (text-embedding-3-large)
2) Qdrant dense search (domain filter only) — original query
3) BM25 sparse search — preprocessed query (normalized+stemmed+variants)
4) RRF merge (k=60)
5) Expand to unique parent chunks
6) Cross-encoder reranking — original query (safety net)
7) Out-of-scope check (max CE < -3.0 -> [])
8) Return top-5 chunks
"""

from __future__ import annotations

import re
from typing import Any

from dotenv import load_dotenv
from nltk.stem import PorterStemmer
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sentence_transformers import CrossEncoder

from config import cfg
from ingestion.index import load_bm25, stem_tokenize

load_dotenv(".env")

_HYDE_CACHE: dict[str, str] = {}  # process-local cache: query → hyde rewrite

_STEMMER = PorterStemmer()


def preprocess_for_bm25(query: str) -> list[str]:
    """
    Query-side BM25 tokenization — symmetric with the corpus.

    The BM25 corpus is tokenized with `ingestion.index.stem_tokenize`
    (lowercase + alnum + Porter stem). Using a different tokenizer on
    the query side causes the majority of query tokens to never match
    anything (surface forms are not in the corpus; stop-word removal
    and plural expansion were only applied query-side, creating
    further asymmetry). We now use the exact same tokenizer.
    """
    return stem_tokenize(query)


class Retriever:
    def __init__(self, index_dir: str | None = None):
        self.index_dir = index_dir
        self.embed_model = cfg.models.embeddings
        self.collection = getattr(getattr(cfg, "domain", object()), "kb_collection", cfg.memory.kb_collection)
        self.default_domain = getattr(getattr(cfg, "domain", object()), "retrieval_domain", "")
        # Domain-aware BM25 index path (falls back to cfg.paths.bm25_ot only if dynamic path missing).
        domain_key = (self.default_domain or "").strip().lower()
        dynamic_bm25_attr = f"bm25_{domain_key}" if domain_key else ""
        bm25_path = ""
        if dynamic_bm25_attr and hasattr(cfg.paths, dynamic_bm25_attr):
            bm25_path = getattr(cfg.paths, dynamic_bm25_attr)
        elif hasattr(cfg.paths, "bm25_ot"):
            bm25_path = cfg.paths.bm25_ot

        self.openai = OpenAI()
        self.qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
        self._validate_embedding_dimension()
        self.bm25, self.bm25_props = load_bm25(bm25_path)
        self.cross_encoder = CrossEncoder(cfg.models.cross_encoder)

    def _validate_embedding_dimension(self) -> None:
        # Strict contract: this codebase uses one embedding model for Qdrant.
        required_model = "text-embedding-3-large"
        if (self.embed_model or "").strip() != required_model:
            raise ValueError(
                f"Invalid embedding model '{self.embed_model}'. "
                f"Expected '{required_model}' to match indexed Qdrant vectors."
            )

        expected_dim = int(cfg.qdrant.vector_size)
        info = self.qdrant.get_collection(self.collection)
        vectors_cfg = getattr(getattr(info, "config", None), "params", None)
        vectors = getattr(vectors_cfg, "vectors", None)
        actual_dim = getattr(vectors, "size", None)
        if actual_dim is None:
            raise ValueError(
                f"Could not read vector size for Qdrant collection '{self.collection}'."
            )
        if int(actual_dim) != int(expected_dim):
            raise ValueError(
                f"Embedding/Qdrant dimension mismatch for collection '{self.collection}': "
                f"expected {expected_dim}, but collection uses {actual_dim}. "
                "Reindex collection or fix configuration."
            )

    def _embed_query(self, query: str) -> list[float]:
        resp = self.openai.embeddings.create(model=self.embed_model, input=[query])
        return resp.data[0].embedding

    def _qdrant_search(self, query_vector: list[float], top_k: int, domain: str) -> list[dict]:
        qfilter = Filter(must=[FieldCondition(key="domain", match=MatchValue(value=domain))])
        resp = self.qdrant.query_points(
            collection_name=self.collection,
            query=query_vector,
            query_filter=qfilter,
            limit=top_k,
            with_payload=True,
        )
        points = list(getattr(resp, "points", []))

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
                    "_qdrant_score": float(p.score or 0.0),
                }
            )
        return hits

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        tokens = preprocess_for_bm25(query)
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
                    "domain": payload.get("domain", self.default_domain or ""),
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
        """
        Aggregate proposition-level RRF scores up to their parent chunks by
        SUMMING the scores of all propositions belonging to the same parent.

        Rationale: a parent chunk with ten hitting propositions carries far more
        evidence than a parent chunk with one. Prior implementation took the
        `max`, which threw that signal away and was a primary cause of retrieval
        misses on canonical queries.
        """
        by_parent: dict[str, dict] = {}
        score_sum: dict[str, float] = {}
        hit_count: dict[str, int] = {}

        for prop in merged_props:
            parent_id = prop.get("parent_chunk_id")
            if not parent_id:
                continue
            rrf = float(prop.get("rrf_score", 0.0) or 0.0)
            score_sum[parent_id] = score_sum.get(parent_id, 0.0) + rrf
            hit_count[parent_id] = hit_count.get(parent_id, 0) + 1

            # Keep the first-seen parent metadata (all propositions under the
            # same parent share identical metadata anyway).
            if parent_id not in by_parent:
                by_parent[parent_id] = {
                    "chunk_id": parent_id,
                    "text": prop.get("parent_chunk_text", ""),
                    "chapter_num": prop.get("chapter_num", 0),
                    "chapter_title": prop.get("chapter_title", ""),
                    "section_title": prop.get("section_title", ""),
                    "subsection_title": prop.get("subsection_title", ""),
                    "page": prop.get("page", 0),
                    "element_type": prop.get("element_type", "paragraph"),
                    "image_filename": prop.get("image_filename", ""),
                }

        parent_chunks: list[dict] = []
        for parent_id, meta in by_parent.items():
            row = dict(meta)
            row["rrf_score"] = score_sum[parent_id]
            row["hit_count"] = hit_count[parent_id]
            parent_chunks.append(row)

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

    @staticmethod
    def _is_weak_retrieval(qdrant_hits: list[dict], bm25_hits: list[dict], query: str = "") -> bool:
        """
        Decide whether the original-query retrieval is weak enough that HyDE
        rescue is worth the latency.

        Signal used: max dense cosine over the top-20 Qdrant hits. When cosine
        is below the `hyde_weak_cosine_threshold`, the question is likely in a
        different linguistic register from the corpus (conversational vs
        textbook) — exactly the case HyDE was designed to handle.

        We deliberately do NOT use BM25 here: high BM25 on function-word
        overlap can suppress HyDE on queries that genuinely need it (e.g. a
        question about "the deltoid muscle" — "muscle" matches everywhere but
        the correct "axillary nerve" chunk ranks poorly).

        We do NOT use CE either — running CE requires the full pipeline, which
        defeats the purpose of a cheap pre-gate.
        """
        max_cosine = max((float(h.get("_qdrant_score", 0.0)) for h in qdrant_hits), default=0.0)
        weak_cos = float(getattr(cfg.retrieval, "hyde_weak_cosine_threshold", 0.65))
        return max_cosine < weak_cos

    def _hyde_rewrite(self, query: str) -> str:
        """
        Ask a small LLM to rewrite the student's question as a 2-3 sentence
        hypothetical textbook passage. Cached per-query to avoid duplicate cost
        on anchor-lock + hint-plan calls within the same session.

        Returns empty string on failure so callers can fall through to pure
        original-query retrieval.
        """
        q_key = query.strip().lower()
        if q_key in _HYDE_CACHE:
            return _HYDE_CACHE[q_key]

        try:
            import anthropic  # local import — keeps Retriever construction lightweight
            client = anthropic.Anthropic()
            model = getattr(getattr(cfg, "models", object()), "summarizer", None) \
                or getattr(getattr(cfg, "models", object()), "teacher", "claude-haiku-4-5-20251001")
            prompt_tmpl = getattr(getattr(cfg, "prompts", object()), "hyde_reformulate", "")
            if not prompt_tmpl:
                return ""
            domain_short = getattr(getattr(cfg, "domain", object()), "short", "the subject")
            prompt = prompt_tmpl.format(domain_short=domain_short, query=query)
            resp = client.messages.create(
                model=model,
                max_tokens=220,
                messages=[{"role": "user", "content": prompt}],
            )
            text = (resp.content[0].text or "").strip() if resp.content else ""
            if text:
                _HYDE_CACHE[q_key] = text
            return text
        except Exception:
            # Fail-silent: if HyDE fails, fall back to original-only retrieval.
            return ""

    @staticmethod
    def _dedupe_hits_keep_best_rank(list_a: list[dict], list_b: list[dict]) -> list[dict]:
        """
        Combine two Qdrant hit lists (from original + HyDE queries) keeping
        the best rank for each proposition_id. Preserves ranks so downstream
        RRF merge still sees rank-ordered hits from the dense side.
        """
        seen: dict[str, dict] = {}
        for src in (list_a, list_b):
            for h in src:
                pid = str(h.get("proposition_id") or "")
                if not pid:
                    continue
                if pid not in seen:
                    seen[pid] = dict(h)
                else:
                    if int(h.get("_qdrant_rank", 999999)) < int(seen[pid].get("_qdrant_rank", 999999)):
                        seen[pid]["_qdrant_rank"] = h.get("_qdrant_rank")
                        seen[pid]["_qdrant_score"] = max(
                            float(seen[pid].get("_qdrant_score", 0.0)),
                            float(h.get("_qdrant_score", 0.0)),
                        )
        combined = sorted(seen.values(), key=lambda x: int(x.get("_qdrant_rank", 999999)))
        # Re-rank after dedup so downstream RRF sees a contiguous 1..N ranking.
        for rank, h in enumerate(combined, start=1):
            h["_qdrant_rank"] = rank
        return combined

    @staticmethod
    def _apply_query_aliases(query: str) -> str:
        """
        Word-boundary substring expansion of configured query aliases.

        For each (alias → expansion) in cfg.query_aliases, if the alias appears
        as a word-bounded substring in the query (case-insensitive), append the
        expansion to the query. We never REPLACE — the student's original
        phrasing is preserved so BM25 / embedder can still match on it directly.

        Example: "CN VII palsy" → "CN VII palsy (facial nerve)"
        """
        # Runtime kill-switch: config `retrieval.aliases_enabled=false` disables
        # this without editing the dictionary. Used in v2 ablation testing.
        if not bool(getattr(cfg.retrieval, "aliases_enabled", True)):
            return query
        aliases = getattr(cfg, "query_aliases", {}) or {}
        if not aliases:
            return query
        lowered = query.lower()
        expansions: list[str] = []
        for alias, expansion in aliases.items():
            alias_s = str(alias).strip().lower()
            if not alias_s:
                continue
            if re.search(rf"\b{re.escape(alias_s)}\b", lowered):
                expansions.append(str(expansion))
        if not expansions:
            return query
        # Deduplicate while preserving order.
        seen: set[str] = set()
        uniq: list[str] = []
        for e in expansions:
            if e not in seen:
                seen.add(e)
                uniq.append(e)
        return f"{query} ({' '.join(uniq)})"

    @staticmethod
    def _is_in_scope(qdrant_hits: list[dict], bm25_hits: list[dict], reranked: list[dict]) -> bool:
        """
        Multi-signal in-scope check (domain-agnostic).

        Returns True if the query has at least one *semantic* signal indicating
        the corpus contains relevant content. A query is OOD only when BOTH of
        the semantic signals fail simultaneously:
          - Dense cosine similarity (OpenAI text-embedding-3-large).
          - Cross-encoder score after rerank.

        Why not include BM25 in the scope decision:
        Empirical calibration shows BM25 lets through OOD queries with high
        scores (e.g. "best pizza in buffalo" → BM25=9.6) because common function
        words ("best", "in", "is") overlap the corpus. BM25 is still useful for
        *ranking* among candidates, but is not a reliable scope signal.

        Both semantic signals failing (low cosine AND low CE) is a robust OOD
        indicator — a query that has neither semantic closeness to any chunk
        nor reranker agreement is genuinely off-topic.
        """
        max_cosine = max((float(h.get("_qdrant_score", 0.0)) for h in qdrant_hits), default=0.0)
        max_ce = max((float(r.get("score", 0.0)) for r in reranked), default=float("-inf"))

        cos_thr = float(getattr(cfg.retrieval, "ood_cosine_threshold", 0.30))
        ce_thr = float(getattr(cfg.retrieval, "out_of_scope_threshold", -5.0))

        return (max_cosine >= cos_thr) or (max_ce >= ce_thr)

    def clear_cache(self) -> None:
        # kept for compatibility with evaluation scripts
        return None

    def retrieve(self, query: str, domain: str | None = None, top_k: int | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        domain = (domain or self.default_domain or "").strip()
        if not domain:
            raise ValueError(
                "Retriever.retrieve: empty domain. Set cfg.domain.retrieval_domain "
                "(e.g., 'anatomy') or pass domain=... explicitly."
            )

        # Expand domain-configurable query aliases (additive — original tokens
        # preserved alongside the alias expansion).
        expanded_query = self._apply_query_aliases(query)

        q_top_k = int(cfg.retrieval.qdrant_top_k)
        b_top_k = int(cfg.retrieval.bm25_top_k)
        final_k = int(top_k) if top_k is not None else int(cfg.retrieval.top_chunks_final)
        rrf_k = int(cfg.retrieval.rrf_k)

        # --- Stage 1: original query (fast path, no LLM) -------------------
        orig_vec = self._embed_query(expanded_query)
        orig_qdrant = self._qdrant_search(orig_vec, top_k=q_top_k, domain=domain)
        orig_bm25 = self._bm25_search(expanded_query, top_k=b_top_k)

        # --- OOD short-circuit: if the ORIGINAL retrieval is so weak on the
        # primary scope signal (cosine) that the query is almost certainly
        # off-topic, return empty WITHOUT running HyDE. This prevents HyDE
        # from "rescuing" genuinely OOD queries by hallucinating biomedical
        # text that then matches chunks in the anatomy corpus.
        orig_top_cos = max((float(h.get("_qdrant_score", 0.0)) for h in orig_qdrant), default=0.0)
        ood_cos_floor = float(getattr(cfg.retrieval, "ood_cosine_threshold", 0.30))
        if orig_top_cos < ood_cos_floor:
            # Let the standard post-rerank OOD check make the final call (it
            # may still rescue via CE confidence), but DO NOT fire HyDE.
            use_hyde = False
        else:
            use_hyde = bool(getattr(cfg.retrieval, "hyde_enabled", True))

        # --- Stage 2: HyDE rescue (only fires on moderately-weak IN-SCOPE queries) ---
        # Design: original-first, HyDE as rescue. If the original is strong,
        # we never pay HyDE's latency. If it's weak BUT in-scope, we run HyDE
        # in addition (not replacing) and UNION the candidate pools before
        # rerank. BM25 always uses the original query — HyDE's hypothetical
        # text is used only for the dense side.
        hyde_qdrant: list[dict] = []
        if use_hyde and self._is_weak_retrieval(orig_qdrant, orig_bm25, query=expanded_query):
            hyde_text = self._hyde_rewrite(query)
            if hyde_text:
                hyde_vec = self._embed_query(hyde_text)
                hyde_qdrant = self._qdrant_search(hyde_vec, top_k=q_top_k, domain=domain)

        # Merge Qdrant hit lists from both stages (dedupe by proposition_id).
        merged_qdrant = self._dedupe_hits_keep_best_rank(orig_qdrant, hyde_qdrant)

        merged = self._rrf_merge(merged_qdrant, orig_bm25, k=rrf_k)
        parent_chunks = self._expand_to_parent_chunks(merged)
        # CE reranks against the alias-expanded query (`expanded_query`), not
        # the HyDE rewrite. The alias expansion is deterministic and high-
        # precision (curated human-written mappings); the HyDE rewrite is LLM
        # output that may introduce paraphrased terminology. So keep the alias
        # signal at rerank time, but not the HyDE signal.
        reranked = self._cross_encoder_rerank(expanded_query, parent_chunks)

        if not reranked:
            return []

        if not self._is_in_scope(merged_qdrant, orig_bm25, reranked):
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


class MockRetriever:
    """
    Lightweight fallback retriever for local dev when Qdrant/BM25/embeddings
    are unavailable. Keeps conversation flow functional but does not represent
    real RAG behavior.
    """

    def __init__(self):
        domain = getattr(cfg, "domain", object())
        domain_name = getattr(domain, "name", "the subject")
        domain_short = getattr(domain, "short", "subject")
        example_topic = getattr(domain, "example_topic_specific", "core concept")
        example_answer = getattr(domain, "example_answer", example_topic)
        self._chunks = [
            {
                "chunk_id": "mock-001",
                "text": (
                    f"This is a fallback retrieval chunk for {domain_name}. "
                    f"The key focus concept in this topic is {example_topic}, and the "
                    f"core answer term is {example_answer}."
                ),
                "score": 0.92,
                "chapter_num": 1,
                "chapter_title": f"Foundations of {domain_short}",
                "section_title": f"Core ideas in {domain_short}",
                "subsection_title": "Primary concept",
                "page": 1,
                "element_type": "paragraph",
                "image_filename": "",
            },
            {
                "chunk_id": "mock-002",
                "text": (
                    f"Use definition, mechanism, and application to reason through {example_topic}. "
                    "First identify what it is, then explain why it behaves that way, then apply it."
                ),
                "score": 0.88,
                "chapter_num": 1,
                "chapter_title": f"Foundations of {domain_short}",
                "section_title": "Reasoning workflow",
                "subsection_title": "Definition to application",
                "page": 2,
                "element_type": "paragraph",
                "image_filename": "",
            },
            {
                "chunk_id": "mock-003",
                "text": (
                    f"When evaluating responses in {domain_name}, prioritize precise terminology "
                    "and explicit reasoning steps instead of guess-based answers."
                ),
                "score": 0.84,
                "chapter_num": 2,
                "chapter_title": "Assessment strategy",
                "section_title": "Evidence-based responses",
                "subsection_title": "Reasoning quality",
                "page": 10,
                "element_type": "paragraph",
                "image_filename": "",
            },
            {
                "chunk_id": "mock-004",
                "text": (
                    f"A correct response should connect {example_topic} to an observable outcome "
                    "or practical implication in the learner's context."
                ),
                "score": 0.8,
                "chapter_num": 2,
                "chapter_title": "Assessment strategy",
                "section_title": "Application reasoning",
                "subsection_title": "Outcome mapping",
                "page": 12,
                "element_type": "paragraph",
                "image_filename": "",
            },
            {
                "chunk_id": "mock-005",
                "text": (
                    "If answers are vague, ask a narrower follow-up focused on mechanism, "
                    "then move back to application once the mechanism is clear."
                ),
                "score": 0.77,
                "chapter_num": 3,
                "chapter_title": "Tutoring strategy",
                "section_title": "Hint progression",
                "subsection_title": "Narrowing prompts",
                "page": 20,
                "element_type": "paragraph",
                "image_filename": "",
            },
        ]

    def clear_cache(self) -> None:
        return None

    def retrieve(self, query: str, domain: str | None = None) -> list[dict]:
        _ = (query, domain)
        return [dict(c) for c in self._chunks]
