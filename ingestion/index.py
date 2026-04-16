"""
ingestion/index.py
------------------
Step 4 of the ingestion pipeline. Run this AFTER inspecting chunks.jsonl
and propositions.jsonl and confirming they look correct.

Builds two indexes from two sources:

SOURCE 1 — Textbook propositions (from propositions_ot.jsonl)
SOURCE 2 — Diagram metadata JSONs (from data/diagrams/*.json)

Both sources are indexed together into the same Qdrant collection (sokratic_kb)
and the same BM25 index. element_type field distinguishes them:
  - "paragraph" | "table" | "figure_caption" → textbook
  - "diagram"                                 → scraped diagram metadata

1. Qdrant (sokratic_kb collection)
   - Embeds each proposition with text-embedding-3-large
   - Upserts vector + full payload into Qdrant
   - Payload: {text, parent_chunk_id, parent_chunk_text, chapter_title,
               section_title, subsection_title, page, domain, element_type,
               image_filename (diagram only)}
   - domain field ("ot" or "physics") allows filtering at query time

2. BM25 (BM25Okapi from rank_bm25)
   - Tokenized proposition text from both sources
   - Saved locally to data/indexes/bm25_ot.pkl

Qdrant must be running before indexing:
    docker run -p 6333:6333 qdrant/qdrant

Usage:
    python -m ingestion.index
"""

from pathlib import Path


# TODO: load propositions from cfg.paths.propositions_ot
# TODO: load diagram propositions from index_diagrams()
# TODO: combine both lists → single propositions list
# TODO: embed all proposition texts with text-embedding-3-large in batches
# TODO: upsert to Qdrant sokratic_kb (create collection if not exists, vector_size=3072)
# TODO: each Qdrant point: id=uuid, vector=embedding, payload=full proposition dict + domain
# TODO: build BM25Okapi on tokenized proposition texts, pickle to data/indexes/bm25_ot.pkl
# NOTE: parent_chunk_text goes in Qdrant payload so retriever can expand proposition→chunk
#       without a separate lookup file


def build_indexes(propositions: list[dict], domain: str = "ot") -> None:
    """
    Embed propositions, upsert into Qdrant, and build local BM25 index.

    Args:
        propositions: Combined list from textbook + diagrams
        domain:       "ot" or "physics" — stored as payload field for filtering
    """
    raise NotImplementedError


def index_diagrams(diagrams_dir: str = "data/diagrams") -> list[dict]:
    """
    Convert scraped diagram metadata JSONs into propositions for indexing.

    Each diagram JSON has structure:
        {
          "filename": "shoulder_rotator_cuff.png",
          "structures": ["Supraspinatus", "Infraspinatus"],
          "actions": {"Supraspinatus": "initiates abduction", ...},
          "innervation": {"Supraspinatus": "suprascapular nerve", ...},
          "chapter": "Chapter 11",
          "section": "11.6"
        }

    Each structure entry becomes one proposition, e.g.:
        "The Supraspinatus initiates abduction and is innervated by
         the suprascapular nerve."

    Each proposition gets:
        element_type = "diagram"
        image_filename = "shoulder_rotator_cuff.png"  ← so UI can display the image
        parent_chunk_id = diagram filename (no parent chunk — diagram is its own source)
        parent_chunk_text = full JSON stringified (for cross-encoder context)

    Returns:
        List of proposition dicts ready for build_indexes()
    """
    # TODO: glob data/diagrams/*.json
    # TODO: for each JSON: iterate structures, build one proposition sentence per structure
    # TODO: tag with element_type="diagram", image_filename, chapter, section
    raise NotImplementedError


def load_bm25(bm25_path: str) -> tuple:
    """
    Load persisted BM25 index and its proposition list from disk.

    Returns:
        (bm25, propositions_list)
    """
    raise NotImplementedError


if __name__ == "__main__":
    import json
    from config import cfg

    # Load textbook propositions
    propositions = []
    with open(cfg.paths.propositions_ot) as f:
        for line in f:
            propositions.append(json.loads(line))

    # Load diagram propositions and combine
    diagram_propositions = index_diagrams("data/diagrams")
    all_propositions = propositions + diagram_propositions

    print(f"Textbook propositions: {len(propositions)}")
    print(f"Diagram propositions:  {len(diagram_propositions)}")
    print(f"Total:                 {len(all_propositions)}")

    build_indexes(all_propositions, domain="ot")
    print(f"Indexes built.")
