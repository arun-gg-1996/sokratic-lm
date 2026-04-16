"""
ingestion/chunk.py
------------------
Step 2 of the ingestion pipeline.

Takes raw extracted elements and produces semantic chunks using
LlamaIndex SemanticSplitterNodeParser.

How it works:
  - Sentences that are semantically close are grouped together.
  - A split happens where cosine similarity drops below the 75th percentile.
  - Each chunk inherits the metadata of its source element
    (chapter_num, chapter_title, section_title, subsection_title, page).

Target: 1,500–2,000 chunks of 600–900 characters each.

Output saved to cfg.paths.chunks_ot as JSONL for human inspection BEFORE indexing.

Each JSONL line:
{
  "chunk_id": "uuid",
  "text": "...",
  "chapter_num": 11,
  "chapter_title": "Muscle Tissue",
  "section_num": "11.2",
  "section_title": "Naming Skeletal Muscles",
  "subsection_title": "Deltoid",
  "page": 412
}

Usage:
    python -m ingestion.chunk
"""

# TODO: use LlamaIndex SemanticSplitterNodeParser with text-embedding-3-small
# TODO: set breakpoint_percentile_threshold=75
# TODO: assign a uuid chunk_id to each chunk
# TODO: propagate metadata from source element to each chunk
# TODO: save to cfg.paths.chunks_ot as JSONL


def chunk_elements(elements: list[dict]) -> list[dict]:
    """
    Semantically chunk a list of raw text elements.

    Args:
        elements: Output of extract.extract_pdf()

    Returns:
        List of chunk dicts with text + metadata + chunk_id.
    """
    raise NotImplementedError


if __name__ == "__main__":
    from config import cfg
    from ingestion.extract import extract_pdf

    elements = extract_pdf(cfg.paths.raw_ot_pdf)
    chunks = chunk_elements(elements)
    print(f"Produced {len(chunks)} chunks")
    # save to JSONL here
