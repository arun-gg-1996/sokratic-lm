"""
ingestion/run.py
----------------
CLI entry point for the rebuilt ingestion pipeline (B.7).

Examples:

  Dry run — print the plan, no API calls:
      python -m ingestion.run --source openstax_anatomy --dry-run

  Pilot — first 50 chunks only, stop before Qdrant upsert:
      python -m ingestion.run --source openstax_anatomy \
          --limit 50 --skip-stages embed,bm25,upsert

  Full run, fresh Qdrant collection:
      python -m ingestion.run --source openstax_anatomy --fresh

  Resume after a partial failure — skip already-done stages:
      python -m ingestion.run --source openstax_anatomy \
          --skip-stages parse,chunk,enrich

  Just rebuild BM25 + Qdrant without re-running the LLM:
      python -m ingestion.run --source openstax_anatomy \
          --only-stages embed,bm25,upsert

The CLI is a thin wrapper over `ingestion.core.pipeline.run_pipeline`. All
real logic lives in pipeline.py; this file only parses argv and translates
to PipelineOptions.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

from ingestion.core.pipeline import (  # noqa: E402
    PipelineOptions, run_pipeline,
)
from ingestion.core.propositions_dual import (  # noqa: E402
    DEFAULT_MODEL as DEFAULT_PROPS_MODEL,
)


def _parse_stages(value: str) -> set[str]:
    """Parse comma-separated stage list from CLI."""
    if not value:
        return set()
    return {s.strip() for s in value.split(",") if s.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the ingestion pipeline for a given textbook source.",
    )
    parser.add_argument(
        "--source", default="openstax_anatomy",
        help="source name; matches a directory under ingestion/sources/. "
             "(default: openstax_anatomy)",
    )
    parser.add_argument(
        "--pdf",
        default="data/raw/Anatomy_and_Physiology_2e_-_WEB_c9nD9QL.pdf",
        help="PDF input path (default: OpenStax A&P 2e)",
    )
    parser.add_argument(
        "--output-dir", default="data/processed",
        help="where intermediate JSONL artifacts get written",
    )
    parser.add_argument(
        "--bm25-dir", default="data/indexes",
        help="where BM25 index pickle gets written",
    )

    parser.add_argument(
        "--limit", type=int, default=None,
        help="process only the first N chunks (pilot/test mode)",
    )
    parser.add_argument(
        "--skip-stages", default="",
        help="comma-separated list of stages to skip (parse,chunk,enrich,"
             "dual_task,embed,bm25,upsert)",
    )
    parser.add_argument(
        "--only-stages", default="",
        help="comma-separated list of stages to run exclusively. Wins over "
             "--skip-stages.",
    )

    parser.add_argument(
        "--model", default=DEFAULT_PROPS_MODEL,
        help=f"Anthropic model for the dual-task call (default: {DEFAULT_PROPS_MODEL}). "
             "Note: Sonnet 4-6 silently ignores cache_control in our environment; "
             "Sonnet 4-5 caches correctly. Stick with the default unless you have a reason.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=20,
        help="dual-task max in-flight requests (default: 20)",
    )
    parser.add_argument(
        "--no-cache-warmup", action="store_true",
        help="disable the serial warmup call before the parallel dual-task batch. "
             "Default: warmup ON (single serial call primes the prompt cache).",
    )

    parser.add_argument(
        "--fresh", action="store_true",
        help="wipe and recreate the Qdrant collection before upserting",
    )
    parser.add_argument(
        "--embed-model", default="text-embedding-3-large",
        help="OpenAI embedding model (default: text-embedding-3-large)",
    )

    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the plan + cost estimates, do NOT make API calls",
    )

    args = parser.parse_args(argv)

    only = _parse_stages(args.only_stages)
    skip = _parse_stages(args.skip_stages)
    if only and skip:
        print("warn: --only-stages takes precedence; --skip-stages will be ignored",
              file=sys.stderr)

    opts = PipelineOptions(
        source_name=args.source,
        pdf_path=args.pdf,
        output_dir=args.output_dir,
        bm25_dir=args.bm25_dir,
        limit=args.limit,
        skip_stages=skip,
        only_stages=only or None,
        propositions_model=args.model,
        concurrency=args.concurrency,
        cache_warmup=not args.no_cache_warmup,
        fresh=args.fresh,
        embed_model=args.embed_model,
        dry_run=args.dry_run,
    )

    results = asyncio.run(run_pipeline(opts))

    n_failed = sum(
        1 for r in results
        if not r.skipped and any("err=" in n and not n.endswith("err=0") for n in r.notes)
    )
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
