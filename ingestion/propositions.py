"""
ingestion/propositions.py
--------------------------
Step 3 of the ingestion pipeline.

Takes chunks and decomposes each one into atomic single-fact propositions
using GPT-4o (Dense X Retrieval approach).

Why propositions?
  Student queries are short and specific. Propositions match them better
  than full chunks because each proposition is one testable fact.
  At retrieval time, matched propositions expand back to their parent chunk
  to give the model full context.

Each proposition stores a pointer to its parent chunk_id.

Target: ~5,000–8,000 propositions total (~$2 API cost).

Output saved to cfg.paths.propositions_ot as JSONL for human inspection BEFORE indexing.

Each JSONL line:
{
  "proposition_id": "uuid",
  "text": "The deltoid is innervated by the axillary nerve from C5 and C6.",
  "parent_chunk_id": "uuid",
  "chapter_title": "Muscle Tissue",
  "section_title": "Naming Skeletal Muscles",
  "subsection_title": "Deltoid"
}

Usage:
    python -m ingestion.propositions
"""

# TODO: for each chunk, call GPT-4o with the 'proposition_extraction' prompt from config.yaml
# TODO: parse the response into a list of proposition strings
# TODO: assign uuid proposition_id to each
# TODO: attach parent_chunk_id and metadata from the source chunk
# TODO: save to cfg.paths.propositions_ot as JSONL
# TODO: handle API errors and empty responses gracefully


def extract_propositions(chunks: list[dict]) -> list[dict]:
    """
    Decompose each chunk into atomic single-fact propositions via GPT-4o.

    Args:
        chunks: Output of chunk.chunk_elements()

    Returns:
        List of proposition dicts with text + parent_chunk_id + metadata.
    """
    raise NotImplementedError


if __name__ == "__main__":
    import json
    from config import cfg

    # Load chunks from JSONL
    chunks = []
    with open(cfg.paths.chunks_ot) as f:
        for line in f:
            chunks.append(json.loads(line))

    propositions = extract_propositions(chunks)
    print(f"Produced {len(propositions)} propositions from {len(chunks)} chunks")
