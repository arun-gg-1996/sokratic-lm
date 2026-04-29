"""
scripts/validate_topic_index.py
--------------------------------
Post-process `data/topic_index.json` by running real retrieval against every
TOC entry and stamping a `teachable` flag. A card shown to the student is a
promise we can teach — `teachable=False` entries are removed from the card
sampler so we don't surface dead-ends mid-session.

Validation rule (mirrors `conversation.dean._coverage_gate`):
  1. Retrieve with hard section/subsection filter (as run_turn does at lock).
  2. Retrieval must return at least one chunk.
  3. Top chunk score must be >= cfg.retrieval.ood_cosine_threshold.

Query used for each entry: the most specific label (subsection → section →
chapter). This matches the query `_build_retrieval_query` produces when a
student types that exact label.

Run:
    .venv/bin/python -m scripts.validate_topic_index
    .venv/bin/python -m scripts.validate_topic_index --limit 10   # dry smoke test

The script updates `data/topic_index.json` in place, adding/refreshing the
`teachable` field on every entry.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import cfg  # noqa: E402
from retrieval.retriever import Retriever  # noqa: E402

OUT_PATH = ROOT / "data" / "topic_index.json"


def _label_for(entry: dict) -> str:
    return (
        (entry.get("subsection") or "").strip()
        or (entry.get("section") or "").strip()
        or (entry.get("chapter") or "").strip()
    )


def _is_teachable(chunks: list[dict], threshold: float) -> bool:
    if not chunks:
        return False
    top_score = chunks[0].get("score")
    if isinstance(top_score, (int, float)) and float(top_score) < threshold:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Validate only the first N entries (smoke test). 0 = all.",
    )
    args = parser.parse_args()

    entries: list[dict] = json.loads(OUT_PATH.read_text())
    if args.limit > 0:
        entries_to_check = entries[: args.limit]
    else:
        entries_to_check = entries

    threshold = float(getattr(cfg.retrieval, "ood_cosine_threshold", 0.30))
    retriever = Retriever()

    t_start = time.time()
    teachable_count = 0
    for i, entry in enumerate(entries_to_check, start=1):
        label = _label_for(entry)
        locked_section = entry.get("section") or None
        locked_subsection = entry.get("subsection") or None
        try:
            chunks = retriever.retrieve(
                label,
                top_k=5,
                locked_section=locked_section,
                locked_subsection=locked_subsection,
            )
        except Exception as e:
            chunks = []
            entry["teachable_error"] = str(e)[:200]

        teachable = _is_teachable(chunks, threshold)
        entry["teachable"] = teachable
        if teachable:
            teachable_count += 1

        if i % 25 == 0 or i == len(entries_to_check):
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed else 0
            print(
                f"[{i}/{len(entries_to_check)}] teachable={teachable_count} "
                f"elapsed={elapsed:.1f}s ({rate:.1f}/s)"
            )

    OUT_PATH.write_text(json.dumps(entries, indent=2))
    total = len(entries_to_check)
    print(
        f"\nWrote {len(entries)} entries → {OUT_PATH.relative_to(ROOT)}\n"
        f"  validated:  {total}\n"
        f"  teachable:  {teachable_count}\n"
        f"  rejected:   {total - teachable_count}\n"
        f"  threshold:  CE score >= {threshold}"
    )


if __name__ == "__main__":
    main()
