"""
L19 + L38 — Complete topic_index entries with display_label + raptor summary.

Per docs/AUDIT_2026-05-02.md:
  L19. Card Label Text — every topic_index entry MUST have a Sonnet-rewritten
       display_label (concise student-friendly phrasing).
  L38. Ingestion Guarantees Summary Per Topic_Index Entry — topic_index AND
       raptor_subsection_summaries MUST be 1:1 by (chapter, section, subsection).
       Missing summaries trigger an LLM call to generate one from the
       subsection's chunks.

This script is the L40 trigger condensed: every topic_index rebuild includes
both passes, single integration point.

Inputs:
  data/topic_index.json
  data/processed/chunks_openstax_anatomy.jsonl  (post-L76)
  data/artifacts/raptor_subsection_summaries.jsonl

Outputs (in-place, with .pre_l19_l38.bak):
  data/topic_index.json  (entries gain display_label field)
  data/artifacts/raptor_subsection_summaries.jsonl  (gains missing entries)

Usage:
  .venv/bin/python scripts/l19_l38_topic_index_complete.py [--dry-run] [--limit N] [--concurrency N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from conversation.llm_client import (  # noqa: E402
    make_async_anthropic_client,
    resolve_model,
)

TOPIC_INDEX_PATH = REPO / "data" / "topic_index.json"
CHUNKS_PATH = REPO / "data" / "processed" / "chunks_openstax_anatomy.jsonl"
RAPTOR_PATH = REPO / "data" / "artifacts" / "raptor_subsection_summaries.jsonl"

SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Display label conventions per L19 — concise, student-friendly, anatomical.
DISPLAY_LABEL_STYLE = "concise student-friendly anatomical phrasing"


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_topic_index() -> list[dict]:
    raw = json.loads(TOPIC_INDEX_PATH.read_text())
    if isinstance(raw, list):
        return raw
    return list(raw.values())


def load_chunks_by_subsection() -> dict[tuple, list[dict]]:
    """Group chunks by (chapter_title, section_title, subsection_title)."""
    out: dict[tuple, list[dict]] = defaultdict(list)
    for line in CHUNKS_PATH.open():
        c = json.loads(line)
        k = (
            c.get("chapter_title") or "",
            c.get("section_title") or "",
            c.get("subsection_title") or "",
        )
        out[k].append(c)
    # Order each by sequence_index for stable concatenation
    for k in out:
        out[k].sort(key=lambda c: c.get("sequence_index", 0))
    return out


def load_raptor_summaries() -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    if not RAPTOR_PATH.exists():
        return out
    for line in RAPTOR_PATH.open():
        s = json.loads(line)
        k = (
            s.get("chapter") or s.get("chapter_title") or "",
            s.get("section") or s.get("section_title") or "",
            s.get("subsection") or s.get("subsection_title") or "",
        )
        out[k] = s
    return out


def topic_index_key(entry: dict) -> tuple:
    return (
        entry.get("chapter") or entry.get("chapter_title") or "",
        entry.get("section") or entry.get("section_title") or "",
        entry.get("subsection") or entry.get("subsection_title") or "",
    )


# ─────────────────────────────────────────────────────────────────────────────
# LLM call helpers
# ─────────────────────────────────────────────────────────────────────────────

async def call_llm(client, model: str, prompt: str, max_tokens: int = 600) -> str:
    resp = await client.messages.create(
        model=resolve_model(model),
        max_tokens=max_tokens,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def build_summary_prompt(chapter: str, section: str, subsection: str, chunk_texts: list[str]) -> str:
    joined = "\n\n".join(chunk_texts[:8])  # cap at 8 chunks (~8k chars) to keep prompt size sane
    return f"""You are a textbook editor writing a concise summary of one subsection of an
anatomy & physiology textbook.

CHAPTER: {chapter}
SECTION: {section}
SUBSECTION: {subsection}

CONTENT (concatenated chunks of this subsection):
{joined}

Write ONE PARAGRAPH (3-5 sentences, ~80-150 words) that summarizes what this
subsection teaches: the key concept, the main entities/processes covered, and
their relationship. Use precise anatomical terminology. No preamble, no
markdown — just the paragraph.
"""


def build_label_prompt(chapter: str, section: str, subsection: str, summary: str) -> str:
    return f"""You are rewriting a textbook table-of-contents entry into a {DISPLAY_LABEL_STYLE}.

CHAPTER: {chapter}
SECTION: {section}
SUBSECTION (raw textbook heading): {subsection}
SUMMARY (one paragraph, for context): {summary}

Rewrite the SUBSECTION as a short, student-friendly card label (2-6 words).

Rules:
- Plain title case, no quotes, no period.
- Concise — drop filler like "Anatomy of the", "Overview of", "Introduction to" unless removing them changes the meaning.
- Faithful to the subsection's actual content.
- No emoji, no markdown, no leading/trailing whitespace.

Output ONLY the label string.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────────────────────────────────────

async def gen_summary(client, entry: dict, chunks_by_sub: dict[tuple, list[dict]], sem) -> dict:
    async with sem:
        k = topic_index_key(entry)
        ch, sec, sub = k
        section_chunks = chunks_by_sub.get(k, [])
        if not section_chunks:
            return {
                "key": k,
                "summary": "",
                "error": "no_chunks_under_subsection",
            }
        texts = [c.get("text", "") for c in section_chunks]
        prompt = build_summary_prompt(ch, sec, sub, texts)
        try:
            summary = await call_llm(client, SONNET_MODEL, prompt, max_tokens=400)
        except Exception as e:
            return {"key": k, "summary": "", "error": f"llm_error: {e}"}
        return {"key": k, "summary": summary, "error": None}


async def gen_label(client, entry: dict, summary_text: str, sem) -> dict:
    async with sem:
        k = topic_index_key(entry)
        ch, sec, sub = k
        prompt = build_label_prompt(ch, sec, sub, summary_text or "(no summary available)")
        try:
            label = await call_llm(client, SONNET_MODEL, prompt, max_tokens=60)
        except Exception as e:
            return {"key": k, "label": sub, "error": f"llm_error: {e}"}
        # Strip wrapping quotes if any
        label = label.strip().strip('"').strip("'").strip()
        # Single-line guard
        label = label.split("\n")[0].strip()
        return {"key": k, "label": label, "error": None}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main_async(args):
    print("Loading topic_index, chunks, raptor summaries...", flush=True)
    entries = load_topic_index()
    chunks_by_sub = load_chunks_by_subsection()
    summaries = load_raptor_summaries()
    print(f"  {len(entries)} topic_index entries, {len(chunks_by_sub)} chunk groups, {len(summaries)} existing summaries", flush=True)

    # Identify missing summaries
    missing_summary = []
    for e in entries:
        k = topic_index_key(e)
        if k not in summaries or not (summaries[k].get("summary") or summaries[k].get("text")):
            missing_summary.append(e)

    # Identify missing display_labels
    missing_label = [e for e in entries if not (isinstance(e, dict) and e.get("display_label"))]

    print(f"\nGap report:", flush=True)
    print(f"  missing summaries: {len(missing_summary)}", flush=True)
    print(f"  missing display_labels: {len(missing_label)}", flush=True)

    if args.limit:
        missing_summary = missing_summary[: args.limit]
        missing_label = missing_label[: args.limit]
        print(f"  --limit {args.limit}: capped both lists", flush=True)

    if args.dry_run:
        print("\nDRY RUN — sample entries needing each:", flush=True)
        for e in missing_summary[:5]:
            print(f"  SUM: {topic_index_key(e)}", flush=True)
        for e in missing_label[:5]:
            print(f"  LBL: {topic_index_key(e)}", flush=True)
        return

    # ── Stage 1: Generate missing summaries ───────────────────────────────
    client = make_async_anthropic_client()
    sem = asyncio.Semaphore(args.concurrency)

    if missing_summary:
        print(f"\nGenerating {len(missing_summary)} summaries...", flush=True)
        t0 = time.time()
        completed = 0
        sum_results: list[dict] = []

        async def runner_sum(e):
            nonlocal completed
            r = await gen_summary(client, e, chunks_by_sub, sem)
            sum_results.append(r)
            completed += 1
            if completed % 10 == 0 or completed == len(missing_summary):
                el = time.time() - t0
                print(f"  [{completed}/{len(missing_summary)}] elapsed={el:.0f}s", flush=True)

        await asyncio.gather(*(runner_sum(e) for e in missing_summary))

        # Apply
        new_summary_records = []
        for r in sum_results:
            if r["error"] or not r["summary"]:
                print(f"  WARN: {r['key']} -> {r['error']}", flush=True)
                continue
            ch, sec, sub = r["key"]
            rec = {
                "chapter": ch,
                "section": sec,
                "subsection": sub,
                "summary": r["summary"],
                "source": "llm_generated_l38",
            }
            summaries[r["key"]] = rec
            new_summary_records.append(rec)

        # Append to JSONL (preserve existing entries)
        if new_summary_records:
            shutil.copy(RAPTOR_PATH, str(RAPTOR_PATH) + ".pre_l19_l38.bak")
            with RAPTOR_PATH.open("a") as f:
                for r in new_summary_records:
                    f.write(json.dumps(r) + "\n")
            print(f"  appended {len(new_summary_records)} new summaries to {RAPTOR_PATH.name}", flush=True)

    # ── Stage 2: Generate display_labels ──────────────────────────────────
    if missing_label:
        print(f"\nGenerating {len(missing_label)} display_labels...", flush=True)
        t0 = time.time()
        completed = 0
        lbl_results: list[dict] = []

        async def runner_lbl(e):
            nonlocal completed
            k = topic_index_key(e)
            summ = summaries.get(k, {})
            summ_text = summ.get("summary") or summ.get("text") or ""
            r = await gen_label(client, e, summ_text, sem)
            lbl_results.append(r)
            completed += 1
            if completed % 25 == 0 or completed == len(missing_label):
                el = time.time() - t0
                print(f"  [{completed}/{len(missing_label)}] elapsed={el:.0f}s", flush=True)

        await asyncio.gather(*(runner_lbl(e) for e in missing_label))

        # Apply to entries (in place — entries are dict references inside the loaded list)
        by_key = {topic_index_key(e): e for e in entries}
        applied = 0
        for r in lbl_results:
            if r["error"]:
                print(f"  WARN label {r['key']} -> {r['error']}", flush=True)
            target = by_key.get(r["key"])
            if target:
                target["display_label"] = r["label"]
                applied += 1

        # Persist topic_index
        shutil.copy(TOPIC_INDEX_PATH, str(TOPIC_INDEX_PATH) + ".pre_l19_l38.bak")
        TOPIC_INDEX_PATH.write_text(json.dumps(entries, indent=2))
        print(f"  applied {applied} display_labels; wrote {TOPIC_INDEX_PATH.name}", flush=True)

    # ── Final invariants ──────────────────────────────────────────────────
    print("\nFinal invariant check:", flush=True)
    final_entries = load_topic_index()
    final_summaries = load_raptor_summaries()
    final_keys = {topic_index_key(e) for e in final_entries}
    final_summary_keys = set(final_summaries.keys())

    no_label = [e for e in final_entries if not e.get("display_label")]
    no_sum = [k for k in final_keys if k not in final_summary_keys]
    extra_sum = [k for k in final_summary_keys if k not in final_keys]

    print(f"  topic_index entries without display_label: {len(no_label)}", flush=True)
    print(f"  topic_index entries without raptor summary: {len(no_sum)}", flush=True)
    print(f"  raptor summaries without topic_index entry (orphans): {len(extra_sum)}", flush=True)

    if not no_label and not no_sum:
        print("  ✓ L19 + L38 invariants satisfied", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=10)
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
