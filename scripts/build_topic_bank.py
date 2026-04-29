"""
scripts/build_topic_bank.py
---------------------------
Build a topic bank for scaled e2e testing. Each topic in the bank is one
(chapter, section, subsection) tuple known to have strong corpus coverage.
For each topic, we generate 6 profile-specific student-style query
phrasings using Haiku — so the harness can run 3 seeds × 6 profiles per
topic without all 18 conversations using the same query.

The 6 profiles (matching simulation/profiles.py):
  S1 Strong       — precise scientific phrasing, often multi-part
  S2 Moderate     — clean, complete questions, neutral register
  S3 Weak         — vague, layman, often runs words together
  S4 Overconfident- leading assertions ("X is basically Y, right?")
  S5 Disengaged   — terse, often 1-3 words, lowercase
  S6 Anxious      — hesitant, hedging, multi-clause apology phrasing

Output schema (per row in data/eval/topic_bank_v1.jsonl):
  {
    "topic_id": "ch11_lever_systems_exercise_and_stretching",
    "chapter_num": 11,
    "chapter_title": "...",
    "section_title": "Lever Systems",
    "subsection_title": "Exercise and Stretching",
    "chunk_count": 24,
    "queries": {
      "S1": "...",  "S2": "...",  "S3": "...",
      "S4": "...",  "S5": "...",  "S6": "..."
    }
  }

Cost: 30 topics × 1 Haiku call (returns all 6 phrasings per call) = ~$0.30,
~2 min wall.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from anthropic import AsyncAnthropic  # noqa: E402

SEED_PATH = Path("/tmp/topic_bank_seed.jsonl")
OUT_PATH = ROOT / "data/eval/topic_bank_v1.jsonl"
MODEL = "claude-haiku-4-5-20251001"
SEMAPHORE = 6


PROMPT_TEMPLATE = """You are generating realistic student questions for an
anatomy tutoring system, varied across six student profiles. The student
wants to learn about the following topic from their textbook:

  Chapter {chapter_num}: {chapter_title}
  Section: {section_title}
  Subsection: {subsection_title}

Write ONE question phrasing for each of the six profiles below. Each
phrasing should reflect the profile's distinct voice and ask about THIS
topic specifically — not adjacent topics.

Profiles:
  S1 STRONG: precise scientific phrasing, often multi-part. Uses
     terminology correctly. Asks a clean question. ~15-25 words.
  S2 MODERATE: clean, complete question, neutral student register.
     ~10-15 words.
  S3 WEAK: vague, layman, may misspell, may run words together. May not
     remember the exact term. ~6-12 words. Lowercase mostly.
  S4 OVERCONFIDENT: leads with an assertion (often partially wrong),
     hedges only slightly. Pattern: "X is basically Y, right?". ~10-18 words.
  S5 DISENGAGED: terse, 1-4 words. Lowercase. Just the topic noun phrase.
  S6 ANXIOUS: hesitant, hedging, multi-clause. Lots of "I think... maybe...
     not sure...". ~20-35 words.

Return STRICT JSON only, no preamble:
{{
  "S1": "...",
  "S2": "...",
  "S3": "...",
  "S4": "...",
  "S5": "...",
  "S6": "..."
}}
"""


def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:60]


def _topic_id(row: dict) -> str:
    return f"ch{row['chapter_num']}_{_slugify(row['section_title'])}__{_slugify(row['subsection_title'])}"


async def _gen_one(sem, client, row):
    async with sem:
        prompt = PROMPT_TEMPLATE.format(
            chapter_num=row["chapter_num"],
            chapter_title=row["chapter_title"],
            section_title=row["section_title"],
            subsection_title=row["subsection_title"],
        )
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=900,
                temperature=0.4,
                messages=[{"role": "user", "content": prompt}],
            )
            text = ""
            for block in resp.content:
                t = getattr(block, "text", "") or ""
                text += t
            text = text.strip()
            # Strip markdown fences if any
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
            queries = json.loads(text)
            # Validate schema
            for k in ("S1","S2","S3","S4","S5","S6"):
                if not queries.get(k):
                    raise ValueError(f"missing {k}")
            return {**row, "topic_id": _topic_id(row), "queries": queries}
        except Exception as e:
            return {**row, "topic_id": _topic_id(row), "queries": None,
                    "error": f"{type(e).__name__}: {e}"}


async def main():
    rows = [json.loads(l) for l in open(SEED_PATH)]
    print(f"Loaded {len(rows)} topic seeds from {SEED_PATH}", flush=True)

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(SEMAPHORE)
    t0 = time.time()
    tasks = [_gen_one(sem, client, r) for r in rows]
    results = []
    for fut in asyncio.as_completed(tasks):
        r = await fut
        results.append(r)
        ok = "✓" if r.get("queries") else "✗"
        print(f"  {ok} {r.get('topic_id')} ({len(results)}/{len(rows)})",
              flush=True)
    print(f"\nDone in {int(time.time()-t0)}s", flush=True)

    # Write to output
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    n_ok = sum(1 for r in results if r.get("queries"))
    with open(OUT_PATH, "w") as f:
        for r in results:
            if r.get("queries"):
                f.write(json.dumps(r) + "\n")
    print(f"\nWrote {n_ok}/{len(results)} entries to {OUT_PATH.relative_to(ROOT)}")
    if n_ok < len(results):
        print("Failed entries:")
        for r in results:
            if not r.get("queries"):
                print(f"  {r.get('topic_id')}: {r.get('error')}")


if __name__ == "__main__":
    asyncio.run(main())
