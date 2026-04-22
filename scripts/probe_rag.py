"""
scripts/probe_rag.py
---------------------
Diverse-query empirical RAG probe. Measures hit-on-target across categories
including canonical, conversational, misspellings, synonyms, cross-chapter,
OT-clinical, and irrelevant queries.

Run: .venv/bin/python scripts/probe_rag.py
"""
from dotenv import load_dotenv
from pathlib import Path
import sys

load_dotenv(Path(__file__).parent.parent / ".env", override=True)
sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.retriever import Retriever
from tools.mcp_tools import search_textbook


QUERIES = [
    ("canonical", "What nerve innervates the deltoid muscle?", "axillary"),
    ("canonical", "Which muscle initiates shoulder abduction?", "supraspinatus"),
    ("canonical", "What nerve is damaged in wrist drop?", "radial"),
    ("canonical", "Which nerve passes through the carpal tunnel?", "median"),
    ("canonical", "Which artery supplies the anterior wall of the heart?", "left anterior descending"),
    ("conversational", "why do I get a dead arm after a shoulder dislocation?", "axillary"),
    ("conversational", "what is the long thing bone in the upper arm", "humerus"),
    ("informal", "the nerve that controls bicep flexion", "musculocutaneous"),
    ("misspelling", "axillery nerve", "axillary"),
    ("misspelling", "supraspinatous muscle", "supraspinatus"),
    ("synonym", "cranial nerve X", "vagus"),
    ("synonym", "CN VII", "facial"),
    ("cross_chapter", "how does a concussion relate to the meninges?", "meninges"),
    ("cross_chapter", "relationship between bone marrow and immune cells", "hematopoiesis"),
    ("OT_clinical", "what muscle is tested in the empty can test?", "supraspinatus"),
    ("OT_clinical", "rotator cuff injury and sleep pain", "rotator cuff"),
    ("irrelevant", "best pizza in buffalo", ""),
    ("irrelevant", "what is the capital of France?", ""),
]


def main():
    r = Retriever()
    import time
    results = []
    for cat, q, expected_kw in QUERIES:
        t0 = time.time()
        try:
            chunks = search_textbook(q, r)
        except Exception as e:
            print(f"[{cat}] {q!r} ERROR: {e}")
            continue
        ms = int((time.time() - t0) * 1000)
        if not chunks:
            hit = "correctly_empty" if expected_kw == "" else "empty"
            snippet = ""
        else:
            joined = " ".join(c.get("text", "") for c in chunks).lower()
            if expected_kw == "":
                hit = "RETURNED_DESPITE_OOD"
            else:
                hit = "yes" if expected_kw.lower() in joined else "no"
            snippet = chunks[0].get("text", "")[:120]
        results.append((cat, q, expected_kw, hit, len(chunks), ms, snippet))

    print(f"\n{'cat':15} {'hit':22} {'n':>3} {'ms':>5}  query / expected")
    for cat, q, ek, hit, n, ms, sn in results:
        print(f"{cat:15} {hit:22} {n:>3} {ms:>5}  {q!r} | expect={ek!r}")
        if hit == "no":
            print(f"              TOP: {sn!r}")

    print("\n=== SUMMARY ===")
    by_cat = {}
    for cat, q, ek, hit, n, ms, sn in results:
        by_cat.setdefault(cat, []).append(hit)
    total_rel = 0
    total_rel_hit = 0
    for cat, hits in by_cat.items():
        n = len(hits)
        yes = sum(1 for h in hits if h == "yes")
        empty = sum(1 for h in hits if "correctly_empty" in h)
        other = n - yes - empty
        print(f"  {cat:15} hit={yes}/{n}  correctly_empty={empty}/{n}  miss={other}/{n}")
        if cat != "irrelevant":
            total_rel += n
            total_rel_hit += yes
    print(f"\n  RELEVANT-query Hit@top-k: {total_rel_hit}/{total_rel} = "
          f"{100*total_rel_hit/max(total_rel,1):.1f}%")


if __name__ == "__main__":
    main()
