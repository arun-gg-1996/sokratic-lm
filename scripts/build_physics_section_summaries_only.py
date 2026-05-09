#!/usr/bin/env python3
"""
Run only the SECTION-level RAPTOR pass for physics, reusing the
already-saved subsection summaries.

Why this exists
The full build_raptor_summaries.py runs subsection then section. The
subsection pass already completed and is saved at
data/artifacts/raptor_subsection_summaries_openstax_physics.jsonl. The
section pass got stuck at 0% CPU (httpx connection hang in the
AsyncAnthropic SDK with concurrency=4 + the default 10-min timeout).
This script reads the already-saved subsection summaries, runs only the
115 section-level summaries with concurrency=2 and an explicit 60s
timeout per call, and writes the result.

Usage
  SOKRATIC_USE_BEDROCK=0 SOKRATIC_DOMAIN=physics python \\
    scripts/build_physics_section_summaries_only.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# Selective env load (same pattern as rerun_failed_dual_task.py).
_shell_overrides = {
    k: os.environ[k]
    for k in ("SOKRATIC_DOMAIN", "SOKRATIC_USE_BEDROCK")
    if os.environ.get(k)
}
load_dotenv(ROOT / ".env", override=True)
for k, v in _shell_overrides.items():
    os.environ[k] = v

from anthropic import AsyncAnthropic  # noqa: E402

from config import cfg  # noqa: E402
from conversation.llm_client import make_async_anthropic_client, resolve_model  # noqa: E402

CHUNKS_PATH = ROOT / cfg.domain_path("chunks")
SUB_PATH = ROOT / cfg.domain_path("raptor_subsection_summaries")
SEC_PATH = ROOT / cfg.domain_path("raptor_section_summaries")
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 2
PER_CALL_TIMEOUT = 60.0


# Domain-aware prompt vars (same shape as build_raptor_summaries.py)
_DOMAIN_PROMPT_VARS: dict[str, dict[str, str]] = {
    "anatomy": {
        "domain_descriptor": "human anatomy and physiology",
        "domain_entity_label": "anatomical structures, processes, or concepts",
    },
    "physics": {
        "domain_descriptor": "university physics",
        "domain_entity_label": "physical quantities, principles, or relationships",
    },
}


def _domain_prompt_vars() -> dict[str, str]:
    short = (getattr(cfg.domain, "short", "") or "").lower()
    return _DOMAIN_PROMPT_VARS.get(short, _DOMAIN_PROMPT_VARS["anatomy"])


SUMMARY_PROMPT_SECTION = """\
You are summarizing a SECTION of an undergraduate {domain_descriptor}
textbook for a retrieval index. Produce a 6-10 sentence summary that
lists the topics covered in each subsection within the section, naming
key {domain_entity_label} and relationships. Use the textbook's exact
terminology. Stay grounded in the source.

CHAPTER {chapter_num}: {chapter_title}
SECTION: {section_title}

SOURCE (subsection summaries already produced for this section):
{source}

Write only the summary, no preamble."""


def truncate_tokens_approx(text: str, max_chars: int = 320_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... TRUNCATED ...]"


async def summarize_one(
    sem: asyncio.Semaphore,
    client: AsyncAnthropic,
    prompt: str,
    label: str,
) -> tuple[str, str | None]:
    async with sem:
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=resolve_model(SUMMARY_MODEL),
                    max_tokens=420,
                    temperature=0.0,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=PER_CALL_TIMEOUT,
            )
            text = "".join(getattr(b, "text", "") or "" for b in resp.content)
            return label, text.strip()
        except asyncio.TimeoutError:
            return label, f"[ERROR: TIMEOUT after {PER_CALL_TIMEOUT}s]"
        except Exception as e:
            return label, f"[ERROR: {type(e).__name__}: {e}]"


async def main() -> int:
    if not SUB_PATH.exists():
        print(f"ERR: subsection summaries missing at {SUB_PATH}", file=sys.stderr)
        return 2

    chunks = [json.loads(l) for l in CHUNKS_PATH.open()]
    sub_summaries = [json.loads(l) for l in SUB_PATH.open()]
    print(f"chunks:             {len(chunks)}")
    print(f"subsection summaries: {len(sub_summaries)}")

    # Group chunks by (chapter_num, section_title) to enumerate sections
    # AND get chapter_title.
    by_section: dict[tuple, list[dict]] = defaultdict(list)
    for c in chunks:
        by_section[(c.get("chapter_num"), c.get("section_title", ""))].append(c)
    print(f"section groups:     {len(by_section)}")

    # Group subsection summaries by (chapter_num, section_title)
    sub_by_section: dict[tuple, list[dict]] = defaultdict(list)
    for s in sub_summaries:
        sub_by_section[(s.get("chapter_num"), s.get("section_title", ""))].append(s)

    items = list(by_section.items())
    client = make_async_anthropic_client()
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = []
    metas = []
    domain_vars = _domain_prompt_vars()
    for (ch, sec), chs in items:
        chapter_title = chs[0].get("chapter_title", "")
        sub_summs = sub_by_section.get((ch, sec), [])
        if not sub_summs:
            continue  # No subsection summaries → skip this section
        source_text = "\n\n".join(
            f"[{s.get('subsection_title') or '(intro)'}] {s.get('summary','')}"
            for s in sub_summs
        )
        source_text = truncate_tokens_approx(source_text)
        prompt = SUMMARY_PROMPT_SECTION.format(
            chapter_num=ch, chapter_title=chapter_title,
            section_title=sec, source=source_text,
            **domain_vars,
        )
        tasks.append(summarize_one(sem, client, prompt, label=f"{ch}|{sec}"))
        metas.append({
            "chapter_num": ch,
            "chapter_title": chapter_title,
            "section_title": sec,
            "subsection_title": "",
            "n_source_subsections": len(sub_summs),
            "group_subsection_keys": [
                (s.get("chapter_num"), s.get("section_title"), s.get("subsection_title"))
                for s in sub_summs
            ],
        })

    print(f"running {len(tasks)} section summaries @ concurrency={CONCURRENCY}, timeout={PER_CALL_TIMEOUT}s/call")

    results: list[dict] = []
    done = 0
    n_err = 0
    t0 = time.time()
    for fut in asyncio.as_completed(tasks):
        label, text = await fut
        done += 1
        if text and text.startswith("[ERROR"):
            n_err += 1
        if done % 10 == 0 or done == len(tasks):
            print(
                f"  section summaries: {done}/{len(tasks)} "
                f"(elapsed={int(time.time()-t0)}s, errs={n_err})",
                flush=True,
            )

        ch_s, sec_s = label.split("|", 1)
        for m in metas:
            if str(m["chapter_num"]) == ch_s and m["section_title"] == sec_s:
                results.append({**m, "summary": text or ""})
                break

    SEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEC_PATH.open("w") as f:
        for s in results:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"\nsaved → {SEC_PATH.relative_to(ROOT)}")
    print(f"  total section summaries: {len(results)}")
    print(f"  errors: {n_err}")
    print(f"  elapsed: {int(time.time()-t0)}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
