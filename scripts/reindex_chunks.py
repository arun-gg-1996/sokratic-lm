"""
scripts/reindex_chunks.py
-------------------------
Re-index the corpus at the CHUNK level (drop the proposition layer).

Why this exists
---------------
The Dense X Retrieval / propositions-as-retrieval-units architecture was
adopted on faith from the Wikipedia-trained EMNLP 2024 result. End-to-end
testing against canonical anatomy questions ("which nerve innervates the
deltoid?") showed the right chunks don't even reach top-50 of either dense
or BM25 search — propositions atomize the relational verb that should be
the discriminative signal. Literature follow-up confirmed: the Dense-X
result has not been validated on biomedical / textbook corpora, and the
2025 SOTA pattern (arXiv 2510.04757) uses bi-encoder retrieval over full
chunks with optional ColBERT rerank.

What this does
--------------
  - Reads data/processed/chunks_openstax_anatomy.jsonl (7,574 chunks)
  - Embeds each chunk's `text` field via OpenAI text-embedding-3-large
  - Upserts to a NEW Qdrant collection `sokratic_kb_chunks`
  - Builds a NEW BM25 index at data/indexes/bm25_chunks_openstax_anatomy.pkl
  - Leaves the existing `sokratic_kb` (propositions) collection untouched
    so we can A/B compare and roll back instantly if needed.

Cost
----
  - ~7,500 embeddings × ~100 tokens each = ~750k input tokens
    @ $0.13 per 1M for text-embedding-3-large = ~$0.10
  - ~10 minutes wall on a stable network

Usage
-----
  cd /Users/arun-ghontale/UB/NLP/sokratic
  .venv/bin/python scripts/reindex_chunks.py
  .venv/bin/python scripts/reindex_chunks.py --collection sokratic_kb_chunks --fresh
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.http.models import (
    Distance,
    PointStruct,
    VectorParams,
)  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

from config import cfg  # noqa: E402
from ingestion.core.index import stem_tokenize  # noqa: E402

CHUNKS_PATH = ROOT / "data/processed/chunks_openstax_anatomy.jsonl"
BM25_OUT_PATH = ROOT / "data/indexes/bm25_chunks_openstax_anatomy.pkl"
EMBED_MODEL = "text-embedding-3-large"
EMBED_BATCH_SIZE = 100
UPSERT_BATCH_SIZE = 100
VECTOR_SIZE = 3072  # text-embedding-3-large


def load_chunks(path: Path) -> list[dict]:
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def ensure_fresh_collection(client: QdrantClient, name: str) -> None:
    if client.collection_exists(name):
        print(f"  Wiping existing collection {name!r}")
        client.delete_collection(collection_name=name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"  Created fresh collection {name!r} (size={VECTOR_SIZE}, cosine)")


def chunk_to_payload(chunk: dict, domain: str) -> dict:
    """Build the Qdrant payload for a chunk. Preserves all the metadata the
    retriever already reads (chunk_id, prev/next links for window expansion,
    chapter/section/subsection for filtering, page + element_type for
    multimodal hooks)."""
    return {
        "chunk_id": chunk.get("chunk_id", ""),
        "text": chunk.get("text", ""),
        "domain": domain,
        "textbook_id": chunk.get("textbook_id", domain),
        "chapter_num": chunk.get("chapter_num", 0),
        "chapter_title": chunk.get("chapter_title", ""),
        "section_title": chunk.get("section_title", ""),
        "subsection_title": chunk.get("subsection_title", ""),
        "page": chunk.get("page", 0),
        "element_type": chunk.get("element_type", "paragraph"),
        "prev_chunk_id": chunk.get("prev_chunk_id"),
        "next_chunk_id": chunk.get("next_chunk_id"),
        "image_filename": chunk.get("image_filename", ""),
        # Marker so retriever / debug tools can tell at a glance that this
        # is a chunk-indexed point, not a proposition-indexed one.
        "indexing_unit": "chunk",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="sokratic_kb_chunks",
                    help="Qdrant collection name (default: sokratic_kb_chunks; "
                         "kept separate from propositions collection sokratic_kb).")
    ap.add_argument("--domain", default="openstax_anatomy",
                    help="Payload `domain` value (default: openstax_anatomy).")
    ap.add_argument("--fresh", action="store_true", default=True,
                    help="Wipe + recreate the collection before upsert (default: yes).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Smoke-test mode: only embed the first N chunks.")
    args = ap.parse_args()

    print(f"Loading chunks from {CHUNKS_PATH.relative_to(ROOT)}", flush=True)
    chunks = load_chunks(CHUNKS_PATH)
    if args.limit:
        chunks = chunks[: args.limit]
    print(f"  {len(chunks)} chunks", flush=True)

    openai = OpenAI()
    qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)

    if args.fresh:
        ensure_fresh_collection(qdrant, args.collection)
    else:
        if not qdrant.collection_exists(args.collection):
            qdrant.create_collection(
                collection_name=args.collection,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )

    total = len(chunks)
    embedded = 0
    upserted = 0
    points_buffer: list[PointStruct] = []
    t_start = time.time()

    for i in range(0, total, EMBED_BATCH_SIZE):
        batch_chunks = chunks[i:i + EMBED_BATCH_SIZE]
        # Replace empty texts with a single space — OpenAI rejects empty inputs.
        batch_texts = [(c.get("text", "") or " ").strip() or " "
                       for c in batch_chunks]

        resp = openai.embeddings.create(model=EMBED_MODEL, input=batch_texts)
        vectors = [d.embedding for d in resp.data]
        embedded += len(vectors)

        for chunk, vec in zip(batch_chunks, vectors):
            points_buffer.append(
                PointStruct(
                    id=chunk["chunk_id"],
                    vector=vec,
                    payload=chunk_to_payload(chunk, args.domain),
                )
            )
            if len(points_buffer) >= UPSERT_BATCH_SIZE:
                qdrant.upsert(collection_name=args.collection, points=points_buffer)
                upserted += len(points_buffer)
                points_buffer = []

        elapsed = int(time.time() - t_start)
        rate = embedded / max(elapsed, 1)
        eta = int((total - embedded) / max(rate, 0.01))
        print(f"  embedded {embedded}/{total} | upserted {upserted}/{total} | "
              f"{elapsed}s elapsed | ETA ~{eta}s", flush=True)

    if points_buffer:
        qdrant.upsert(collection_name=args.collection, points=points_buffer)
        upserted += len(points_buffer)
        points_buffer = []

    print(f"\nDONE Qdrant: {embedded}/{total} embedded, {upserted}/{total} upserted "
          f"in {int(time.time()-t_start)}s.", flush=True)

    # ----- BM25 over chunks -----
    print(f"\nBuilding BM25 over chunks → {BM25_OUT_PATH.relative_to(ROOT)}", flush=True)
    texts = [(c.get("text", "") or "") for c in chunks]
    tokenized = [stem_tokenize(t) for t in texts]
    bm25 = BM25Okapi(tokenized)
    BM25_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BM25_OUT_PATH, "wb") as f:
        # Match the schema the retriever's load_bm25 expects: it pickles a
        # dict with keys {"bm25": bm25_obj, "propositions": list_of_records}.
        # We store chunk records under the same key so the retriever can read
        # this file with zero changes once the path is configured.
        pickle.dump({"bm25": bm25, "propositions": chunks}, f)
    print(f"  BM25 saved ({len(chunks)} chunks).")

    print("\nALL DONE.")
    print(f"  Qdrant collection : {args.collection}  ({upserted} points)")
    print(f"  BM25 index file   : {BM25_OUT_PATH.relative_to(ROOT)}")
    print(f"  To use, point retriever at this collection + BM25 path "
          f"(see retriever wiring; cfg.memory.kb_collection / cfg.paths).")


if __name__ == "__main__":
    main()
