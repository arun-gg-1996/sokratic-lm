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
from ingestion.core.index import load_bm25, stem_tokenize

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
        # max_length=512 prevents the "tensor a vs b dimension mismatch"
        # crash when (query + chunk_text) tokenizes to >512 — long student
        # questions paired with full parent chunks blow past it. The MedCPT
        # encoder was trained with 512 max anyway; truncating in the
        # tokenizer matches its training distribution.
        # Device selection: prefer Apple-Silicon MPS, fall back to CUDA, then CPU.
        # MPS gives ~3-5x speedup on M-series Macs vs CPU for the
        # cross-encoder forward pass.
        try:
            import torch  # type: ignore
            if torch.backends.mps.is_available():
                _ce_device = "mps"
            elif torch.cuda.is_available():
                _ce_device = "cuda"
            else:
                _ce_device = "cpu"
        except Exception:
            _ce_device = "cpu"
        self.cross_encoder = CrossEncoder(
            cfg.models.cross_encoder, max_length=512, device=_ce_device
        )
        # Domain ontology adapter (UMLS for anatomy/OT, Noop for physics etc.).
        # Construct, then EAGERLY warm up so the first retrieve() call doesn't
        # pay the ~70-second scispacy + UMLS KB load cost. Warmup is a single
        # link_entities() invocation; subsequent calls hit the cached pipeline.
        from retrieval.ontology import get_ontology_adapter

        self._ontology = get_ontology_adapter(self.default_domain)
        try:
            self._ontology.link_entities("anatomy")
        except Exception:
            # Warmup failures degrade silently to noop semantics on first real call.
            pass

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

    def _qdrant_search(
        self,
        query_vector: list[float],
        top_k: int,
        domain: str,
        locked_section: str | None = None,
        locked_subsection: str | None = None,
    ) -> list[dict]:
        must: list = [FieldCondition(key="domain", match=MatchValue(value=domain))]
        # Hard section/subsection filter at query time. This is the core
        # groundedness guarantee: when a TOC topic is locked, retrieval cannot
        # drift to another chapter because off-section chunks never enter the
        # candidate pool. Prefer the most specific locked field available.
        sub = (locked_subsection or "").strip()
        sec = (locked_section or "").strip()
        if sub:
            must.append(FieldCondition(key="subsection_title", match=MatchValue(value=sub)))
        elif sec:
            must.append(FieldCondition(key="section_title", match=MatchValue(value=sec)))
        qfilter = Filter(must=must)
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

    def _bm25_search(
        self,
        query: str,
        top_k: int,
        locked_section: str | None = None,
        locked_subsection: str | None = None,
    ) -> list[dict]:
        tokens = preprocess_for_bm25(query)
        scores = self.bm25.get_scores(tokens)

        sub = (locked_subsection or "").strip()
        sec = (locked_section or "").strip()
        if sub or sec:
            # Hard section/subsection filter: mask out-of-section propositions
            # so BM25 can't rank a lucky keyword match from another chapter
            # above in-section evidence. Mirrors the Qdrant-side hard filter.
            for i, p in enumerate(self.bm25_props):
                if sub and str(p.get("subsection_title", "")).strip() != sub:
                    scores[i] = float("-inf")
                elif not sub and sec and str(p.get("section_title", "")).strip() != sec:
                    scores[i] = float("-inf")

        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        top_idx = [i for i in top_idx if scores[i] != float("-inf")]

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

    def _cross_encoder_rerank(
        self,
        query: str,
        parent_chunks: list[dict],
    ) -> list[dict]:
        if not parent_chunks:
            return []
        pairs = [(query, c.get("text", "")) for c in parent_chunks]
        ce_scores = self.cross_encoder.predict(pairs)

        ranked: list[dict] = []
        for chunk, score in zip(parent_chunks, ce_scores):
            row = dict(chunk)
            base = float(score)
            row["score"] = base
            row["ce_score_raw"] = base
            ranked.append(row)

        ranked.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        return ranked

    @staticmethod
    def _is_weak_retrieval(qdrant_hits: list[dict], bm25_hits: list[dict], query: str = "") -> bool:
        """
        Decide whether the original-query retrieval is weak enough that HyDE
        rescue is worth the latency.

        Distributional cosine signal (P1.4): a single high-cosine hit surrounded
        by weak hits (e.g. one lucky chunk + noise) is NOT a strong retrieval —
        it often reflects a single literal-keyword match rather than broad
        topical coverage. We fire HyDE in two complementary cases:
          (a) `max_cosine < hyde_weak_cosine_threshold` — primary signal; the
              whole retrieval is below threshold.
          (b) `topk_mean_cosine < hyde_weak_topk_mean_threshold` — secondary
              signal; even though one chunk is decent, the top-K as a whole is
              weak, which usually means the query's register mismatches the
              corpus. HyDE's hypothetical passage often fixes this.

        We deliberately do NOT use BM25 here: function-word overlap inflates
        BM25 on genuinely off-topic queries.

        We deliberately do NOT use CE here: running CE requires the full
        candidate expansion, which defeats the purpose of a cheap pre-gate.
        """
        if not qdrant_hits:
            return True  # empty dense side → HyDE can't hurt
        cosines = [float(h.get("_qdrant_score", 0.0)) for h in qdrant_hits]
        max_cosine = max(cosines, default=0.0)
        weak_cos = float(getattr(cfg.retrieval, "hyde_weak_cosine_threshold", 0.65))
        if max_cosine < weak_cos:
            return True

        topk = int(getattr(cfg.retrieval, "hyde_weak_topk", 5))
        topk = max(1, min(topk, len(cosines)))
        topk_mean = sum(cosines[:topk]) / topk
        weak_mean = float(getattr(cfg.retrieval, "hyde_weak_topk_mean_threshold", 0.0))
        # Threshold of 0.0 disables the distributional signal (keeps old behavior).
        if weak_mean > 0.0 and topk_mean < weak_mean:
            return True
        return False

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

        Curated high-precision fallback: each alias dict entry is a hand-written
        (alias → expansion) mapping. For each alias that appears as a word-
        bounded substring in the query (case-insensitive), append the expansion.
        We never REPLACE — the student's original phrasing is preserved so
        BM25 / embedder can still match on it directly.

        Ontology expansion (via `_apply_ontology_expansion`) supersedes this for
        the OT/anatomy domain when the UMLS pipeline is available. The alias
        dict remains active as a safety net for cases UMLS misses and for
        domains without an ontology adapter.

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

    def _apply_ontology_expansion(self, query: str) -> str:
        """
        Append UMLS canonical names (and, when available, a small number of
        aliases) for entities detected in the query. Purely additive — the
        original phrasing is preserved for BM25 / dense exact matches.

        The ontology adapter is a NoopAdapter for domains without an ontology
        configured, in which case this is a zero-cost no-op. For anatomy/OT,
        UMLS turns "deltoid" into "Deltoid muscle" (matches section titles)
        and "cn vii" into "Facial nerve" (bridges abbreviation gap).

        Toggle: `cfg.retrieval.ontology_expansion_enabled` (default True).
        Keeping it configurable lets us A/B with the curated alias dict
        during the v2 ablation window before fully retiring the dict.
        """
        if not query:
            return query
        if not bool(getattr(cfg.retrieval, "ontology_expansion_enabled", True)):
            return query
        if self._ontology is None:
            return query
        try:
            mentions = self._ontology.link_entities(query)
        except Exception:
            return query
        if not mentions:
            return query
        lowered = query.lower()
        seen: set[str] = {lowered}
        extras: list[str] = []
        # Cap total additions so a pathological query doesn't balloon the BM25
        # tokens. In practice the linker returns 1-4 mentions per student query.
        max_extras = int(getattr(cfg.retrieval, "ontology_max_extras", 6))
        for m in mentions:
            if len(extras) >= max_extras:
                break
            for term in (m.canonical, m.span):
                t = (term or "").strip()
                if not t:
                    continue
                if t.lower() in seen:
                    continue
                seen.add(t.lower())
                extras.append(t)
                if len(extras) >= max_extras:
                    break
        if not extras:
            return query
        return f"{query} ({' '.join(extras)})"

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

    # ── D.0 — Window expansion at retrieval time ─────────────────────────
    # The new chunker produces fine-grained chunks (median ~89 tokens for
    # paragraph chunks, ~133 tokens for overlap chunks). At top_k=5 with no
    # window, the LLM sees only ~565 tokens of context — too thin for
    # tutoring. Window expansion fetches W chunks before and W chunks after
    # each retrieved primary chunk via the prev_chunk_id/next_chunk_id
    # links that B.4 wrote into every Qdrant payload + chunks JSONL row.
    #
    # Implementation note: we lazy-load the chunks JSONL into an in-memory
    # dict on first use. This avoids extra Qdrant round-trips per retrieval
    # call (5 retrieved × 2 neighbors = 10 lookups each ~5-10ms otherwise).
    # The full chunks dict is ~10 MB for the OpenStax corpus — trivial.

    _chunks_index_cache: dict[str, dict] | None = None

    def _load_chunks_index(self) -> dict[str, dict]:
        """Lazy-load chunks_<domain>.jsonl into a chunk_id -> chunk dict map.
        Idempotent; cached on the instance."""
        if self._chunks_index_cache is not None:
            return self._chunks_index_cache

        domain = (self.default_domain or "").strip().lower()
        # Look for explicit cfg path first, then fall back to standard naming.
        candidate_paths: list[str] = []
        path_attr = f"chunks_{domain}" if domain else ""
        if path_attr and hasattr(cfg.paths, path_attr):
            candidate_paths.append(getattr(cfg.paths, path_attr))
        # Fallback: data/processed/chunks_<domain>.jsonl.
        candidate_paths.append(f"data/processed/chunks_{domain}.jsonl")
        # Final fallback: legacy chunks_ot.jsonl.
        candidate_paths.append("data/processed/chunks_ot.jsonl")

        loaded_path = None
        index: dict[str, dict] = {}
        import json as _json
        from pathlib import Path as _Path
        for p in candidate_paths:
            if _Path(p).exists():
                loaded_path = p
                with open(p) as f:
                    for line in f:
                        c = _json.loads(line)
                        cid = c.get("chunk_id")
                        if cid:
                            index[cid] = c
                break
        if loaded_path is None:
            print(f"  [retriever] WARN: no chunks file found for domain {domain!r}; "
                  "window expansion disabled.")
        else:
            print(f"  [retriever] loaded {len(index)} chunks for window expansion "
                  f"from {loaded_path}")
        self._chunks_index_cache = index
        return index

    def _expand_window(
        self,
        primary_chunks: list[dict],
        window_size: int,
        max_total_tokens: int = 4000,
    ) -> list[dict]:
        """For each chunk in `primary_chunks`, prepend up to W neighbors via
        prev_chunk_id and append up to W neighbors via next_chunk_id.

        Returns a flat list with `_window_role` markers:
          "primary"   — the originally-retrieved chunk (kept first)
          "neighbor_prev" — N-W..N-1 chunks
          "neighbor_next" — N+1..N+W chunks
        Each neighbor row has `_primary_chunk_id` pointing back to its primary.

        Token budget cap: stops adding neighbors once total cumulative chunk
        text exceeds max_total_tokens (rough estimate: chars/4).
        """
        if window_size <= 0 or not primary_chunks:
            return primary_chunks

        idx = self._load_chunks_index()
        if not idx:
            return primary_chunks  # No chunks file available; degrade gracefully.

        # Avoid duplicates if two retrieved primaries share neighbors.
        emitted: set[str] = set()
        out: list[dict] = []
        total_chars = 0

        def _approx_tokens(text: str) -> int:
            return max(1, len(text or "") // 4)

        for primary in primary_chunks:
            pid = primary.get("chunk_id")
            if not pid or pid in emitted:
                continue

            # Build the prev neighbors (walking backward W steps).
            prevs: list[dict] = []
            cursor = idx.get(pid, {})
            for _ in range(window_size):
                prev_id = cursor.get("prev_chunk_id") if isinstance(cursor, dict) else None
                if not prev_id or prev_id in emitted:
                    break
                prev_chunk = idx.get(prev_id)
                if not prev_chunk:
                    break
                prevs.insert(0, prev_chunk)  # earliest first
                cursor = prev_chunk

            # Build the next neighbors (walking forward W steps).
            nexts: list[dict] = []
            cursor = idx.get(pid, {})
            for _ in range(window_size):
                next_id = cursor.get("next_chunk_id") if isinstance(cursor, dict) else None
                if not next_id or next_id in emitted:
                    break
                next_chunk = idx.get(next_id)
                if not next_chunk:
                    break
                nexts.append(next_chunk)
                cursor = next_chunk

            # Emit prevs (in reading order), then primary, then nexts.
            for nb in prevs:
                t = nb.get("text", "")
                if total_chars // 4 + _approx_tokens(t) > max_total_tokens:
                    break  # budget exhausted
                out.append({
                    **{k: nb.get(k, "") for k in (
                        "chunk_id", "text", "chapter_num", "chapter_title",
                        "section_title", "subsection_title", "page",
                        "element_type", "image_filename",
                    )},
                    "_window_role": "neighbor_prev",
                    "_primary_chunk_id": pid,
                })
                emitted.add(nb.get("chunk_id", ""))
                total_chars += len(t)

            # Always emit primary even if budget tight (it's the actual hit).
            primary_row = dict(primary)
            primary_row["_window_role"] = "primary"
            primary_row["_primary_chunk_id"] = pid
            out.append(primary_row)
            emitted.add(pid)
            total_chars += len(primary.get("text", ""))

            for nb in nexts:
                t = nb.get("text", "")
                if total_chars // 4 + _approx_tokens(t) > max_total_tokens:
                    break
                out.append({
                    **{k: nb.get(k, "") for k in (
                        "chunk_id", "text", "chapter_num", "chapter_title",
                        "section_title", "subsection_title", "page",
                        "element_type", "image_filename",
                    )},
                    "_window_role": "neighbor_next",
                    "_primary_chunk_id": pid,
                })
                emitted.add(nb.get("chunk_id", ""))
                total_chars += len(t)

        return out

    def retrieve(
        self,
        query: str,
        domain: str | None = None,
        top_k: int | None = None,
        locked_section: str | None = None,
        locked_subsection: str | None = None,
        window_size: int | None = None,
    ) -> list[dict]:
        # Per-call timing instrumentation. Each stage's elapsed wall-time
        # is written to self.last_timings (in ms) so callers (eval scripts,
        # observability) can read it after retrieve() returns. Cleared at
        # the top of every call so stale stats don't leak across queries.
        import time as _time
        t = _time.perf_counter
        timings: dict[str, Any] = {
            "ontology_ms": 0.0, "alias_ms": 0.0,
            "embed_orig_ms": 0.0, "qdrant_orig_ms": 0.0, "bm25_ms": 0.0,
            "hyde_fired": False, "ood_short_circuited": False,
            "hyde_rewrite_ms": 0.0, "hyde_embed_ms": 0.0, "hyde_qdrant_ms": 0.0,
            "rrf_merge_ms": 0.0, "expand_parents_ms": 0.0,
            "ce_rerank_ms": 0.0, "in_scope_ms": 0.0, "window_expand_ms": 0.0,
            "n_qdrant_orig": 0, "n_qdrant_hyde": 0, "n_bm25": 0,
            "n_parent_chunks": 0, "n_returned": 0, "total_ms": 0.0,
            # Diagnostic signals for OOD threshold tuning. max_cosine is the
            # top dense-similarity score from the original Qdrant search;
            # max_ce is the top cross-encoder score post-rerank. These two
            # govern whether _is_in_scope passes — capture them per query so
            # callers can pick thresholds from real distributions.
            "max_cosine": 0.0, "max_ce": 0.0,
        }
        self.last_timings = timings
        t_call_start = t()

        query = (query or "").strip()
        if not query:
            timings["total_ms"] = (t() - t_call_start) * 1000.0
            return []
        domain = (domain or self.default_domain or "").strip()
        if not domain:
            raise ValueError(
                "Retriever.retrieve: empty domain. Set cfg.domain.retrieval_domain "
                "(e.g., 'anatomy') or pass domain=... explicitly."
            )

        # Expand query in two additive stages:
        #   1) UMLS / domain ontology: canonical + abbreviated entity names.
        #   2) Curated alias dict: high-precision fallback for misses.
        # Both stages are additive — original phrasing is preserved so BM25
        # and dense exact matches are unaffected.
        t0 = t()
        expanded_query = self._apply_ontology_expansion(query)
        timings["ontology_ms"] = (t() - t0) * 1000.0
        t0 = t()
        expanded_query = self._apply_query_aliases(expanded_query)
        timings["alias_ms"] = (t() - t0) * 1000.0

        q_top_k = int(cfg.retrieval.qdrant_top_k)
        b_top_k = int(cfg.retrieval.bm25_top_k)
        final_k = int(top_k) if top_k is not None else int(cfg.retrieval.top_chunks_final)
        rrf_k = int(cfg.retrieval.rrf_k)

        # --- Stage 1: original query (fast path, no LLM) -------------------
        t0 = t()
        orig_vec = self._embed_query(expanded_query)
        timings["embed_orig_ms"] = (t() - t0) * 1000.0
        t0 = t()
        orig_qdrant = self._qdrant_search(
            orig_vec,
            top_k=q_top_k,
            domain=domain,
            locked_section=locked_section,
            locked_subsection=locked_subsection,
        )
        timings["qdrant_orig_ms"] = (t() - t0) * 1000.0
        timings["n_qdrant_orig"] = len(orig_qdrant)
        t0 = t()
        orig_bm25 = self._bm25_search(
            expanded_query,
            top_k=b_top_k,
            locked_section=locked_section,
            locked_subsection=locked_subsection,
        )
        timings["bm25_ms"] = (t() - t0) * 1000.0
        timings["n_bm25"] = len(orig_bm25)
        # Snapshot the top original-cosine for diagnostics (set before the
        # OOD short-circuit reads the same value).
        timings["max_cosine"] = max(
            (float(h.get("_qdrant_score", 0.0)) for h in orig_qdrant), default=0.0
        )

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
            timings["ood_short_circuited"] = True
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
            timings["hyde_fired"] = True
            t0 = t()
            hyde_text = self._hyde_rewrite(query)
            timings["hyde_rewrite_ms"] = (t() - t0) * 1000.0
            if hyde_text:
                t0 = t()
                hyde_vec = self._embed_query(hyde_text)
                timings["hyde_embed_ms"] = (t() - t0) * 1000.0
                t0 = t()
                hyde_qdrant = self._qdrant_search(
                    hyde_vec,
                    top_k=q_top_k,
                    domain=domain,
                    locked_section=locked_section,
                    locked_subsection=locked_subsection,
                )
                timings["hyde_qdrant_ms"] = (t() - t0) * 1000.0
                timings["n_qdrant_hyde"] = len(hyde_qdrant)

        # Merge Qdrant hit lists from both stages (dedupe by proposition_id).
        t0 = t()
        merged_qdrant = self._dedupe_hits_keep_best_rank(orig_qdrant, hyde_qdrant)
        merged = self._rrf_merge(merged_qdrant, orig_bm25, k=rrf_k)
        timings["rrf_merge_ms"] = (t() - t0) * 1000.0
        t0 = t()
        parent_chunks = self._expand_to_parent_chunks(merged)
        timings["expand_parents_ms"] = (t() - t0) * 1000.0
        timings["n_parent_chunks"] = len(parent_chunks)
        # CE reranks against the alias-expanded query (`expanded_query`), not
        # the HyDE rewrite. The alias expansion is deterministic and high-
        # precision (curated human-written mappings); the HyDE rewrite is LLM
        # output that may introduce paraphrased terminology. So keep the alias
        # signal at rerank time, but not the HyDE signal.
        t0 = t()
        reranked = self._cross_encoder_rerank(expanded_query, parent_chunks)
        timings["ce_rerank_ms"] = (t() - t0) * 1000.0
        timings["max_ce"] = max(
            (float(r.get("score", 0.0)) for r in reranked), default=0.0
        )

        if not reranked:
            timings["total_ms"] = (t() - t_call_start) * 1000.0
            return []

        t0 = t()
        in_scope = self._is_in_scope(merged_qdrant, orig_bm25, reranked)
        timings["in_scope_ms"] = (t() - t0) * 1000.0
        if not in_scope:
            timings["total_ms"] = (t() - t_call_start) * 1000.0
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

        # D.0 — Window expansion. Default W=1 so 5 retrieved chunks become
        # 15 chunks shown to LLM (~1700 tokens median context, vs ~565 tok
        # without expansion at the new chunker's median 113 tok/chunk).
        # Caller can pass window_size=0 to opt out, or larger for more context.
        ws = window_size
        if ws is None:
            ws = int(getattr(cfg.retrieval, "window_size", 1))
        if ws > 0:
            t0 = t()
            payload = self._expand_window(payload, window_size=ws)
            timings["window_expand_ms"] = (t() - t0) * 1000.0
        timings["n_returned"] = len(payload)
        timings["total_ms"] = (t() - t_call_start) * 1000.0
        return payload


class ChunkRetriever(Retriever):
    """
    Chunk-level (non-proposition) retriever variant.

    Why this class exists
    ---------------------
    The default `Retriever` indexes propositions and expands them back to
    parent chunks at retrieval time (the Dense-X-Retrieval architecture).
    End-to-end testing on canonical anatomy questions ("which nerve
    innervates the deltoid?") showed atomic propositions destroy the
    relational verb that should be the discriminative retrieval signal.
    Literature follow-up (arXiv 2510.04757, Oct 2025) corroborates: the
    2025 SOTA pattern for biomedical RAG indexes full chunks with bi-encoder
    retrieval, not propositions.

    What this overrides
    -------------------
    Three points where the proposition pipeline differs from chunk-level:
      1. `__init__`        — point at the chunks Qdrant collection +
                              the chunks BM25 path produced by
                              `scripts/reindex_chunks.py`.
      2. `_qdrant_search`  — payload is already chunk-shaped; surface
                              `chunk_id` directly, no parent-chunk indirection.
      3. `_bm25_search`    — pickle file's "propositions" key now holds
                              chunks (same structural type), but item-level
                              fields are chunk fields.
      4. `_rrf_merge`      — fuse by `chunk_id` instead of `proposition_id`.
      5. `_expand_to_parent_chunks` — chunks ARE the parents; pass-through
                              with normalised score / hit_count.

    Everything else (HyDE, ontology expansion, alias dictionary, CE rerank,
    in-scope check, window expansion, OOD short-circuit) is inherited
    unchanged.
    """

    def __init__(self, *, collection: str = "sokratic_kb_chunks",
                 bm25_path: str | None = None) -> None:
        # Resolve BM25 path to an absolute path relative to the project root
        # so the retriever works no matter what cwd the caller is in.
        # Default lives at <project_root>/data/indexes/bm25_chunks_openstax_anatomy.pkl.
        if bm25_path is None:
            from pathlib import Path as _Path
            _root = _Path(__file__).resolve().parent.parent
            bm25_path = str(_root / "data/indexes/bm25_chunks_openstax_anatomy.pkl")
        # Bypass Retriever.__init__ so we can swap collection + bm25 path
        # without touching cfg.memory.kb_collection / cfg.paths.
        from openai import OpenAI
        from qdrant_client import QdrantClient
        from sentence_transformers import CrossEncoder
        from ingestion.core.index import load_bm25
        from retrieval.ontology import get_ontology_adapter

        self.index_dir = None
        self.embed_model = cfg.models.embeddings
        self.collection = collection
        self.default_domain = getattr(getattr(cfg, "domain", object()),
                                      "retrieval_domain", "")

        self.openai = OpenAI()
        self.qdrant = QdrantClient(host=cfg.memory.qdrant_host,
                                   port=cfg.memory.qdrant_port)
        # Validate the chunks collection's vector dimension matches the
        # embedding model. We don't reuse Retriever._validate_embedding_dimension
        # because that one reads from cfg.memory.kb_collection.
        info = self.qdrant.get_collection(self.collection)
        actual_dim = getattr(getattr(getattr(info, "config", None), "params", None),
                             "vectors", None)
        actual_dim = getattr(actual_dim, "size", None)
        expected_dim = int(cfg.qdrant.vector_size)
        if actual_dim is None or int(actual_dim) != expected_dim:
            raise ValueError(
                f"ChunkRetriever: dimension mismatch on '{self.collection}': "
                f"expected {expected_dim}, got {actual_dim}. Run reindex_chunks.py first."
            )

        self.bm25, self.bm25_props = load_bm25(bm25_path)

        # Cross-encoder + ontology — same setup as the parent.
        try:
            import torch
            if torch.backends.mps.is_available():
                _ce_device = "mps"
            elif torch.cuda.is_available():
                _ce_device = "cuda"
            else:
                _ce_device = "cpu"
        except Exception:
            _ce_device = "cpu"
        self.cross_encoder = CrossEncoder(
            cfg.models.cross_encoder, max_length=512, device=_ce_device
        )

        self._ontology = get_ontology_adapter(self.default_domain)
        try:
            self._ontology.link_entities("anatomy")
        except Exception:
            pass

    # ----- payload extraction overrides -------------------------------------

    def _qdrant_search(
        self,
        query_vector: list[float],
        top_k: int,
        domain: str,
        locked_section: str | None = None,
        locked_subsection: str | None = None,
    ) -> list[dict]:
        must: list = [FieldCondition(key="domain", match=MatchValue(value=domain))]
        sub = (locked_subsection or "").strip()
        sec = (locked_section or "").strip()
        if sub:
            must.append(FieldCondition(key="subsection_title", match=MatchValue(value=sub)))
        elif sec:
            must.append(FieldCondition(key="section_title", match=MatchValue(value=sec)))
        qfilter = Filter(must=must)
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
            cid = payload.get("chunk_id") or str(getattr(p, "id", rank))
            hits.append(
                {
                    # Stamp `chunk_id` AND `proposition_id` so downstream code
                    # that keys on either field (RRF, expand-to-parent) works
                    # without branches. They're identical in chunks-mode.
                    "chunk_id": str(cid),
                    "proposition_id": str(cid),
                    "text": payload.get("text", ""),
                    "parent_chunk_id": str(cid),
                    "parent_chunk_text": payload.get("text", ""),
                    "chapter_num": payload.get("chapter_num", 0),
                    "chapter_title": payload.get("chapter_title", ""),
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

    def _bm25_search(
        self,
        query: str,
        top_k: int,
        locked_section: str | None = None,
        locked_subsection: str | None = None,
    ) -> list[dict]:
        tokens = preprocess_for_bm25(query)
        scores = self.bm25.get_scores(tokens)

        sub = (locked_subsection or "").strip()
        sec = (locked_section or "").strip()
        if sub or sec:
            for i, p in enumerate(self.bm25_props):
                if sub and str(p.get("subsection_title", "")).strip() != sub:
                    scores[i] = float("-inf")
                elif not sub and sec and str(p.get("section_title", "")).strip() != sec:
                    scores[i] = float("-inf")

        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

        hits: list[dict] = []
        for rank, idx in enumerate(top_idx, start=1):
            if scores[idx] == float("-inf"):
                break
            chunk = self.bm25_props[idx]
            cid = chunk.get("chunk_id") or f"bm25-{idx}"
            hits.append(
                {
                    "chunk_id": str(cid),
                    "proposition_id": str(cid),
                    "text": chunk.get("text", ""),
                    "parent_chunk_id": str(cid),
                    "parent_chunk_text": chunk.get("text", ""),
                    "chapter_num": chunk.get("chapter_num", 0),
                    "chapter_title": chunk.get("chapter_title", ""),
                    "section_title": chunk.get("section_title", ""),
                    "subsection_title": chunk.get("subsection_title", ""),
                    "page": chunk.get("page", 0),
                    "element_type": chunk.get("element_type", "paragraph"),
                    "domain": chunk.get("domain", self.default_domain or ""),
                    "image_filename": chunk.get("image_filename", ""),
                    "_bm25_rank": rank,
                    "_bm25_score": float(scores[idx]),
                }
            )
        return hits


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
