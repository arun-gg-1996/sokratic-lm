"""
ingestion/propositions.py
--------------------------
Extract atomic propositions from base chunks.

Key behavior:
- Processes base chunks only (is_overlap=False)
- Skips paragraph_overlap chunks entirely
- Supports resume from existing propositions file
- Uses parallel workers with a shared sliding-window rate limiter
- Appends checkpoint output after every completed chunk
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from schemas import validate_proposition

load_dotenv()

PROPOSITIONS_MODEL = "claude-sonnet-4-6"
MIN_PROP_CHARS = 10


def _parse_proposition_lines(raw_text: str) -> list[str]:
    """Parse model output into cleaned proposition lines."""
    cleaned: list[str] = []
    seen: set[str] = set()

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line = re.sub(r"^\d+\s*[\.\)]\s*", "", line)
        line = re.sub(r"^[-*•]\s*", "", line)
        line = line.strip().strip('"').strip()

        if len(line) < MIN_PROP_CHARS:
            continue
        if line in seen:
            continue

        seen.add(line)
        cleaned.append(line)

    return cleaned


def _load_jsonl(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_base_chunks(chunks_path: str) -> list[dict]:
    chunks = _load_jsonl(chunks_path)
    return [
        c
        for c in chunks
        if c.get("is_overlap") is False
        and c.get("element_type") != "paragraph_overlap"
    ]


def _build_proposition(chunk: dict, text: str) -> dict:
    """Create proposition dict conforming to PropositionSchema."""
    return {
        "proposition_id": str(uuid.uuid4()),
        "text": text,
        "parent_chunk_id": chunk["chunk_id"],
        "parent_chunk_text": chunk["text"],
        "chapter_num": int(chunk["chapter_num"]),
        "chapter_title": chunk["chapter_title"],
        "section_num": chunk.get("section_num", ""),
        "section_title": chunk.get("section_title", ""),
        "subsection_title": chunk.get("subsection_title", ""),
        "page": int(chunk["page"]),
        "element_type": chunk["element_type"],
        "domain": "ot",
        "image_filename": "",
    }


class SlidingWindowRateLimiter:
    """
    Thread-safe sliding-window limiter for:
    - requests/minute
    - output_tokens/minute
    """

    def __init__(
        self,
        request_limit: int = 45,
        output_token_limit: int = 7000,
        window_seconds: float = 60.0,
    ) -> None:
        self.request_limit = request_limit
        self.output_token_limit = output_token_limit
        self.window_seconds = window_seconds
        self._req_times: deque[float] = deque()
        self._out_events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._req_times and self._req_times[0] < cutoff:
            self._req_times.popleft()
        while self._out_events and self._out_events[0][0] < cutoff:
            self._out_events.popleft()

    def wait_for_request_slot(self) -> None:
        """Block until safe to send one more request, then reserve it."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                req_count = len(self._req_times)
                out_count = sum(tok for _, tok in self._out_events)

                if req_count < self.request_limit and out_count < self.output_token_limit:
                    self._req_times.append(now)
                    return

                waits: list[float] = []
                if req_count >= self.request_limit and self._req_times:
                    waits.append(self.window_seconds - (now - self._req_times[0]))
                if out_count >= self.output_token_limit and self._out_events:
                    waits.append(self.window_seconds - (now - self._out_events[0][0]))
                sleep_for = max(0.05, min(waits) if waits else 0.5)

            time.sleep(sleep_for)

    def record_output_tokens(self, output_tokens: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._out_events.append((now, max(0, int(output_tokens))))


class CheckpointAppender:
    """Thread-safe append writer for proposition JSONL checkpoints."""

    def __init__(self, out_path: str, initial_count: int = 0) -> None:
        self.path = Path(out_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._total_count = initial_count
        if not self.path.exists():
            self.path.touch()

    def append(self, propositions: list[dict]) -> int:
        """
        Append propositions and return running total count.
        Called after every completed chunk.
        """
        with self._lock:
            if propositions:
                with open(self.path, "a", encoding="utf-8") as f:
                    for prop in propositions:
                        f.write(json.dumps(prop, ensure_ascii=False) + "\n")
                self._total_count += len(propositions)
            return self._total_count

    @property
    def total_count(self) -> int:
        with self._lock:
            return self._total_count


_thread_local = threading.local()


def _get_thread_client() -> anthropic.Anthropic:
    if not hasattr(_thread_local, "client"):
        _thread_local.client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )
    return _thread_local.client


def _extract_chunk_lines(
    chunk_text: str,
    prompt_template: str,
    limiter: SlidingWindowRateLimiter,
) -> tuple[list[str], bool]:
    """
    Extract proposition lines for one chunk.
    Returns (lines, had_api_error).
    """
    prompt = prompt_template.format(chunk_text=chunk_text)

    while True:
        limiter.wait_for_request_slot()
        try:
            client = _get_thread_client()
            message = client.messages.create(
                model=PROPOSITIONS_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip() if message.content else ""

            output_tokens = 0
            usage = getattr(message, "usage", None)
            if usage is not None:
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            if output_tokens <= 0:
                # Fallback estimate only if API usage metadata is unavailable.
                output_tokens = max(1, len(raw) // 4)
            limiter.record_output_tokens(output_tokens)
            return _parse_proposition_lines(raw), False

        except anthropic.RateLimitError:
            print("RateLimitError hit. Waiting 60 seconds, then retrying same chunk...")
            time.sleep(60)
            continue
        except Exception as exc:
            print(f"API error on chunk; skipping chunk. Error: {exc}")
            return [], True


def _process_one_chunk(
    chunk: dict,
    prompt_template: str,
    limiter: SlidingWindowRateLimiter,
) -> dict:
    """
    Worker for one chunk.
    Returns:
      {
        chunk_id, chapter_num, propositions, count, api_error
      }
    """
    chunk_id = chunk["chunk_id"]
    etype = chunk.get("element_type", "paragraph")
    lines: list[str] = []
    had_error = False

    if etype == "paragraph":
        lines, had_error = _extract_chunk_lines(chunk["text"], prompt_template, limiter)
    elif etype in ("table", "figure_caption"):
        lines = [chunk["text"].strip()] if chunk.get("text", "").strip() else []
    else:
        # Should not happen for base chunks, but keep safe.
        lines = []

    propositions: list[dict] = []
    for line in lines:
        prop = _build_proposition(chunk, line)
        errors = validate_proposition(prop)
        if errors:
            continue
        propositions.append(prop)

    return {
        "chunk_id": chunk_id,
        "chapter_num": int(chunk.get("chapter_num", 0)),
        "propositions": propositions,
        "count": len(propositions),
        "api_error": had_error,
    }


def run_propositions(
    out_path: str,
    workers: int = 5,
    process_all: bool = False,
    limit: int = 50,
) -> dict:
    from config import cfg

    base_chunks = _load_base_chunks(cfg.domain_path("chunks"))
    target_chunks = base_chunks if process_all else base_chunks[:limit]
    total_target = len(target_chunks)
    target_chunk_ids = {c["chunk_id"] for c in target_chunks}

    existing_props = _load_jsonl(out_path)
    processed_chunk_ids = {
        p.get("parent_chunk_id", "")
        for p in existing_props
        if p.get("parent_chunk_id")
    }
    processed_chunk_ids = {cid for cid in processed_chunk_ids if cid in target_chunk_ids}

    pending_chunks = [c for c in target_chunks if c["chunk_id"] not in processed_chunk_ids]
    prompt_template = cfg.prompts.proposition_extraction

    limiter = SlidingWindowRateLimiter(
        request_limit=45,
        output_token_limit=7000,
        window_seconds=60.0,
    )
    writer = CheckpointAppender(out_path=out_path, initial_count=len(existing_props))

    api_error_chunks: list[str] = []
    chunk_prop_count: dict[str, int] = defaultdict(int)
    chapter_prop_count: dict[int, int] = defaultdict(int)

    # Seed counts for existing propositions in resume mode.
    for p in existing_props:
        cid = p.get("parent_chunk_id")
        if cid in target_chunk_ids:
            chunk_prop_count[cid] += 1
            ch = int(p.get("chapter_num", 0) or 0)
            chapter_prop_count[ch] += 1

    overall_done = len(processed_chunk_ids)
    run_done = 0
    start = time.time()

    print(f"Using model: {PROPOSITIONS_MODEL}")
    print(f"Workers: {workers}")
    print(f"Base chunks available: {len(base_chunks)}")
    print(f"Target chunks: {total_target}")
    print(f"Already processed in output: {len(processed_chunk_ids)}")
    print(f"Pending chunks: {len(pending_chunks)}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_process_one_chunk, chunk, prompt_template, limiter): chunk
            for chunk in pending_chunks
        }

        for future in as_completed(future_map):
            chunk = future_map[future]
            chunk_id = chunk["chunk_id"]

            try:
                result = future.result()
            except Exception as exc:
                print(f"Worker crash on chunk {chunk_id}; skipping. Error: {exc}")
                result = {
                    "chunk_id": chunk_id,
                    "chapter_num": int(chunk.get("chapter_num", 0) or 0),
                    "propositions": [],
                    "count": 0,
                    "api_error": True,
                }

            if result["api_error"]:
                api_error_chunks.append(chunk_id)

            chunk_prop_count[chunk_id] = result["count"]
            if result["count"] > 0:
                chapter_prop_count[result["chapter_num"]] += result["count"]

            total_props = writer.append(result["propositions"])

            run_done += 1
            overall_done += 1
            elapsed_min = (time.time() - start) / 60.0
            rate = (run_done / elapsed_min) if elapsed_min > 0 else 0.0
            print(
                f"Chunk {overall_done}/{total_target} | propositions total: {total_props} "
                f"| elapsed: {elapsed_min:.2f} min | rate: {rate:.2f} chunks/min"
            )

    elapsed_total_min = (time.time() - start) / 60.0

    # Reload final output to guarantee consistent post-run stats.
    final_props = _load_jsonl(out_path)
    final_target_props = [p for p in final_props if p.get("parent_chunk_id") in target_chunk_ids]

    final_chunk_count = Counter(p.get("parent_chunk_id") for p in final_target_props)
    avg_props_per_chunk = (
        len(final_target_props) / total_target if total_target else 0.0
    )

    chapter_distribution = {ch: 0 for ch in range(1, 29)}
    for p in final_target_props:
        ch = int(p.get("chapter_num", 0) or 0)
        if 1 <= ch <= 28:
            chapter_distribution[ch] += 1

    top5 = sorted(chapter_distribution.items(), key=lambda x: x[1], reverse=True)[:5]
    bottom5 = sorted(chapter_distribution.items(), key=lambda x: x[1])[:5]

    return {
        "model": PROPOSITIONS_MODEL,
        "workers": workers,
        "output_path": out_path,
        "chunks_total_target": total_target,
        "chunks_processed_this_run": run_done,
        "chunks_already_processed": len(processed_chunk_ids),
        "total_propositions_target_scope": len(final_target_props),
        "avg_props_per_chunk": avg_props_per_chunk,
        "api_error_chunks": api_error_chunks,
        "elapsed_minutes": elapsed_total_min,
        "chapter_distribution": chapter_distribution,
        "top5_chapters": top5,
        "bottom5_chapters": bottom5,
        "final_chunk_count_map": dict(final_chunk_count),
    }


if __name__ == "__main__":
    import argparse

    from config import cfg

    parser = argparse.ArgumentParser(
        description="Extract propositions with parallel workers and resume support."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all base chunks (default is first N).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="If --all is not set, process first N base chunks (default: 50).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Parallel worker count (default: 5).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=cfg.domain_path("propositions"),
        help="Output JSONL path (default: cfg.domain_path('propositions')).",
    )
    args = parser.parse_args()

    stats = run_propositions(
        out_path=args.output,
        workers=args.workers,
        process_all=args.all,
        limit=args.limit,
    )

    chapter_line = " | ".join(
        f"Ch {ch}: {stats['chapter_distribution'][ch]} props" for ch in range(1, 29)
    )
    print("\n" + "=" * 80)
    print("PROPOSITIONS RUN SUMMARY")
    print("=" * 80)
    print(f"Model                         : {stats['model']}")
    print(f"Workers                       : {stats['workers']}")
    print(f"Chunks total target           : {stats['chunks_total_target']}")
    print(f"Chunks already processed      : {stats['chunks_already_processed']}")
    print(f"Chunks processed this run     : {stats['chunks_processed_this_run']}")
    print(f"Total propositions produced   : {stats['total_propositions_target_scope']}")
    print(f"Average props/chunk           : {stats['avg_props_per_chunk']:.2f}")
    print(f"API-error chunks              : {len(stats['api_error_chunks'])}")
    if stats["api_error_chunks"]:
        print(f"API-error chunk_ids           : {stats['api_error_chunks']}")
    print(f"Total time                    : {stats['elapsed_minutes']:.2f} minutes")
    print(f"Output path                   : {stats['output_path']}")
    print("-" * 80)
    print("Chapter distribution")
    print(chapter_line)
    print("-" * 80)
    print(
        "Top 5 chapters by proposition count: "
        + ", ".join(f"Ch {ch}={count}" for ch, count in stats["top5_chapters"])
    )
    print(
        "Bottom 5 chapters by proposition count: "
        + ", ".join(
            f"Ch {ch}={count}{' (UNDER 100)' if count < 100 else ''}"
            for ch, count in stats["bottom5_chapters"]
        )
    )
    print("=" * 80)
