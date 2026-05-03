"""
Validation gate — assert all data-prep invariants from docs/AUDIT_2026-05-02.md
are satisfied after the L76 + L19 + L38 + reindex passes.

Invariants checked:

L76 — every chunk has all 3 hierarchy levels (chapter + section + subsection).
L38 — topic_index <-> raptor_subsection_summaries are 1:1 by (chapter, section, subsection).
L19 — every topic_index entry has display_label.
G   — BM25 pickle file exists and loads.
H   — Qdrant collection chunk count matches chunks JSONL count.
TS  — textbook_structure contains every (chapter, section, subsection) referenced
       by chunks (parser + llm_synthesized).
RAG — Smoke retrieval test on a known topic returns >= 1 chunk for each of
       3 canonical anatomy queries.

Exit code 0 = all green. Non-zero = at least one invariant violated.

Usage:
  .venv/bin/python scripts/validate_data_prep_invariants.py [--no-rag] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

CHUNKS_PATH = REPO / "data" / "processed" / "chunks_openstax_anatomy.jsonl"
TOPIC_INDEX_PATH = REPO / "data" / "topic_index.json"
RAPTOR_PATH = REPO / "data" / "artifacts" / "raptor_subsection_summaries.jsonl"
TEXTBOOK_STRUCT_PATH = REPO / "data" / "textbook_structure.json"
BM25_PATH = REPO / "data" / "indexes" / "bm25_chunks_openstax_anatomy.pkl"
CURATED_ABBREVS_PATH = REPO / "data" / "curated_abbrevs_ot.json"

QDRANT_COLLECTION = "sokratic_kb_chunks"

CANONICAL_RAG_QUERIES = [
    "What does the suprascapular nerve supply?",
    "Describe the cardiac cycle",
    "What is the function of the SA node?",
]


class CheckResult:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def __repr__(self):
        return f"{'✓' if self.passed else '✗'} {self.name}: {self.detail}"


def check_l76() -> CheckResult:
    missing = 0
    for line in CHUNKS_PATH.open():
        c = json.loads(line)
        if not (c.get("chapter_title") and c.get("section_title") and c.get("subsection_title")):
            missing += 1
    if missing == 0:
        return CheckResult("L76 — every chunk has full hierarchy", True, "0 chunks orphaned")
    return CheckResult("L76 — every chunk has full hierarchy", False, f"{missing} chunks orphaned")


def _topic_index_keys() -> set[tuple]:
    raw = json.loads(TOPIC_INDEX_PATH.read_text())
    items = raw if isinstance(raw, list) else list(raw.values())
    return {
        (
            x.get("chapter") or x.get("chapter_title") or "",
            x.get("section") or x.get("section_title") or "",
            x.get("subsection") or x.get("subsection_title") or "",
        )
        for x in items
        if isinstance(x, dict)
    }


def _raptor_keys() -> set[tuple]:
    out = set()
    for line in RAPTOR_PATH.open():
        s = json.loads(line)
        out.add(
            (
                s.get("chapter") or s.get("chapter_title") or "",
                s.get("section") or s.get("section_title") or "",
                s.get("subsection") or s.get("subsection_title") or "",
            )
        )
    return out


def check_l38() -> CheckResult:
    ti = _topic_index_keys()
    ra = _raptor_keys()
    missing_summary = ti - ra
    orphan_summary = ra - ti
    if not missing_summary and not orphan_summary:
        return CheckResult("L38 — topic_index ↔ raptor_summary 1:1", True, f"{len(ti)} entries each")
    return CheckResult(
        "L38 — topic_index ↔ raptor_summary 1:1",
        False,
        f"{len(missing_summary)} entries missing summary, {len(orphan_summary)} orphan summaries",
    )


def check_l19() -> CheckResult:
    raw = json.loads(TOPIC_INDEX_PATH.read_text())
    items = raw if isinstance(raw, list) else list(raw.values())
    missing = [x for x in items if isinstance(x, dict) and not x.get("display_label")]
    if not missing:
        return CheckResult("L19 — every topic_index entry has display_label", True, f"{len(items)} labeled")
    return CheckResult(
        "L19 — every topic_index entry has display_label", False, f"{len(missing)} missing labels"
    )


def check_textbook_structure_coverage() -> CheckResult:
    """Every (chapter, section, subsection) referenced by chunks must exist in textbook_structure."""
    structure = json.loads(TEXTBOOK_STRUCT_PATH.read_text())

    # Build a set of all (chapter_title, section_title, subsection_title) present in structure
    in_structure = set()
    for ch_key, ch_node in structure.items():
        # ch_key like "Chapter 10: Muscle Tissue"
        if not isinstance(ch_node, dict):
            continue
        ch_title = ch_key.split(":", 1)[1].strip() if ":" in ch_key else ch_key
        sections = ch_node.get("sections", {})
        if not isinstance(sections, dict):
            continue
        for sec_name, sec_node in sections.items():
            if not isinstance(sec_node, dict):
                continue
            subs = sec_node.get("subsections", {})
            if isinstance(subs, dict):
                for sub_name in subs.keys():
                    in_structure.add((ch_title, sec_name, sub_name))

    chunk_keys = set()
    for line in CHUNKS_PATH.open():
        c = json.loads(line)
        chunk_keys.add(
            (
                c.get("chapter_title") or "",
                c.get("section_title") or "",
                c.get("subsection_title") or "",
            )
        )

    not_in_structure = chunk_keys - in_structure
    if not not_in_structure:
        return CheckResult(
            "TS — textbook_structure contains all chunk hierarchies",
            True,
            f"{len(chunk_keys)} unique chunk hierarchies all present",
        )
    return CheckResult(
        "TS — textbook_structure contains all chunk hierarchies",
        False,
        f"{len(not_in_structure)} chunk hierarchies missing from textbook_structure (sample: {list(not_in_structure)[:2]})",
    )


def check_bm25() -> CheckResult:
    if not BM25_PATH.exists():
        return CheckResult("G — BM25 index file exists", False, f"missing: {BM25_PATH}")
    try:
        with open(BM25_PATH, "rb") as f:
            obj = pickle.load(f)
        size = len(getattr(obj, "doc_freqs", []) or getattr(obj, "corpus_size", 0) or [])
        return CheckResult("G — BM25 index loads", True, f"loaded ({type(obj).__name__})")
    except Exception as e:
        return CheckResult("G — BM25 index loads", False, f"load error: {e}")


def check_qdrant() -> CheckResult:
    try:
        from qdrant_client import QdrantClient

        from config import cfg

        client = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
        info = client.get_collection(QDRANT_COLLECTION)
        qcount = info.points_count
    except Exception as e:
        return CheckResult("H — Qdrant collection accessible", False, f"error: {e}")

    chunk_count = sum(1 for _ in CHUNKS_PATH.open())
    if qcount == chunk_count:
        return CheckResult(
            "H — Qdrant chunk count == JSONL chunk count", True, f"{qcount} == {chunk_count}"
        )
    return CheckResult(
        "H — Qdrant chunk count == JSONL chunk count",
        False,
        f"qdrant has {qcount}, jsonl has {chunk_count}",
    )


def check_curated_abbrevs() -> CheckResult:
    if not CURATED_ABBREVS_PATH.exists():
        return CheckResult("L9 — curated_abbrevs_ot.json exists", False, f"missing: {CURATED_ABBREVS_PATH}")
    try:
        d = json.loads(CURATED_ABBREVS_PATH.read_text())
        n = len(d.get("abbreviations", []))
        if n < 10:
            return CheckResult("L9 — curated_abbrevs_ot.json has entries", False, f"only {n} entries (expected >= 10)")
        return CheckResult("L9 — curated_abbrevs_ot.json valid", True, f"{n} abbreviations")
    except Exception as e:
        return CheckResult("L9 — curated_abbrevs_ot.json parses", False, f"parse error: {e}")


def check_rag_smoke() -> CheckResult:
    """End-to-end retrieval smoke test."""
    try:
        from retrieval.retriever import ChunkRetriever
    except ImportError:
        try:
            from retrieval.retriever import Retriever as ChunkRetriever  # type: ignore
        except ImportError as e:
            return CheckResult("RAG — retriever importable", False, f"import error: {e}")

    try:
        retriever = ChunkRetriever()
    except Exception as e:
        return CheckResult("RAG — retriever instantiable", False, f"init error: {e}")

    failures = []
    for q in CANONICAL_RAG_QUERIES:
        try:
            res = retriever.retrieve(q) if hasattr(retriever, "retrieve") else retriever.query(q)
            if not res:
                failures.append(f"{q!r} -> 0 results")
        except Exception as e:
            failures.append(f"{q!r} -> error: {e}")

    if not failures:
        return CheckResult(
            "RAG — canonical queries all return results", True, f"{len(CANONICAL_RAG_QUERIES)}/{len(CANONICAL_RAG_QUERIES)} returned chunks"
        )
    return CheckResult("RAG — canonical queries all return results", False, "; ".join(failures))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-rag", action="store_true", help="Skip RAG smoke test")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    print("Validating data-prep invariants per docs/AUDIT_2026-05-02.md...\n", flush=True)

    checks = [
        check_l76(),
        check_l38(),
        check_l19(),
        check_textbook_structure_coverage(),
        check_curated_abbrevs(),
        check_bm25(),
        check_qdrant(),
    ]
    if not args.no_rag:
        checks.append(check_rag_smoke())

    for c in checks:
        print(f"  {c}", flush=True)

    failed = [c for c in checks if not c.passed]
    print()
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} invariants violated.", flush=True)
        sys.exit(1)
    print(f"ALL GREEN: {len(checks)}/{len(checks)} invariants satisfied. Data prep complete.", flush=True)


if __name__ == "__main__":
    main()
