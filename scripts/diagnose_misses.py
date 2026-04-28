"""
scripts/diagnose_misses.py
--------------------------
Qualitative audit: pick failed `original`-style queries from the legacy set,
look at what the v4 retriever returned, and look at where the answer text
ACTUALLY lives in the corpus. Surfaces the failure mode per row:

  - WRONG_AREA: retrieved chunks were in the wrong chapter/section, but the
    answer's distinctive phrase exists somewhere in the corpus.
  - SPLIT_ANSWER: retrieved the right area, but the answer text is split
    across multiple new chunks, none of which clear the 60% threshold alone.
  - NOT_IN_CORPUS: the expected_answer's distinctive phrase doesn't appear
    in the new corpus at all (test-set/source artifact).
  - CE_DEMOTION: the right chunk was in the candidate pool (qdrant or BM25
    top-k) but cross-encoder ranked it below the cutoff.
  - WINDOW_MISSED: right chunk would have been included via window expansion
    but isn't because it's not adjacent to a primary.

Usage:
  cd /Users/arun-ghontale/UB/NLP/sokratic
  .venv/bin/python scripts/diagnose_misses.py --n 12
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from retrieval.retriever import Retriever  # noqa: E402

LEGACY = ROOT / "data/eval/rag_qa_expanded.jsonl"
CHUNKS = ROOT / "data/processed/chunks_openstax_anatomy.jsonl"


def normalize(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_STOP = set("""
a an the of in on at by for to from with as is are was were be been being
this that these those it its and or but if then so than such which who whom
whose what when where why how do does did doing done has have had having not
no nor any all some most more less few many one two three first second into
onto out up down off over under again further also already still very their
them they we us our you your he she his her this can will would should could
may might must i me my mine
""".split())


def content_tokens(s: str) -> list[str]:
    return [t for t in normalize(s).split() if t not in _STOP and len(t) > 2]


def overlap(answer: str, text: str) -> float:
    ans = content_tokens(answer)
    if not ans:
        return 0.0
    txt = set(content_tokens(text))
    return sum(1 for t in ans if t in txt) / len(ans)


def parse_legacy_section(s: str) -> str:
    s = re.sub(r"^\d+\.\d+\s+", "", (s or "").strip())
    if " — " in s:
        return s.split(" — ", 1)[0].strip()
    return s


def load_corpus():
    chunks = []
    with open(CHUNKS) as f:
        for l in f:
            chunks.append(json.loads(l))
    return chunks


def find_answer_chunks(corpus: list[dict], answer: str, top: int = 5) -> list[dict]:
    """Find chunks in the corpus whose text most overlaps the answer."""
    scored = []
    for c in corpus:
        ov = overlap(answer, c.get("text", ""))
        if ov > 0:
            scored.append((ov, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top]]


def classify(row: dict, retrieved: list[dict], answer_chunks: list[dict]) -> str:
    """Bucket the failure mode."""
    expected_chap = row.get("chapter_num")
    expected_sec = parse_legacy_section(row.get("section_title", "")).lower()
    answer = row.get("expected_answer", "")
    if not answer.strip():
        return "NO_ANSWER_LABEL"

    # Did the retriever return ANY chunk from the expected chapter?
    chap_hit = any(c.get("chapter_num") == expected_chap for c in retrieved)
    sec_hit = any(c.get("section_title", "").lower() == expected_sec for c in retrieved)

    # Does the answer text exist in the corpus at all?
    if not answer_chunks:
        return "NOT_IN_CORPUS"
    top_corpus_overlap = overlap(answer, answer_chunks[0].get("text", ""))

    # Does any retrieved chunk overlap >=0.40 (looser than headline metric)?
    best_retrieved_overlap = max(
        (overlap(answer, c.get("text", "")) for c in retrieved), default=0.0
    )

    if chap_hit and sec_hit and best_retrieved_overlap < 0.60 and top_corpus_overlap >= 0.60:
        return "SPLIT_ANSWER"  # right area but answer text scattered across chunks
    if chap_hit and not sec_hit:
        return "WRONG_SECTION"
    if not chap_hit and top_corpus_overlap >= 0.60:
        # Right answer EXISTS, retriever just landed in wrong chapter
        return "WRONG_CHAPTER"
    return "OTHER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12, help="how many failures to show")
    ap.add_argument("--style", default="original")
    args = ap.parse_args()

    print("Loading corpus...", flush=True)
    corpus = load_corpus()
    print(f"  {len(corpus)} chunks loaded.")
    rows = [json.loads(l) for l in open(LEGACY)]
    target = [r for r in rows
              if r.get("question_style") == args.style
              and r.get("expected_answer", "").strip()]
    print(f"  {len(target)} {args.style!r}-style rows with non-empty expected_answer\n",
          flush=True)

    print("Initializing retriever...", flush=True)
    retriever = Retriever()
    print(f"  ready (domain={retriever.default_domain})\n", flush=True)

    failures: list[tuple[dict, list[dict], list[dict], str]] = []
    bucket_counts: dict[str, int] = {}

    for i, row in enumerate(target, 1):
        try:
            retrieved = retriever.retrieve(row["question"])
        except Exception as e:
            print(f"  [{i:>3}] ERROR: {e}", flush=True)
            continue
        # Was the answer text found in retrieved (window-aware: pool the
        # primary + neighbors per group)?
        primaries = [c for c in retrieved if c.get("_window_role", "primary") == "primary"]
        neighbors = {c.get("_primary_chunk_id", ""): [] for c in retrieved}
        for c in retrieved:
            if c.get("_window_role", "primary") != "primary":
                neighbors.setdefault(c.get("_primary_chunk_id", ""), []).append(c)

        answer = row.get("expected_answer", "")
        any_hit = False
        for p in primaries:
            combined = p.get("text", "")
            for nb in neighbors.get(p.get("chunk_id", ""), []):
                combined += " " + nb.get("text", "")
            if overlap(answer, combined) >= 0.60:
                any_hit = True
                break

        if any_hit:
            continue

        # This is a failure — analyze it.
        ans_chunks = find_answer_chunks(corpus, answer, top=5)
        bucket = classify(row, retrieved, ans_chunks)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        failures.append((row, retrieved, ans_chunks, bucket))

        if i % 20 == 0:
            print(f"  [{i}/{len(target)}] failures so far: {len(failures)}", flush=True)

    # Summary
    print("\n" + "=" * 90)
    print(f"FAILURES on {args.style!r}-style rows: {len(failures)} / {len(target)}")
    print("=" * 90)
    print("\nBy failure bucket:")
    for k, v in sorted(bucket_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<22} {v:>3}  ({v/len(target)*100:.1f}% of {args.style!r})")

    # Show first N examples per bucket — most-informative is one of each
    print(f"\n--- Sample failures (up to {args.n} total) ---")
    shown = 0
    for bucket in ["WRONG_CHAPTER", "WRONG_SECTION", "SPLIT_ANSWER", "NOT_IN_CORPUS", "OTHER"]:
        if shown >= args.n:
            break
        bucket_failures = [f for f in failures if f[3] == bucket]
        for f in bucket_failures[: max(1, args.n // 5)]:
            row, retrieved, ans_chunks, b = f
            print(f"\n--- [{b}] ---")
            print(f"  Q: {row['question']}")
            print(f"  expected ch{row.get('chapter_num')} | sec={parse_legacy_section(row.get('section_title',''))!r}")
            ans = row.get("expected_answer", "")
            print(f"  expected_answer: {ans[:160]}")
            primaries = [c for c in retrieved if c.get("_window_role", "primary") == "primary"]
            print(f"  retrieved primaries (top {len(primaries)}):")
            for j, p in enumerate(primaries, 1):
                print(f"    {j}. ch{p.get('chapter_num')} | "
                      f"{p.get('section_title','')[:35]} | "
                      f"{p.get('subsection_title','')[:30]}")
            print(f"  answer-rich chunks in corpus (top 3 by overlap):")
            for j, c in enumerate(ans_chunks[:3], 1):
                ov = overlap(ans, c.get("text", ""))
                print(f"    {j}. ch{c.get('chapter_num')} | "
                      f"{c.get('section_title','')[:35]} | "
                      f"{c.get('subsection_title','')[:30]} "
                      f"(overlap={ov:.2f})")
            shown += 1


if __name__ == "__main__":
    main()
