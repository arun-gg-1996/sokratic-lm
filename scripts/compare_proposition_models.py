"""
scripts/compare_proposition_models.py
-------------------------------------
Run the current proposition-extraction prompt through 4 models on 10 diverse
chunks and dump outputs side-by-side so a human (or a follow-up analysis
script) can compare atomicity, faithfulness, redundancy, and formatting.

Models:
  - anthropic:claude-haiku-4-5
  - anthropic:claude-sonnet-4-6
  - openai:gpt-4o-mini
  - openai:gpt-4o

Output: data/artifacts/proposition_model_comparison.json
        (one record per chunk, with per-model outputs + latency + token usage)
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

import anthropic
from openai import OpenAI

CHUNKS_PATH = ROOT / "data/processed/chunks_ot.jsonl"
OUT_PATH = ROOT / "data/artifacts/proposition_model_comparison.json"

PROMPT_TEMPLATE = (
    "Rewrite the following passage as a list of standalone factual statements.\n"
    "Each statement must be self-contained, testable, and require no other context to understand.\n"
    "Return only the list, one statement per line, no bullets or numbering.\n\n"
    "Passage:\n{chunk_text}"
)

MODELS = [
    ("anthropic", "claude-haiku-4-5"),
    ("anthropic", "claude-sonnet-4-6"),
    ("openai", "gpt-4o-mini"),
    ("openai", "gpt-4o"),
]


def pick_chunks(n: int = 10) -> list[dict]:
    random.seed(42)
    rows = []
    with CHUNKS_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            if r["element_type"] == "paragraph":
                rows.append(r)
    short = [r for r in rows if 200 <= len(r["text"]) < 800]
    med = [r for r in rows if 800 <= len(r["text"]) < 1500]
    long_ = [r for r in rows if 1500 <= len(r["text"]) <= 2507]
    picks = []
    chapters_used = set()
    for bucket, k in [(short, 3), (med, 4), (long_, 3)]:
        random.shuffle(bucket)
        added = 0
        for r in bucket:
            if r["chapter_num"] not in chapters_used or added < 2:
                picks.append(r)
                chapters_used.add(r["chapter_num"])
                added += 1
                if added == k:
                    break
    return picks


def run_anthropic(client: anthropic.Anthropic, model: str, prompt: str) -> dict:
    t0 = time.time()
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "text": resp.content[0].text if resp.content else "",
        "latency_s": round(time.time() - t0, 2),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


def run_openai(client: OpenAI, model: str, prompt: str) -> dict:
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "text": resp.choices[0].message.content or "",
        "latency_s": round(time.time() - t0, 2),
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
    }


def main() -> int:
    picks = pick_chunks(10)
    print(f"Picked {len(picks)} chunks across "
          f"{len(set(p['chapter_num'] for p in picks))} chapters")

    anth = anthropic.Anthropic()
    oai = OpenAI()

    records = []
    for i, chunk in enumerate(picks, 1):
        prompt = PROMPT_TEMPLATE.format(chunk_text=chunk["text"])
        rec = {
            "chunk_id": chunk["chunk_id"],
            "chapter_num": chunk["chapter_num"],
            "section_title": chunk["section_title"],
            "subsection_title": chunk.get("subsection_title"),
            "page": chunk["page"],
            "text_len": len(chunk["text"]),
            "source_text": chunk["text"],
            "outputs": {},
        }
        print(f"\n[{i}/{len(picks)}] ch{chunk['chapter_num']} p{chunk['page']} "
              f"len={len(chunk['text'])}  {chunk['section_title'][:60]}")
        for provider, model in MODELS:
            print(f"  - {provider}:{model} ...", end=" ", flush=True)
            try:
                if provider == "anthropic":
                    out = run_anthropic(anth, model, prompt)
                else:
                    out = run_openai(oai, model, prompt)
                n_lines = len([l for l in out["text"].splitlines() if l.strip()])
                print(f"{n_lines} lines, {out['latency_s']}s, "
                      f"{out['input_tokens']}→{out['output_tokens']} tok")
                rec["outputs"][f"{provider}:{model}"] = out
            except Exception as e:
                print(f"FAILED: {e}")
                rec["outputs"][f"{provider}:{model}"] = {"error": str(e)}
        records.append(rec)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(records, indent=2))
    print(f"\nWrote {OUT_PATH}  ({len(records)} chunks × {len(MODELS)} models)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
