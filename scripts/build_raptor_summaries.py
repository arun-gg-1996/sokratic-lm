"""
scripts/build_raptor_summaries.py
---------------------------------
Build mid-level summaries (subsection + section level) and add them to the
Qdrant chunks index, so aggregation queries ("list the X", "what are the
types of Y") have a retrievable handle that matches their conceptual
granularity.

Why
---
Chunks-mode retrieval (see reindex_chunks.py) recovers most relational
queries that the proposition pipeline broke. But "list-the-Xs" / aggregation
queries still need an item that explicitly aggregates the X1..Xn — a single
chunk rarely contains that aggregation. RAPTOR (Sarthi et al, ICLR 2024)
addresses this by recursively summarizing into a tree. Our textbook already
has explicit (chapter, section, subsection) hierarchy — so we summarize
along that hierarchy directly.

What this does
--------------
  1. Group chunks_openstax_anatomy.jsonl by (chapter, section, subsection).
  2. For each group, ask Claude Haiku 4.5 to produce a 4-6 sentence
     subsection summary that mentions the key entities/concepts the textbook
     introduces in that subsection.
  3. Same at (chapter, section) level — coarser handle for "give me an
     overview of section X" queries.
  4. Embed each summary via OpenAI text-embedding-3-large.
  5. Upsert into the SAME Qdrant collection as chunks (sokratic_kb_chunks),
     payload-marked with indexing_unit="subsection_summary" or
     "section_summary" so the retriever can blend or filter.
  6. Append summaries to the chunks BM25 pickle so the BM25 leg sees them
     too (retriever's BM25 is keyed by index, not chunk_id, so order is
     stable).

Cost
----
  - 549 subsection + 170 section = ~720 Haiku calls (~$0.30-1.00 in tokens)
  - 720 OpenAI embeddings (~$0.10)
  - ~5-10 min wall with concurrency=8

Usage
-----
  cd /Users/arun-ghontale/UB/NLP/sokratic
  .venv/bin/python scripts/build_raptor_summaries.py
  .venv/bin/python scripts/build_raptor_summaries.py --limit 5  # smoke test
  .venv/bin/python scripts/build_raptor_summaries.py --section-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from anthropic import AsyncAnthropic  # noqa: E402
from openai import OpenAI  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.http.models import PointStruct  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

from config import cfg  # noqa: E402
from ingestion.core.index import stem_tokenize  # noqa: E402

CHUNKS_PATH = ROOT / "data/processed/chunks_openstax_anatomy.jsonl"
BM25_PATH = ROOT / "data/indexes/bm25_chunks_openstax_anatomy.pkl"
EMBED_MODEL = "text-embedding-3-large"
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
DOMAIN = "openstax_anatomy"
COLLECTION = "sokratic_kb_chunks"
EMBED_BATCH_SIZE = 100
UPSERT_BATCH_SIZE = 100
SEMAPHORE = 8       # parallel Anthropic calls
MAX_INPUT_TOKENS = 80_000  # safety; chapters fit in Haiku's 200k context

SUMMARY_PROMPT_SUBSECTION = """\
You are summarizing a subsection of an undergraduate human anatomy and
physiology textbook for a retrieval index. Produce a 4-6 sentence summary
that:

  - Names the key anatomical structures, processes, or concepts the
    subsection introduces.
  - Lists the entities a student would search for to find this content
    (use the exact terminology the textbook uses, not paraphrases).
  - Captures any relationships the textbook explicitly states ("X innervates
    Y", "A causes B", "in P, structure Q connects to R").
  - Stays grounded — only summarize what the source states; do not infer
    or add outside knowledge.

The summary will be embedded and used as a retrieval target, so favor
concrete entity-mention coverage over prose flow.

CHAPTER {chapter_num}: {chapter_title}
SECTION: {section_title}
SUBSECTION: {subsection_title}

SOURCE CHUNKS (concatenated):
{source}

Write only the summary, no preamble."""

SUMMARY_PROMPT_SECTION = """\
You are summarizing a SECTION of an undergraduate human anatomy and
physiology textbook for a retrieval index. Produce a 6-10 sentence
summary that lists the topics covered in each subsection within the
section, naming key structures and relationships. Use the textbook's
exact terminology. Stay grounded in the source.

CHAPTER {chapter_num}: {chapter_title}
SECTION: {section_title}

SOURCE (subsection summaries already produced for this section):
{source}

Write only the summary, no preamble."""


def load_chunks(path: Path) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def group_by_subsection(chunks: list[dict]) -> dict[tuple, list[dict]]:
    g: dict[tuple, list[dict]] = defaultdict(list)
    for c in chunks:
        g[(c.get("chapter_num"), c.get("section_title", ""), c.get("subsection_title", ""))].append(c)
    return g


def group_by_section(chunks: list[dict]) -> dict[tuple, list[dict]]:
    g: dict[tuple, list[dict]] = defaultdict(list)
    for c in chunks:
        g[(c.get("chapter_num"), c.get("section_title", ""))].append(c)
    return g


async def summarize_one(
    sem: asyncio.Semaphore,
    client: AsyncAnthropic,
    prompt: str,
    label: str,
) -> tuple[str, str | None]:
    async with sem:
        try:
            resp = await client.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=420,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = ""
            for block in resp.content:
                t = getattr(block, "text", "") or ""
                if t:
                    text += t
            return label, text.strip()
        except Exception as e:
            return label, f"[ERROR: {type(e).__name__}: {e}]"


def truncate_tokens_approx(text: str, max_chars: int = 320_000) -> str:
    """Very rough — Haiku 4-5 has 200k context, ~4 chars/token. Cap at
    320k chars (~80k tokens) to leave room for prompt + output."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... TRUNCATED ...]"


async def build_subsection_summaries(
    groups: dict[tuple, list[dict]],
    *,
    limit: int | None = None,
) -> list[dict]:
    """Returns list of summary dicts: {chapter_num, section_title,
    subsection_title, summary, n_source_chunks, group_chunk_ids}."""
    items = list(groups.items())
    if limit:
        items = items[:limit]
    client = AsyncAnthropic()
    sem = asyncio.Semaphore(SEMAPHORE)
    tasks = []
    metas = []
    for (ch, sec, sub), chs in items:
        chapter_title = chs[0].get("chapter_title", "")
        source_text = "\n\n---\n\n".join(c.get("text", "") for c in chs)
        source_text = truncate_tokens_approx(source_text)
        prompt = SUMMARY_PROMPT_SUBSECTION.format(
            chapter_num=ch, chapter_title=chapter_title,
            section_title=sec, subsection_title=sub or "(intro)",
            source=source_text,
        )
        tasks.append(summarize_one(sem, client, prompt,
                                   label=f"{ch}|{sec}|{sub}"))
        metas.append({
            "chapter_num": ch,
            "chapter_title": chapter_title,
            "section_title": sec,
            "subsection_title": sub,
            "n_source_chunks": len(chs),
            "group_chunk_ids": [c.get("chunk_id") for c in chs],
        })

    results: list[dict] = []
    done = 0
    t0 = time.time()
    for fut in asyncio.as_completed(tasks):
        label, text = await fut
        # Match label back to its meta — we walk metas in label order; not
        # ideal but tasks were created in metas order so as_completed losing
        # order is the only problem. Use a label index.
        done += 1
        if done % 20 == 0 or done == len(tasks):
            print(f"  subsection summaries: {done}/{len(tasks)} ({int(time.time()-t0)}s)",
                  flush=True)
        # Find meta by label
        ch_s, sec_s, sub_s = label.split("|", 2)
        for m in metas:
            if (str(m["chapter_num"]) == ch_s
                and m["section_title"] == sec_s
                and m["subsection_title"] == sub_s):
                results.append({**m, "summary": text})
                break
    return results


async def build_section_summaries(
    section_groups: dict[tuple, list[dict]],
    subsection_summaries: list[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    items = list(section_groups.items())
    if limit:
        items = items[:limit]
    # Index subsection summaries by (chapter, section)
    subs_by_section: dict[tuple, list[dict]] = defaultdict(list)
    for s in subsection_summaries:
        subs_by_section[(s["chapter_num"], s["section_title"])].append(s)

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(SEMAPHORE)
    tasks = []
    metas = []
    for (ch, sec), chs in items:
        chapter_title = chs[0].get("chapter_title", "")
        sub_summs = subs_by_section.get((ch, sec), [])
        if not sub_summs:
            continue
        # Source = subsection summaries' text
        source_text = "\n\n".join(
            f"[{s.get('subsection_title') or '(intro)'}] {s.get('summary','')}"
            for s in sub_summs
        )
        source_text = truncate_tokens_approx(source_text)
        prompt = SUMMARY_PROMPT_SECTION.format(
            chapter_num=ch, chapter_title=chapter_title,
            section_title=sec, source=source_text,
        )
        tasks.append(summarize_one(sem, client, prompt,
                                   label=f"{ch}|{sec}"))
        metas.append({
            "chapter_num": ch,
            "chapter_title": chapter_title,
            "section_title": sec,
            "subsection_title": "",
            "n_source_subsections": len(sub_summs),
        })

    results: list[dict] = []
    done = 0
    t0 = time.time()
    for fut in asyncio.as_completed(tasks):
        label, text = await fut
        done += 1
        if done % 20 == 0 or done == len(tasks):
            print(f"  section summaries: {done}/{len(tasks)} ({int(time.time()-t0)}s)",
                  flush=True)
        ch_s, sec_s = label.split("|", 1)
        for m in metas:
            if (str(m["chapter_num"]) == ch_s and m["section_title"] == sec_s):
                results.append({**m, "summary": text})
                break
    return results


def upsert_summaries(qdrant: QdrantClient, openai: OpenAI,
                     summaries: list[dict], unit_label: str) -> int:
    if not summaries:
        return 0
    texts = [s["summary"] for s in summaries]
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        # Replace empty with a space — OpenAI rejects empty input.
        batch = [t if (t and t.strip()) else " " for t in batch]
        resp = openai.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend([d.embedding for d in resp.data])

    points: list[PointStruct] = []
    for s, vec in zip(summaries, vectors):
        # Synthetic id — UUID prefix tag so it can't collide with chunk ids.
        pid = str(uuid.uuid5(uuid.NAMESPACE_OID,
                              f"{unit_label}|{s['chapter_num']}|"
                              f"{s['section_title']}|{s.get('subsection_title','')}"))
        payload = {
            "chunk_id": pid,
            "text": s["summary"],
            "domain": DOMAIN,
            "textbook_id": DOMAIN,
            "chapter_num": s["chapter_num"],
            "chapter_title": s["chapter_title"],
            "section_title": s["section_title"],
            "subsection_title": s.get("subsection_title", ""),
            "page": 0,
            "element_type": "paragraph",
            "indexing_unit": unit_label,  # "subsection_summary" or "section_summary"
            "n_source_chunks": s.get("n_source_chunks", 0),
            "n_source_subsections": s.get("n_source_subsections", 0),
        }
        points.append(PointStruct(id=pid, vector=vec, payload=payload))

    upserted = 0
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i:i + UPSERT_BATCH_SIZE]
        qdrant.upsert(collection_name=COLLECTION, points=batch)
        upserted += len(batch)
    return upserted


def append_to_bm25(summaries: list[dict], unit_label: str) -> None:
    """Append summaries to the chunks BM25 pickle. The pickle's key
    'propositions' is a list whose order matches BM25 indices; we add new
    rows at the end and rebuild BM25Okapi."""
    if not summaries:
        return
    with open(BM25_PATH, "rb") as f:
        bundle = pickle.load(f)
    chunks_list = bundle["propositions"]  # actually chunks now
    for s in summaries:
        chunks_list.append({
            "chunk_id": str(uuid.uuid5(uuid.NAMESPACE_OID,
                                       f"{unit_label}|{s['chapter_num']}|"
                                       f"{s['section_title']}|{s.get('subsection_title','')}")),
            "text": s["summary"],
            "domain": DOMAIN,
            "textbook_id": DOMAIN,
            "chapter_num": s["chapter_num"],
            "chapter_title": s["chapter_title"],
            "section_title": s["section_title"],
            "subsection_title": s.get("subsection_title", ""),
            "page": 0,
            "element_type": "paragraph",
            "indexing_unit": unit_label,
        })
    # Rebuild BM25 with all texts
    texts = [c.get("text", "") for c in chunks_list]
    tokenized = [stem_tokenize(t) for t in texts]
    bm25 = BM25Okapi(tokenized)
    with open(BM25_PATH, "wb") as f:
        pickle.dump({"bm25": bm25, "propositions": chunks_list}, f)
    print(f"  BM25 rebuilt with {len(chunks_list)} total entries.")


async def main_async(args):
    chunks = load_chunks(CHUNKS_PATH)
    print(f"Loaded {len(chunks)} chunks")
    sub_groups = group_by_subsection(chunks)
    sec_groups = group_by_section(chunks)
    print(f"  {len(sub_groups)} subsection groups, {len(sec_groups)} section groups")

    qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
    openai = OpenAI()

    print(f"\n=== Building subsection summaries ({len(sub_groups)} groups) ===")
    sub_summaries = await build_subsection_summaries(sub_groups, limit=args.limit)
    # Save raw artifact for review
    out = ROOT / "data/artifacts/raptor_subsection_summaries.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for s in sub_summaries:
            f.write(json.dumps(s) + "\n")
    print(f"  saved → {out.relative_to(ROOT)}")

    print("\n  Embedding + upserting subsection summaries to Qdrant...")
    n = upsert_summaries(qdrant, openai, sub_summaries, "subsection_summary")
    print(f"  upserted {n} subsection summaries to '{COLLECTION}'")
    append_to_bm25(sub_summaries, "subsection_summary")

    if args.section_only or not args.skip_section:
        print(f"\n=== Building section summaries ({len(sec_groups)} groups) ===")
        sec_summaries = await build_section_summaries(
            sec_groups, sub_summaries, limit=args.limit
        )
        out = ROOT / "data/artifacts/raptor_section_summaries.jsonl"
        with open(out, "w") as f:
            for s in sec_summaries:
                f.write(json.dumps(s) + "\n")
        print(f"  saved → {out.relative_to(ROOT)}")
        print("\n  Embedding + upserting section summaries to Qdrant...")
        n = upsert_summaries(qdrant, openai, sec_summaries, "section_summary")
        print(f"  upserted {n} section summaries to '{COLLECTION}'")
        append_to_bm25(sec_summaries, "section_summary")

    print("\nALL DONE.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="smoke test: only N groups per level")
    ap.add_argument("--skip-section", action="store_true",
                    help="only build subsection summaries (skip section-level)")
    ap.add_argument("--section-only", action="store_true",
                    help="only section-level (skip subsection)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
