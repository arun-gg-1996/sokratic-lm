"""
evaluation/generate_rag_qa.py
------------------------------
One-time script: generate a RAG validation dataset from the textbook.

What it produces:
    data/eval/rag_qa.jsonl — 100-200 question/answer pairs, each anchored
    to a specific source chunk. Used by tests/test_rag.py to verify that
    retrieval surfaces the right chunk for each question.

How it works:
    1. Load chunks from data/processed/chunks_ot.jsonl
    2. Sample N chunks — spread across chapters (stratified by chapter_title)
       so the eval set covers the whole textbook, not just one section
    3. For each sampled chunk, call GPT-4o with a prompt asking it to generate
       one student-style question and a short expected answer grounded in that chunk
    4. Save each record as:
       {
         "question":        str,   # what a student would ask
         "expected_answer": str,   # short factual answer from the chunk
         "source_chunk_id": str,   # chunk this Q&A was generated from
         "chapter_title":   str,
         "section_title":   str
       }

Hard gate:
    This dataset must exist and have >= 100 records before tests/test_rag.py
    can run. test_rag.py will refuse to run if data/eval/rag_qa.jsonl is missing.

Run once after ingestion is complete:
    python -m evaluation.generate_rag_qa

Cost estimate: ~100-200 GPT-4o calls, each on a short chunk. ~$0.50-1.00 total.
"""

import json
import random
from pathlib import Path
from collections import defaultdict
from config import cfg


# How many Q&A pairs to generate total
N_PAIRS = 150

# GPT-4o prompt for generating one Q&A pair from a chunk
QA_GENERATION_PROMPT = """
You are building a test dataset for a retrieval system. Given the textbook passage below,
generate exactly ONE question that a student studying this topic might ask, and a short
expected answer (1-2 sentences) that is directly and completely answered by the passage.

Rules:
- The question must be answerable ONLY from this passage (no outside knowledge needed).
- The answer must be a direct factual claim from the passage — no inference.
- The question should sound like a student asking during a tutoring session.
- Do not include the answer in the question.

Passage:
{chunk_text}

Respond in JSON:
{{"question": "...", "expected_answer": "..."}}
"""


def load_chunks(path: str) -> list[dict]:
    """Load all chunks from a JSONL file."""
    chunks = []
    with open(path) as f:
        for line in f:
            chunks.append(json.loads(line))
    return chunks


def stratified_sample(chunks: list[dict], n: int) -> list[dict]:
    """
    Sample n chunks spread evenly across chapters.
    If a chapter has fewer chunks than its share, take all of them.
    """
    by_chapter = defaultdict(list)
    for chunk in chunks:
        by_chapter[chunk["chapter_title"]].append(chunk)

    n_chapters = len(by_chapter)
    per_chapter = max(1, n // n_chapters)

    sampled = []
    for chapter_chunks in by_chapter.values():
        sampled.extend(random.sample(chapter_chunks, min(per_chapter, len(chapter_chunks))))

    # trim or top-up to exactly n
    random.shuffle(sampled)
    return sampled[:n]


def generate_qa_for_chunk(chunk: dict, client) -> dict | None:
    """
    Call GPT-4o to generate one Q&A pair for a chunk.
    Returns a record dict or None if generation failed.
    """
    # TODO: call client.chat.completions.create with QA_GENERATION_PROMPT
    # TODO: parse JSON response
    # TODO: return {question, expected_answer, source_chunk_id, chapter_title, section_title}
    raise NotImplementedError


def run():
    """
    Main entry point. Load chunks, sample, generate Q&A pairs, save to JSONL.
    """
    chunks_path = cfg.paths.chunks_ot
    out_path = Path("data/eval/rag_qa.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # TODO: initialize OpenAI client
    # TODO: load chunks
    # TODO: stratified_sample(chunks, N_PAIRS)
    # TODO: for each sampled chunk: generate_qa_for_chunk (with tqdm progress bar)
    # TODO: skip None results (failed generations)
    # TODO: write each record as a JSON line to out_path
    # TODO: print summary: N records written, chapters covered
    raise NotImplementedError


if __name__ == "__main__":
    run()
