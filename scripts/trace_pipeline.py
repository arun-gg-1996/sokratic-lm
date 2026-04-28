"""
scripts/trace_pipeline.py
-------------------------
Per-query candidate-pool tracer. For each chosen query:
  1) Resolve the *answer-rich* chunk_ids in the corpus (via content-token
     overlap with expected_answer >= 0.60).
  2) Run the retriever's internal stages directly:
       - dense top-N qdrant  (with full top-N — wider than retrieval default)
       - BM25 top-N
       - RRF merge of the two
       - parent-chunk expansion
       - cross-encoder rerank
  3) Report whether/at what rank the answer-rich chunks appear at each stage.

Output identifies WHERE in the pipeline the right chunk gets lost.

Usage:
  cd /Users/arun-ghontale/UB/NLP/sokratic
  .venv/bin/python scripts/trace_pipeline.py --n 10
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
from config import cfg  # noqa: E402

LEGACY = ROOT / "data/eval/rag_qa_expanded.jsonl"
CHUNKS = ROOT / "data/processed/chunks_openstax_anatomy.jsonl"

_STOP = set("""
a an the of in on at by for to from with as is are was were be been being
this that these those it its and or but if then so than such which who whom
whose what when where why how do does did doing done has have had having not
no nor any all some most more less few many one two three first second into
onto out up down off over under again further also already still very their
them they we us our you your he she his her this can will would should could
may might must i me my mine
""".split())


def normalize(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def ctok(s):
    return [t for t in normalize(s).split() if t not in _STOP and len(t) > 2]


def overlap(answer, text):
    a = ctok(answer)
    if not a:
        return 0.0
    t = set(ctok(text))
    return sum(1 for x in a if x in t) / len(a)


def parse_legacy_section(s):
    s = re.sub(r"^\d+\.\d+\s+", "", (s or "").strip())
    if " — " in s:
        return s.split(" — ", 1)[0].strip()
    return s


def find_rank(items, key_fn, target_check):
    """Return 1-based rank of first item passing target_check, or -1."""
    for i, x in enumerate(items, 1):
        if target_check(x):
            return i
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--top-n", type=int, default=50,
                    help="how wide to search at each stage")
    args = ap.parse_args()

    print("Loading corpus...", flush=True)
    corpus = []
    with open(CHUNKS) as f:
        for l in f:
            corpus.append(json.loads(l))
    chunks_by_id = {c["chunk_id"]: c for c in corpus}
    print(f"  {len(corpus)} chunks loaded.\n", flush=True)

    rows = [json.loads(l) for l in open(LEGACY)]
    target = [r for r in rows
              if r.get("question_style") == "original"
              and r.get("expected_answer", "").strip()]

    print("Initializing retriever...", flush=True)
    r = Retriever()
    print(f"  ready.\n", flush=True)

    # We want WRONG_CHAPTER failures specifically. Re-run quickly in dry mode
    # to find them.
    failures = []
    for row in target:
        try:
            retrieved = r.retrieve(row["question"])
        except Exception:
            continue
        primaries = [c for c in retrieved if c.get("_window_role", "primary") == "primary"]
        if not primaries:
            continue
        expected_chap = row.get("chapter_num")
        chap_hit = any(p.get("chapter_num") == expected_chap for p in primaries)
        ans = row["expected_answer"]
        # Compute window-aware overlap to detect answer hits
        nbrs = {}
        for c in retrieved:
            if c.get("_window_role", "primary") != "primary":
                nbrs.setdefault(c.get("_primary_chunk_id", ""), []).append(c)
        ans_hit = False
        for p in primaries:
            combined = p.get("text", "")
            for nb in nbrs.get(p.get("chunk_id", ""), []):
                combined += " " + nb.get("text", "")
            if overlap(ans, combined) >= 0.60:
                ans_hit = True
                break
        if not ans_hit and not chap_hit:
            failures.append(row)
        if len(failures) >= args.n:
            break

    print(f"=== Tracing {len(failures)} WRONG_CHAPTER failures ===\n", flush=True)

    # Pull a wider candidate pool than runtime defaults so we can see if the
    # right chunks are anywhere in the dense / BM25 ranking at all.
    domain = r.default_domain
    N = args.top_n

    for idx, row in enumerate(failures, 1):
        q = row["question"]
        ans = row["expected_answer"]
        expected_chap = row.get("chapter_num")
        expected_sec = parse_legacy_section(row.get("section_title", ""))

        # Find answer-rich chunks (>=0.60 overlap with expected_answer).
        gold_chunks = [c for c in corpus if overlap(ans, c.get("text", "")) >= 0.60]
        gold_ids = set(c["chunk_id"] for c in gold_chunks)

        # Run pipeline stages
        eq = r._apply_ontology_expansion(q)
        eq = r._apply_query_aliases(eq)
        vec = r._embed_query(eq)
        qdrant_hits = r._qdrant_search(vec, top_k=N, domain=domain)
        bm25_hits = r._bm25_search(eq, top_k=N)
        merged_qdrant = r._dedupe_hits_keep_best_rank(qdrant_hits, [])
        rrf_merged = r._rrf_merge(merged_qdrant, bm25_hits, k=int(cfg.retrieval.rrf_k))
        parent_chunks = r._expand_to_parent_chunks(rrf_merged)
        reranked = r._cross_encoder_rerank(eq, parent_chunks)

        # For each stage, item is dict with "chunk_id". Stage finds:
        # qdrant/bm25/rrf items have chunk_id from propositions, parent has
        # chunk_id from chunk-level. The gold check should match by chunk_id
        # presence in either.
        def has_gold(items):
            for x in items:
                cid = x.get("chunk_id", "")
                if cid in gold_ids:
                    return True
            return False

        def first_gold_rank(items):
            for i, x in enumerate(items, 1):
                if x.get("chunk_id", "") in gold_ids:
                    return i
            return -1

        n_gold_qd = sum(1 for x in qdrant_hits if x.get("chunk_id", "") in gold_ids)
        n_gold_bm = sum(1 for x in bm25_hits if x.get("chunk_id", "") in gold_ids)
        n_gold_pc = sum(1 for x in parent_chunks if x.get("chunk_id", "") in gold_ids)

        print(f"\n--- [{idx}] ---")
        print(f"  Q: {q[:90]}")
        print(f"  expected ch{expected_chap} | sec={expected_sec!r}")
        print(f"  gold answer-rich chunks in corpus: {len(gold_ids)}")
        if gold_chunks:
            sample = gold_chunks[0]
            print(f"    e.g. ch{sample.get('chapter_num')} | "
                  f"{sample.get('section_title','')[:40]} | "
                  f"{sample.get('subsection_title','')[:30]}  ({sample['chunk_id'][:8]}…)")

        print(f"  --- stage analysis (top-{N} where applicable) ---")
        print(f"    qdrant top-{N}     gold present: {n_gold_qd:>2}  first rank: {first_gold_rank(qdrant_hits):>3}")
        print(f"    bm25   top-{N}     gold present: {n_gold_bm:>2}  first rank: {first_gold_rank(bm25_hits):>3}")
        print(f"    parent_chunks      gold present: {n_gold_pc:>2}  first rank: {first_gold_rank(parent_chunks):>3} "
              f"(out of {len(parent_chunks)})")
        print(f"    after CE rerank    gold present: {sum(1 for x in reranked if x.get('chunk_id','') in gold_ids):>2}  "
              f"first rank: {first_gold_rank(reranked):>3} (out of {len(reranked)})")

        # Diagnose: where did it get lost?
        if n_gold_qd == 0 and n_gold_bm == 0:
            verdict = "LOST_AT_RETRIEVAL — neither Qdrant nor BM25 ranks it in top-{}".format(N)
        elif n_gold_pc == 0:
            verdict = f"LOST_AT_PARENT_EXPANSION — present in raw stage but parent_chunks dropped it"
        elif first_gold_rank(reranked) > 5:
            verdict = f"LOST_AT_CE_RERANK — was rank {first_gold_rank(parent_chunks)} pre-CE, now rank {first_gold_rank(reranked)}"
        else:
            verdict = "RECOVERED at top-5 after rerank? (shouldn't be a failure)"
        print(f"    >> verdict: {verdict}")


if __name__ == "__main__":
    main()
