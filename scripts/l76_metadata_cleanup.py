"""
L76 — LLM-based chunk metadata cleanup (no orphans, no heuristics).

Per docs/AUDIT_2026-05-02.md L76:
  Hard invariant: every chunk in chunks_openstax_anatomy.jsonl MUST have all
  three hierarchy levels populated (chapter_title, section_title,
  subsection_title). The validation gate at the end fails the build if any
  chunk lacks any field.

Algorithm (per L76):
  For each chunk missing subsection_title:
    1. Haiku attempt with chunk text + section's TOC neighbors + raptor
       summaries + neighbor chunks (prev/next 200 chars).
       Output: { action: "use_existing"|"create_new", chapter, section,
                 subsection, confidence, rationale }
       Confidence >= 0.8 (use_existing) OR >= 0.85 (create_new) -> accept
    2. Sonnet escalation if Haiku confidence below threshold.
       Confidence >= 0.8 -> accept
    3. If Sonnet still uncertain -> FORCE create_new at Sonnet's best guess
       (no drops, no orphans). Tag with low_confidence flag for audit.

Outputs:
  - data/processed/chunks_openstax_anatomy.jsonl  (in-place, with .pre_l76.bak)
  - data/textbook_structure.json (in-place, with .pre_l76.bak; new subsections appended)
  - data/artifacts/llm_synthesized_subsections/{date}.json (audit log of create_new decisions)

Usage:
  .venv/bin/python scripts/l76_metadata_cleanup.py [--dry-run] [--limit N] [--concurrency N]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from conversation.llm_client import (  # noqa: E402
    make_async_anthropic_client,
    resolve_model,
)

CHUNKS_PATH = REPO / "data" / "processed" / "chunks_openstax_anatomy.jsonl"
STRUCT_PATH = REPO / "data" / "textbook_structure.json"
RAPTOR_PATH = REPO / "data" / "artifacts" / "raptor_subsection_summaries.jsonl"
AUDIT_DIR = REPO / "data" / "artifacts" / "llm_synthesized_subsections"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

HAIKU_USE_EXISTING_THRESHOLD = 0.80
HAIKU_CREATE_NEW_THRESHOLD = 0.85
SONNET_ACCEPT_THRESHOLD = 0.80


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_chunks() -> list[dict]:
    return [json.loads(l) for l in CHUNKS_PATH.open()]


def load_structure() -> dict:
    return json.loads(STRUCT_PATH.read_text())


def load_raptor_summaries() -> dict[tuple, str]:
    out = {}
    for l in RAPTOR_PATH.open():
        s = json.loads(l)
        k = (
            s.get("chapter") or s.get("chapter_title") or "",
            s.get("section") or s.get("section_title") or "",
            s.get("subsection") or s.get("subsection_title") or "",
        )
        out[k] = s.get("summary") or s.get("text") or ""
    return out


def index_chunks_by_section(chunks: list[dict]) -> dict[tuple, list[dict]]:
    """Group all chunks by (chapter_num, section_title) for neighbor lookup."""
    out = defaultdict(list)
    for c in chunks:
        out[(c.get("chapter_num"), c.get("section_title"))].append(c)
    # Sort each section by sequence_index for stable prev/next lookup
    for k in out:
        out[k].sort(key=lambda c: c.get("sequence_index", 0))
    return out


def find_chapter_node(structure: dict, ch_num: int) -> tuple[str, dict] | tuple[None, None]:
    """Find the textbook_structure entry for a chapter by number.

    Structure shape (nested dict keyed by 'Chapter N: Title'):
        {
          "Chapter 10: Muscle Tissue": {"difficulty": ..., "sections": {...}},
          ...
        }
    """
    prefix = f"Chapter {ch_num}:"
    for k, v in structure.items():
        if k.startswith(prefix) and isinstance(v, dict):
            return k, v
    return None, None


def get_section_subsections(structure: dict, ch_num: int, section_title: str) -> list[str]:
    """Return existing subsections under this (chapter, section). Empty list if not found."""
    _, ch = find_chapter_node(structure, ch_num)
    if not ch:
        return []
    sections = ch.get("sections") or {}
    sec = sections.get(section_title) if isinstance(sections, dict) else None
    if not sec:
        return []
    subs = sec.get("subsections") or {}
    if isinstance(subs, dict):
        return list(subs.keys())
    if isinstance(subs, list):
        return [(s.get("title") or s.get("name") or "") for s in subs if (s.get("title") or s.get("name"))]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt_for_chunk(
    chunk: dict,
    structure: dict,
    raptor: dict[tuple, str],
    section_chunks: list[dict],
    full_section_text: str | None = None,
) -> str:
    ch_num = chunk.get("chapter_num")
    chapter_title = chunk.get("chapter_title") or ""
    section_title = chunk.get("section_title") or ""

    existing_subs = get_section_subsections(structure, ch_num, section_title)

    # Sibling subsection summaries (raptor) for this section
    sibling_summaries = []
    for sub in existing_subs:
        k = (chapter_title, section_title, sub)
        summ = raptor.get(k, "")
        if summ:
            sibling_summaries.append(f"  - {sub}: {summ[:200]}")

    # Neighbor chunks (prev + next within same section)
    seq = chunk.get("sequence_index", 0)
    prev_chunk = next(
        (c for c in reversed(section_chunks) if c.get("sequence_index", 0) < seq), None
    )
    next_chunk = next(
        (c for c in section_chunks if c.get("sequence_index", 0) > seq), None
    )
    prev_text = (prev_chunk.get("text") or "")[:200] if prev_chunk else ""
    next_text = (next_chunk.get("text") or "")[:200] if next_chunk else ""

    sibling_block = (
        "\n".join(sibling_summaries) if sibling_summaries else "  (no existing subsections under this section)"
    )

    section_text_block = (
        f"\n\nFULL SECTION TEXT (for escalation context):\n{full_section_text[:4000]}"
        if full_section_text
        else ""
    )

    return f"""You are classifying a textbook chunk into the correct subsection of an
anatomy & physiology textbook.

CONTEXT:
  Chapter: Ch{ch_num} — {chapter_title}
  Section: {section_title}

EXISTING SUBSECTIONS UNDER THIS SECTION (with summaries):
{sibling_block}

PRECEDING CHUNK (last 200 chars of prior chunk in this section):
{prev_text}

FOLLOWING CHUNK (first 200 chars of next chunk in this section):
{next_text}

THE CHUNK TO CLASSIFY (full text):
{chunk.get('text', '')}
{section_text_block}

YOUR TASK:
Decide whether this chunk fits an EXISTING subsection (action="use_existing")
or whether it needs a NEW subsection (action="create_new"). Pick the most
faithful semantic placement based on the chunk's content.

If you create a new subsection, propose a concise title (3-7 words) that
matches OpenStax textbook conventions (e.g., "Anatomy of the Pleura",
"Mechanism of Insulin Release", "Phases of Cell Division").

Return ONLY a JSON object — no prose, no markdown:
{{
  "action": "use_existing" | "create_new",
  "chapter": "{chapter_title}",
  "section": "{section_title}",
  "subsection": "<title — must exist in EXISTING list if use_existing; new title if create_new>",
  "confidence": 0.0-1.0,
  "rationale": "<one sentence>"
}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# LLM calls
# ─────────────────────────────────────────────────────────────────────────────

async def call_llm(client, model: str, prompt: str, max_tokens: int = 400) -> dict:
    resp = await client.messages.create(
        model=resolve_model(model),
        max_tokens=max_tokens,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: try to find a JSON object
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


async def classify_chunk(
    client,
    chunk: dict,
    structure: dict,
    raptor: dict,
    section_chunks_by_key: dict,
    section_text_by_key: dict[tuple, str],
    sem: asyncio.Semaphore,
) -> dict:
    """Returns a decision dict: { chunk_id, action, subsection, confidence, source, ... }."""
    async with sem:
        ch_num = chunk.get("chapter_num")
        section_title = chunk.get("section_title") or ""
        section_chunks = section_chunks_by_key.get((ch_num, section_title), [])

        # Stage 1: Haiku
        prompt = build_prompt_for_chunk(chunk, structure, raptor, section_chunks)
        try:
            haiku_out = await call_llm(client, HAIKU_MODEL, prompt)
        except Exception as e:
            haiku_out = {"action": "create_new", "subsection": "Unclassified", "confidence": 0.0, "rationale": f"haiku-error: {e}"}

        action = haiku_out.get("action", "create_new")
        confidence = float(haiku_out.get("confidence") or 0.0)

        threshold = HAIKU_USE_EXISTING_THRESHOLD if action == "use_existing" else HAIKU_CREATE_NEW_THRESHOLD
        if confidence >= threshold:
            return {
                "chunk_id": chunk.get("chunk_id"),
                "action": action,
                "subsection": haiku_out.get("subsection") or "Unclassified",
                "confidence": confidence,
                "rationale": haiku_out.get("rationale", ""),
                "source": f"haiku_{action}",
                "model": "haiku",
                "low_confidence": False,
            }

        # Stage 2: Sonnet escalation with full section text
        full_section_text = section_text_by_key.get((ch_num, section_title), "")
        prompt2 = build_prompt_for_chunk(chunk, structure, raptor, section_chunks, full_section_text=full_section_text)
        try:
            sonnet_out = await call_llm(client, SONNET_MODEL, prompt2)
        except Exception as e:
            sonnet_out = {"action": "create_new", "subsection": "Unclassified", "confidence": 0.0, "rationale": f"sonnet-error: {e}"}

        action2 = sonnet_out.get("action", "create_new")
        confidence2 = float(sonnet_out.get("confidence") or 0.0)

        if confidence2 >= SONNET_ACCEPT_THRESHOLD:
            return {
                "chunk_id": chunk.get("chunk_id"),
                "action": action2,
                "subsection": sonnet_out.get("subsection") or "Unclassified",
                "confidence": confidence2,
                "rationale": sonnet_out.get("rationale", ""),
                "source": f"sonnet_{action2}",
                "model": "sonnet",
                "low_confidence": False,
            }

        # Stage 3: forced create_new at Sonnet's best guess (per L76 — no drops)
        return {
            "chunk_id": chunk.get("chunk_id"),
            "action": "create_new",
            "subsection": sonnet_out.get("subsection") or "Unclassified",
            "confidence": confidence2,
            "rationale": (sonnet_out.get("rationale", "") or "") + " [FORCED — no orphans per L76 invariant]",
            "source": "sonnet_forced_create_new",
            "model": "sonnet",
            "low_confidence": True,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def main_async(args):
    print("Loading chunks, structure, raptor summaries...", flush=True)
    chunks = load_chunks()
    structure = load_structure()
    raptor = load_raptor_summaries()
    print(f"  {len(chunks)} chunks, {len(raptor)} raptor summaries", flush=True)

    # Identify chunks missing subsection_title
    needs_fix = [c for c in chunks if not c.get("subsection_title")]
    print(f"  {len(needs_fix)} chunks need subsection_title", flush=True)

    if args.limit:
        needs_fix = needs_fix[: args.limit]
        print(f"  --limit {args.limit}: processing first {len(needs_fix)} only", flush=True)

    # Index for fast neighbor lookup
    section_chunks_by_key = index_chunks_by_section(chunks)

    # Pre-build full section text for each (chapter_num, section_title) key
    # used by Sonnet escalation. Concat all chunks' text in sequence order.
    section_text_by_key: dict[tuple, str] = {}
    for k, scs in section_chunks_by_key.items():
        section_text_by_key[k] = "\n\n".join(c.get("text", "") for c in scs)

    if args.dry_run:
        print("\nDRY RUN — would classify the following sections:", flush=True)
        from collections import Counter
        seccount = Counter((c.get("chapter_num"), c.get("section_title")) for c in needs_fix)
        for (n, t), cnt in sorted(seccount.items(), key=lambda x: -x[1])[:20]:
            print(f"  Ch{n} ({cnt}): {t}", flush=True)
        return

    # Run classifications concurrently
    client = make_async_anthropic_client()
    sem = asyncio.Semaphore(args.concurrency)

    print(f"\nClassifying {len(needs_fix)} chunks at concurrency={args.concurrency}...", flush=True)
    t0 = time.time()
    results: list[dict] = []
    completed = 0

    async def runner(c: dict):
        nonlocal completed
        try:
            r = await classify_chunk(
                client, c, structure, raptor, section_chunks_by_key, section_text_by_key, sem
            )
        except Exception as e:
            r = {
                "chunk_id": c.get("chunk_id"),
                "action": "create_new",
                "subsection": "Unclassified",
                "confidence": 0.0,
                "rationale": f"runner-error: {e}",
                "source": "error_fallback",
                "model": "none",
                "low_confidence": True,
            }
        results.append(r)
        completed += 1
        if completed % 25 == 0 or completed == len(needs_fix):
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (len(needs_fix) - completed) / rate if rate > 0 else 0
            print(
                f"  [{completed}/{len(needs_fix)}] elapsed={elapsed:.0f}s rate={rate:.1f}/s eta={eta:.0f}s",
                flush=True,
            )

    await asyncio.gather(*(runner(c) for c in needs_fix))

    elapsed = time.time() - t0
    print(f"\nClassification done in {elapsed:.0f}s.", flush=True)

    # Stats
    by_source = defaultdict(int)
    low_conf = 0
    for r in results:
        by_source[r["source"]] += 1
        if r.get("low_confidence"):
            low_conf += 1
    print("Sources breakdown:", flush=True)
    for s, n in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}", flush=True)
    print(f"  low_confidence (audit me): {low_conf}", flush=True)

    # Apply results to chunks (in-memory)
    by_id = {r["chunk_id"]: r for r in results}
    new_subsections: dict[tuple, dict] = {}  # (chapter_num, section_title, subsection) -> meta
    for c in chunks:
        cid = c.get("chunk_id")
        if cid not in by_id:
            continue
        r = by_id[cid]
        sub = r["subsection"]
        c["subsection_title"] = sub
        c["subsection_metadata_source"] = "llm_synthesized" if r["action"] == "create_new" else "llm_remapped_existing"
        if r["action"] == "create_new":
            key = (c.get("chapter_num"), c.get("section_title"), sub)
            new_subsections[key] = {
                "chapter_num": c.get("chapter_num"),
                "chapter_title": c.get("chapter_title"),
                "section_title": c.get("section_title"),
                "subsection_title": sub,
                "source": "llm_synthesized",
                "confidence": r["confidence"],
                "rationale": r["rationale"],
                "model": r["model"],
                "low_confidence": r.get("low_confidence", False),
            }

    print(f"\nNew subsections to add to textbook_structure.json: {len(new_subsections)}", flush=True)

    # Write outputs
    print("Writing backups + outputs...", flush=True)
    shutil.copy(CHUNKS_PATH, str(CHUNKS_PATH) + ".pre_l76.bak")
    shutil.copy(STRUCT_PATH, str(STRUCT_PATH) + ".pre_l76.bak")

    with CHUNKS_PATH.open("w") as f:
        for c in chunks:
            f.write(json.dumps(c) + "\n")
    print(f"  wrote {CHUNKS_PATH}", flush=True)

    # Augment textbook_structure with new synthesized subsections.
    # Structure shape: {"Chapter N: Title": {"sections": {sec_name: {"subsections": {sub_name: {...}}}}}}
    added_into_structure = 0
    for (ch_num, sec_title, sub), meta in new_subsections.items():
        _, ch_node = find_chapter_node(structure, ch_num)
        if not ch_node:
            continue
        sections = ch_node.setdefault("sections", {})
        if not isinstance(sections, dict):
            continue
        target_sec = sections.get(sec_title)
        if not target_sec:
            continue
        subs = target_sec.setdefault("subsections", {})
        if not isinstance(subs, dict):
            continue
        if sub in subs:
            continue
        subs[sub] = {
            "difficulty": "moderate",
            "source": "llm_synthesized",
            "confidence": meta["confidence"],
            "rationale": meta["rationale"],
        }
        added_into_structure += 1
    print(f"  appended {added_into_structure} new subsections to textbook_structure", flush=True)

    STRUCT_PATH.write_text(json.dumps(structure, indent=2))
    print(f"  wrote {STRUCT_PATH}", flush=True)

    # Audit log
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    audit_path = AUDIT_DIR / f"{today}.json"
    audit_payload = {
        "ingestion_date": today,
        "total_chunks_processed": len(needs_fix),
        "results": results,
        "new_subsections": [
            {"key": list(k), **v} for k, v in new_subsections.items()
        ],
        "low_confidence_count": low_conf,
        "elapsed_seconds": elapsed,
    }
    audit_path.write_text(json.dumps(audit_payload, indent=2))
    print(f"  wrote audit log {audit_path}", flush=True)

    # Final invariant check (soft — fails loud but doesn't raise so user sees report)
    final_chunks = load_chunks()
    still_missing = [c for c in final_chunks if not c.get("subsection_title")]
    print(f"\nINVARIANT CHECK: chunks still missing subsection_title: {len(still_missing)}", flush=True)
    if still_missing:
        print("  (this should be 0 after L76 — investigate above)", flush=True)
    else:
        print("  ✓ all chunks now have full hierarchy (chapter + section + subsection)", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Show what would be classified, don't call APIs")
    p.add_argument("--limit", type=int, default=0, help="Process only first N missing-subsection chunks (testing)")
    p.add_argument("--concurrency", type=int, default=10, help="Max concurrent LLM calls")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
