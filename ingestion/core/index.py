"""
ingestion/index.py
------------------
Build vector + lexical indexes for OT propositions.

Requirements implemented:
- Index ONLY base-chunk propositions (no overlap chunk propositions)
- Embed with OpenAI text-embedding-3-large (3072 dims)
- Upsert into Qdrant collection cfg.memory.kb_collection
- Build BM25Okapi and save to cfg.paths.bm25_ot
- Preserve full PropositionSchema payload (including parent_chunk_text)
"""

from __future__ import annotations

import argparse
import json
import pickle
import random
import re
from collections import Counter
from pathlib import Path

from nltk.stem import PorterStemmer
from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)
from rank_bm25 import BM25Okapi

load_dotenv()

EMBED_BATCH_SIZE = 100
UPSERT_BATCH_SIZE = 200
EMBED_MODEL = "text-embedding-3-large"
VECTOR_SIZE = 3072
_STEMMER = PorterStemmer()


def stem_tokenize(text: str) -> list[str]:
    """
    Lowercase + alnum tokenization + Porter stemming.
    """
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [_STEMMER.stem(t) for t in tokens if t]


def _load_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_base_chunk_ids(chunks_path: str) -> set[str]:
    chunks = _load_jsonl(chunks_path)
    return {
        c["chunk_id"]
        for c in chunks
        if c.get("is_overlap") is False
        and c.get("element_type") != "paragraph_overlap"
    }


def _filter_base_propositions(propositions: list[dict], base_chunk_ids: set[str]) -> list[dict]:
    filtered: list[dict] = []
    for p in propositions:
        # Keep only textbook propositions linked to non-overlap source chunks.
        if p.get("parent_chunk_id") not in base_chunk_ids:
            continue
        if p.get("element_type") == "paragraph_overlap":
            continue
        p2 = dict(p)
        p2["domain"] = "ot"  # enforce required domain tag
        filtered.append(p2)
    return filtered


def _collection_exists(client: QdrantClient, name: str) -> bool:
    try:
        return bool(client.collection_exists(name))
    except Exception:
        names = {c.name for c in client.get_collections().collections}
        return name in names


def _create_fresh_collection(client: QdrantClient, name: str) -> None:
    if _collection_exists(client, name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )


def _embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    total = len(texts)
    for i in range(0, total, EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend([d.embedding for d in resp.data])
    return vectors


def build_indexes(propositions: list[dict], domain: str = "ot") -> None:
    """
    Embed propositions, upsert into Qdrant, and build local BM25 index.
    """
    from config import cfg

    openai_client = OpenAI()
    qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
    collection = cfg.memory.kb_collection

    total = len(propositions)
    print(f"Preparing fresh Qdrant collection: {collection}")
    _create_fresh_collection(qdrant, collection)

    texts = [p["text"] for p in propositions]
    embeddings: list[list[float]] = []

    upserted = 0
    embedded = 0
    points_buffer: list[PointStruct] = []

    for i in range(0, total, EMBED_BATCH_SIZE):
        batch_props = propositions[i:i + EMBED_BATCH_SIZE]
        batch_texts = texts[i:i + EMBED_BATCH_SIZE]
        resp = openai_client.embeddings.create(model=EMBED_MODEL, input=batch_texts)
        batch_vecs = [d.embedding for d in resp.data]
        embeddings.extend(batch_vecs)
        embedded += len(batch_vecs)

        for prop, vec in zip(batch_props, batch_vecs):
            payload = dict(prop)
            payload["domain"] = domain
            points_buffer.append(
                PointStruct(
                    id=prop["proposition_id"],
                    vector=vec,
                    payload=payload,
                )
            )

            if len(points_buffer) >= UPSERT_BATCH_SIZE:
                qdrant.upsert(collection_name=collection, points=points_buffer)
                upserted += len(points_buffer)
                points_buffer = []

        # Required progress print every 500 propositions.
        if embedded % 500 == 0 or embedded == total:
            print(f"Embedded {embedded}/{total} | upserted {upserted}/{total}")

    if points_buffer:
        qdrant.upsert(collection_name=collection, points=points_buffer)
        upserted += len(points_buffer)
        points_buffer = []

    # Ensure final progress line.
    print(f"Embedded {embedded}/{total} | upserted {upserted}/{total}")

    build_bm25_only(propositions, bm25_path=cfg.paths.bm25_ot)


def build_bm25_only(propositions: list[dict], bm25_path: str) -> None:
    """
    Build only BM25 index using stemmed tokens. No vector operations.
    """
    texts = [p["text"] for p in propositions]
    tokenized = [stem_tokenize(t) for t in texts]
    bm25 = BM25Okapi(tokenized)
    out = Path(bm25_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        pickle.dump({"bm25": bm25, "propositions": propositions}, f)
    print(f"BM25 saved -> {out}")


def index_diagrams(diagrams_dir: str = "data/diagrams") -> list[dict]:
    """
    Optional diagram proposition conversion helper.
    Not used in the current indexing run (textbook-only requirement).
    """
    diagram_path = Path(diagrams_dir)
    if not diagram_path.exists():
        return []
    return []


def load_bm25(bm25_path: str) -> tuple:
    with open(bm25_path, "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["propositions"]


def _get_qdrant_vector_count(client: QdrantClient, collection: str) -> int:
    info = client.get_collection(collection)
    count = getattr(info, "vectors_count", None)
    if count is None:
        count = getattr(info, "points_count", None)
    return int(count or 0)


def _qdrant_top_k(
    client: QdrantClient,
    collection: str,
    query_vector: list[float],
    domain: str,
    limit: int = 2,
):
    """
    Compatibility wrapper across qdrant-client versions.
    """
    query_filter = Filter(
        must=[FieldCondition(key="domain", match=MatchValue(value=domain))]
    )
    if hasattr(client, "query_points"):
        resp = client.query_points(
            collection_name=collection,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return list(getattr(resp, "points", []))
    # Older qdrant-client versions
    return client.search(
        collection_name=collection,
        query_vector=query_vector,
        query_filter=query_filter,
        limit=limit,
    )


def run_index_tests(expected_count: int, propositions: list[dict]) -> None:
    from config import cfg

    print("\n=== INDEX TEST 1 — Qdrant health ===")
    qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
    collection = cfg.memory.kb_collection
    exists = _collection_exists(qdrant, collection)
    print(f"Collection exists ({collection}): {exists}")

    vector_count = _get_qdrant_vector_count(qdrant, collection) if exists else 0
    print(f"Vector count: {vector_count}")
    print(f"Expected count: {expected_count}")

    points, _ = qdrant.scroll(
        collection_name=collection,
        limit=3,
        with_payload=True,
        with_vectors=False,
    )
    for i, pt in enumerate(points, 1):
        keys = sorted((pt.payload or {}).keys())
        print(f"Point {i} payload fields: {keys}")
        print(f"  parent_chunk_text present: {'parent_chunk_text' in keys}")

    print("\n=== INDEX TEST 2 — BM25 health ===")
    bm25_file = Path(cfg.paths.bm25_ot)
    print(f"BM25 file exists: {bm25_file.exists()} ({bm25_file})")
    bm25, bm25_props = load_bm25(str(bm25_file))
    print(f"BM25 loads: {bm25 is not None}")
    print(f"BM25 proposition count: {len(bm25_props)}")

    print("\n=== INDEX TEST 3 — Manual semantic search (Qdrant) ===")
    openai_client = OpenAI()
    queries = [
        "deltoid muscle shoulder",
        "axillary nerve C5 C6",
        "muscle contraction ATP",
        "brachial plexus posterior cord",
        "joint range of motion",
    ]
    for q in queries:
        q_vec = openai_client.embeddings.create(model=EMBED_MODEL, input=[q]).data[0].embedding
        hits = _qdrant_top_k(
            client=qdrant,
            collection=collection,
            query_vector=q_vec,
            domain="ot",
            limit=2,
        )
        print(f"\nQuery: {q}")
        for r in hits:
            txt = (r.payload or {}).get("text", "")[:180]
            print(f"  score={r.score:.4f} | {txt}")

    print("\n=== INDEX TEST 4 — Manual BM25 search ===")
    bm25_queries = ["axillary", "supraspinatus", "sarcomere"]
    for q in bm25_queries:
        tokens = q.lower().split()
        scores = bm25.get_scores(tokens)
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:2]
        print(f"\nQuery: {q}")
        for i in top_idx:
            print(f"  score={scores[i]:.4f} | {bm25_props[i]['text'][:180]}")

    print("\n=== INDEX TEST 5 — Domain filter physics ===")
    q_vec = openai_client.embeddings.create(model=EMBED_MODEL, input=["newtonian mechanics force"]).data[0].embedding
    physics_hits = _qdrant_top_k(
        client=qdrant,
        collection=collection,
        query_vector=q_vec,
        domain="physics",
        limit=2,
    )
    print(f"Physics-filtered results count: {len(physics_hits)}")


if __name__ == "__main__":
    from config import cfg

    parser = argparse.ArgumentParser(description="Build OT indexes (Qdrant + BM25 or BM25-only).")
    parser.add_argument("--bm25-only", action="store_true", help="Rebuild BM25 only (no embedding/upsert).")
    args = parser.parse_args()

    all_props = _load_jsonl(cfg.paths.propositions_ot)
    base_chunk_ids = _load_base_chunk_ids(cfg.paths.chunks_ot)
    propositions = _filter_base_propositions(all_props, base_chunk_ids)

    print(f"Loaded propositions (raw): {len(all_props)}")
    print(f"Base chunk ids: {len(base_chunk_ids)}")
    print(f"Indexing propositions (base only): {len(propositions)}")

    if args.bm25_only:
        build_bm25_only(propositions, bm25_path=cfg.paths.bm25_ot)
    else:
        build_indexes(propositions, domain="ot")
        run_index_tests(expected_count=len(propositions), propositions=propositions)
