"""
scripts/test_chunk_retriever.py
-------------------------------
Quick direct comparison: propositions vs chunks indexing on the two queries
that failed end-to-end in the e2e simulation.

Runs both Retriever (propositions) and ChunkRetriever (chunks) on:
  1. "What nerve innervates the deltoid muscle?"
  2. "Which nerve is damaged in a humeral shaft fracture causing wrist drop?"

Prints the top-5 primaries from each so we can read whether chunks-mode
recovers the expected sections (Ch11/Ch13 for deltoid; Ch11/Ch6/Ch8 for
humeral fracture + radial nerve content).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

import warnings
warnings.filterwarnings("ignore")

from retrieval.retriever import Retriever, ChunkRetriever  # noqa: E402

QUERIES = [
    "What nerve innervates the deltoid muscle?",
    "Which nerve is damaged in a humeral shaft fracture causing wrist drop?",
    # An aggregation query — chunks expected to do better here than propositions.
    "What are the structures included in the anterior muscles?",
    # A relational query that propositions specifically fail on.
    "What does the axillary nerve supply?",
]


def show(label: str, retriever, query: str) -> None:
    chunks = retriever.retrieve(query)
    primaries = [c for c in chunks if c.get("_window_role", "primary") == "primary"]
    print(f"\n  -- {label}: {len(chunks)} returned ({len(primaries)} primaries)")
    for i, c in enumerate(primaries[:5], 1):
        text = (c.get("text", "") or "").replace("\n", " ")[:130]
        print(f"    {i}. ch{c.get('chapter_num')} | "
              f"{c.get('section_title', '')[:35]} | "
              f"{c.get('subsection_title', '')[:30]}")
        print(f"       {text}")


def main():
    print("Loading Retriever (propositions)...", flush=True)
    prop_r = Retriever()
    print("Loading ChunkRetriever (chunks)...", flush=True)
    chunk_r = ChunkRetriever()
    print("\nReady.\n")

    for q in QUERIES:
        print("=" * 90)
        print(f"Q: {q}")
        print("=" * 90)
        show("PROPOSITIONS (current)", prop_r, q)
        show("CHUNKS (new)", chunk_r, q)


if __name__ == "__main__":
    main()
