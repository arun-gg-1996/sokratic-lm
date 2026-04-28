"""
ingestion/core/pipeline.py
--------------------------
Orchestrator for the rebuilt ingestion pipeline (B.7).

Wires the source-specific layer (sources/X/) and the reusable core stages
(parse → filter → chunk → enrich → dual-task → embed → BM25 → upsert)
into a single CLI-driven flow. Stage-level resumability: each stage writes
an intermediate JSONL artifact so a re-run can skip stages already done.

Architecture seam between core and sources:
  core/pipeline.py never imports a specific textbook's parse.py. It calls
  `load_source(name)` which does a dynamic import of the matching module
  in `ingestion/sources/<name>/`. Adding a new textbook in Phase C means
  writing one source module — this orchestrator does not change.

Cache warmup:
  Anthropic's prompt cache only activates after the first call writes the
  cached prefix. With async concurrency, the first ~N parallel calls all
  race the cache write. To avoid that, stage_dual_task does ONE serial
  call as a warmup, waits for it to complete, then launches the rest in
  parallel — every parallel call gets a cache_read hit. Saves ~$15-20 on
  the full-corpus run.

CLI is in ingestion/run.py (kept separate so import-time side effects do
not pollute callers that just want to compose stages programmatically).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ingestion.core.cost_tracker import (
    CostTracker,
    EmbeddingCostTracker,
    MultiTracker,
    make_progress_printer,
)
from ingestion.core.propositions_dual import (
    DEFAULT_MODEL as DEFAULT_PROPS_MODEL,
    DualTaskResult,
    build_cached_system,
    extract_dual_task,
    run_dual_task_batch,
)
from ingestion.core.qdrant import (
    enrich_chunks_with_subsection_id,
    enrich_chunks_with_window_nav,
)


# ── Source module loading ────────────────────────────────────────────────────

@dataclass
class SourceModule:
    """A bundle of the textbook-specific callables and config.

    Loaded dynamically by `load_source(name)`. Each source module exposes:
      - parse_pdf(pdf_path, ...) -> list of raw section dicts (in parse.py)
      - filters / prompt_overrides (optional, may be empty stubs)
      - config.yaml with source-level config
    """
    name: str
    parse_pdf: Callable[..., list[dict]]
    proposition_prompt_suffix: str = ""
    summary_prompt_suffix: str = ""
    config: dict[str, Any] = field(default_factory=dict)


def load_source(name: str) -> SourceModule:
    """Dynamically import sources/<name>/ and bundle its surface."""
    base = f"ingestion.sources.{name}"
    parse_mod = importlib.import_module(f"{base}.parse")
    if not hasattr(parse_mod, "parse_pdf"):
        raise ImportError(f"{base}.parse does not export parse_pdf()")

    suffix_props = ""
    suffix_summary = ""
    try:
        overrides_mod = importlib.import_module(f"{base}.prompt_overrides")
        suffix_props = getattr(overrides_mod, "PROPOSITION_PROMPT_SUFFIX", "") or ""
        suffix_summary = getattr(overrides_mod, "SUMMARY_PROMPT_SUFFIX", "") or ""
    except ImportError:
        pass

    config: dict[str, Any] = {}
    config_path = Path(__file__).resolve().parent.parent / "sources" / name / "config.yaml"
    if config_path.exists():
        try:
            import yaml  # type: ignore[import-untyped]
            with config_path.open() as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"  [pipeline] warn: failed to read {config_path}: {e}")

    return SourceModule(
        name=name,
        parse_pdf=parse_mod.parse_pdf,
        proposition_prompt_suffix=suffix_props,
        summary_prompt_suffix=suffix_summary,
        config=config,
    )


# ── Pipeline options ─────────────────────────────────────────────────────────

@dataclass
class PipelineOptions:
    """Knobs for one pipeline run.

    Stages are: parse, chunk, enrich, dual_task, embed, bm25, upsert.
    """
    source_name: str = "openstax_anatomy"
    pdf_path: str = "data/raw/Anatomy_and_Physiology_2e_-_WEB_c9nD9QL.pdf"
    output_dir: str = "data/processed"
    bm25_dir: str = "data/indexes"

    # Subset filters
    limit: int | None = None     # process only first N chunks (pilot mode)
    skip_stages: set[str] = field(default_factory=set)
    only_stages: set[str] | None = None    # if set, ONLY run these stages

    # Dual-task knobs
    propositions_model: str = DEFAULT_PROPS_MODEL
    concurrency: int = 20
    cache_warmup: bool = True   # serial first call to prime cache

    # Qdrant knobs
    fresh: bool = False         # wipe collection before upsert
    embed_model: str = "text-embedding-3-large"

    # Misc
    dry_run: bool = False       # print plan + cost estimates, no API calls

    def is_active(self, stage: str) -> bool:
        """True if `stage` should run given skip/only filters."""
        if self.only_stages is not None and stage not in self.only_stages:
            return False
        if stage in self.skip_stages:
            return False
        return True


@dataclass
class StageResult:
    name: str
    artifact_path: Path | None
    count: int
    elapsed_s: float
    skipped: bool = False
    notes: list[str] = field(default_factory=list)


# ── Artifact helpers ─────────────────────────────────────────────────────────

def _artifact_path(opts: PipelineOptions, stem: str) -> Path:
    """Where stage output gets written."""
    return Path(opts.output_dir) / f"{stem}_{opts.source_name}.jsonl"


def _bm25_path(opts: PipelineOptions) -> Path:
    return Path(opts.bm25_dir) / f"bm25_{opts.source_name}.pkl"


def _read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── Cache warmup ─────────────────────────────────────────────────────────────

async def warmup_cache(
    chunks: list[dict],
    *,
    model: str,
    cached_system: list[dict],
    tracker: CostTracker | None = None,
) -> dict | None:
    """Run ONE serial dual-task call to populate Anthropic's prompt cache,
    so subsequent parallel calls all hit cache_read.

    Returns the warmup call's usage dict (also recorded by tracker if given).
    """
    if not chunks:
        return None
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(1)
    print("  [pipeline] cache warmup: 1 serial call to prime the cache...")
    t0 = time.time()
    result = await extract_dual_task(
        client, chunks[0], sem,
        model=model,
        cached_system=cached_system,
        usage_callback=tracker.record if tracker else None,
    )
    elapsed = time.time() - t0
    if result.error:
        print(f"  [pipeline] warmup FAILED ({result.error!r}); proceeding without cache.")
        return None
    if result.usage:
        cw = result.usage.get("cache_creation_input_tokens", 0)
        cr = result.usage.get("cache_read_input_tokens", 0)
        in_tok = result.usage.get("input_tokens", 0)
        print(f"  [pipeline] warmup done in {elapsed:.1f}s "
              f"(cache_create={cw}, cache_read={cr}, input={in_tok})")
        if cw == 0 and cr == 0:
            print(f"  [pipeline] WARN: warmup produced no cache_create — "
                  f"either cache is disabled for {model!r} or prompt is below 1024 tokens")
    return result.usage


# ── Stages ───────────────────────────────────────────────────────────────────

def stage_parse(source: SourceModule, opts: PipelineOptions) -> StageResult:
    """Run source.parse_pdf to produce raw section dicts."""
    name = "parse"
    out = _artifact_path(opts, "raw_sections")

    if not opts.is_active(name):
        existing = _read_jsonl(out) if out.exists() else []
        return StageResult(name=name, artifact_path=out if out.exists() else None,
                           count=len(existing), elapsed_s=0.0, skipped=True,
                           notes=[f"skipped (artifact exists: {out.exists()})"])

    if opts.dry_run:
        return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                           skipped=False, notes=["dry-run: would call parse_pdf"])

    t0 = time.time()
    sections = source.parse_pdf(opts.pdf_path, save=False)
    _write_jsonl(out, sections)
    return StageResult(name=name, artifact_path=out, count=len(sections),
                       elapsed_s=time.time() - t0)


def stage_chunk(
    source: SourceModule,
    opts: PipelineOptions,
    sections: list[dict] | None = None,
) -> StageResult:
    """Semantic-split sections into chunks; add overlap chunks; populate
    chunk_type for downstream qdrant payload."""
    name = "chunk"
    out = _artifact_path(opts, "chunks")

    if not opts.is_active(name):
        existing = _read_jsonl(out) if out.exists() else []
        return StageResult(name=name, artifact_path=out if out.exists() else None,
                           count=len(existing), elapsed_s=0.0, skipped=True,
                           notes=[f"skipped (artifact exists: {out.exists()})"])

    if opts.dry_run:
        return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                           notes=["dry-run: would semantic-chunk + overlap"])

    if sections is None:
        sections = _read_jsonl(_artifact_path(opts, "raw_sections"))

    # Lazy import — chunker pulls in llama_index which is heavy.
    from ingestion.core.chunker import (
        semantic_chunk_sections,
        add_overlap_chunks,
        merge_pronoun_start_chunks,
    )

    t0 = time.time()
    base = semantic_chunk_sections(sections)
    base, _merged = merge_pronoun_start_chunks(base)
    chunks = add_overlap_chunks(base)

    # Normalize element_type → chunk_type for the qdrant schema.
    for c in chunks:
        if "chunk_type" not in c:
            c["chunk_type"] = c.get("element_type", "paragraph")

    if opts.limit:
        chunks = chunks[: opts.limit]

    _write_jsonl(out, chunks)
    return StageResult(name=name, artifact_path=out, count=len(chunks),
                       elapsed_s=time.time() - t0)


def stage_enrich(
    opts: PipelineOptions,
    chunks: list[dict] | None = None,
) -> StageResult:
    """Populate subsection_id + window-nav metadata on every chunk."""
    name = "enrich"
    out = _artifact_path(opts, "chunks")

    if not opts.is_active(name):
        return StageResult(name=name, artifact_path=out if out.exists() else None,
                           count=0, elapsed_s=0.0, skipped=True,
                           notes=["skipped"])

    if opts.dry_run:
        return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                           notes=["dry-run: would populate subsection_id + window-nav"])

    if chunks is None:
        if not out.exists():
            return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                               skipped=True,
                               notes=[f"no chunks artifact at {out}; nothing to enrich"])
        chunks = _read_jsonl(out)

    textbook_id = (opts.source_name or "").strip()
    t0 = time.time()
    enrich_chunks_with_subsection_id(chunks, textbook_id=textbook_id)
    enrich_chunks_with_window_nav(chunks)
    _write_jsonl(out, chunks)
    return StageResult(name=name, artifact_path=out, count=len(chunks),
                       elapsed_s=time.time() - t0)


async def stage_dual_task(
    source: SourceModule,
    opts: PipelineOptions,
    chunks: list[dict] | None = None,
    tracker: CostTracker | None = None,
) -> StageResult:
    """Run the dual-task LLM pass: clean chunk text + extract propositions.
    Writes propositions_<source>.jsonl and back-fills cleaned_text into
    chunks_<source>.jsonl."""
    name = "dual_task"
    chunks_out = _artifact_path(opts, "chunks")
    props_out = _artifact_path(opts, "propositions")

    if not opts.is_active(name):
        existing = _read_jsonl(props_out) if props_out.exists() else []
        return StageResult(name=name, artifact_path=props_out if props_out.exists() else None,
                           count=len(existing), elapsed_s=0.0, skipped=True,
                           notes=["skipped"])

    if opts.dry_run:
        # Estimate cost: ~$0.01 per chunk after warmup. Counts are best-effort
        # in dry-run mode since the chunks artifact may not exist yet.
        n_chunks = 0
        if chunks is not None:
            n_chunks = len(chunks)
        elif chunks_out.exists():
            n_chunks = sum(1 for _ in chunks_out.open() if _.strip())
        if opts.limit:
            n_chunks = min(n_chunks, opts.limit) if n_chunks else opts.limit
        est = 0.01 * n_chunks
        return StageResult(name=name, artifact_path=None, count=n_chunks,
                           elapsed_s=0.0,
                           notes=[f"dry-run: would dual-task {n_chunks} chunks "
                                  f"(est ~${est:.2f})"])

    if chunks is None:
        if not chunks_out.exists():
            return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                               skipped=True,
                               notes=[f"no chunks at {chunks_out}"])
        chunks = _read_jsonl(chunks_out)
    if opts.limit:
        chunks = chunks[: opts.limit]

    if tracker is None:
        tracker = CostTracker(model=opts.propositions_model)

    cached_system = build_cached_system(source.proposition_prompt_suffix)
    t0 = time.time()
    if opts.cache_warmup and len(chunks) > 1:
        await warmup_cache(
            chunks, model=opts.propositions_model,
            cached_system=cached_system, tracker=tracker,
        )

    print(f"  [pipeline] dual-task on {len(chunks)} chunks "
          f"@ concurrency={opts.concurrency}, model={opts.propositions_model}")
    results = await run_dual_task_batch(
        chunks,
        model=opts.propositions_model,
        extra_system_suffix=source.proposition_prompt_suffix,
        concurrency=opts.concurrency,
        usage_callback=tracker.record,
        progress_callback=make_progress_printer(tracker, print_every=max(opts.concurrency, 20)),
    )

    n_ok = sum(1 for r in results if r.error is None)
    n_err = len(results) - n_ok
    # Back-fill cleaned text into chunks; collect propositions.
    chunks_by_id = {c["chunk_id"]: c for c in chunks}
    propositions: list[dict] = []
    for r in results:
        if r.error is not None:
            continue
        if r.chunk_id in chunks_by_id and r.cleaned_text:
            chunks_by_id[r.chunk_id]["text"] = r.cleaned_text
        # Inherit chunk metadata onto each proposition record so the embed +
        # upsert stages can build payloads without a second join.
        parent = chunks_by_id.get(r.chunk_id, {})
        for p in r.propositions:
            p_full = dict(p)
            for fld in (
                "chapter_num", "chapter_title", "section_num", "section_title",
                "subsection_title", "subsection_id", "page", "chunk_type",
                "sequence_index", "prev_chunk_id", "next_chunk_id",
                "subsection_chunk_count",
            ):
                if fld in parent and fld not in p_full:
                    p_full[fld] = parent[fld]
            propositions.append(p_full)

    _write_jsonl(chunks_out, list(chunks_by_id.values()))
    _write_jsonl(props_out, propositions)
    elapsed = time.time() - t0
    notes = [f"ok={n_ok}, err={n_err}, total_cost=${tracker.total_cost:.4f}"]
    if n_err:
        notes.append(f"cache_hit_rate={tracker.cache_hit_rate * 100:.1f}%")
    return StageResult(name=name, artifact_path=props_out, count=len(propositions),
                       elapsed_s=elapsed, notes=notes)


def stage_embed_and_bm25(
    opts: PipelineOptions,
    propositions: list[dict] | None = None,
    embed_tracker: EmbeddingCostTracker | None = None,
) -> StageResult:
    """Embed propositions, build BM25 index. Qdrant upsert is stage_upsert."""
    name = "embed_bm25"
    props_path = _artifact_path(opts, "propositions")

    if not opts.is_active("embed") and not opts.is_active("bm25"):
        return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                           skipped=True, notes=["skipped"])

    if opts.dry_run:
        n = 0
        if propositions is not None:
            n = len(propositions)
        elif props_path.exists():
            n = sum(1 for _ in props_path.open() if _.strip())
        n_tokens = n * 50  # rough avg per proposition
        est = (n_tokens / 1_000_000) * 0.13
        return StageResult(name=name, artifact_path=None, count=n,
                           elapsed_s=0.0,
                           notes=[f"dry-run: would build BM25 + embed {n} propositions "
                                  f"(est ~${est:.4f})"])

    if propositions is None:
        if not props_path.exists():
            return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                               skipped=True,
                               notes=[f"no propositions at {props_path}"])
        propositions = _read_jsonl(props_path)

    # NOTE: leaves the actual embedding + BM25 build to the existing
    # core/index.py path to avoid re-implementing batched embed here.
    # The B.7 orchestrator's value-add is the stage sequencing + cache
    # warmup + cost accounting; reusing index.py is intentional.
    from ingestion.core.index import build_bm25_only

    t0 = time.time()
    bm25_path = _bm25_path(opts)
    bm25_path.parent.mkdir(parents=True, exist_ok=True)
    build_bm25_only(propositions, bm25_path=str(bm25_path))
    return StageResult(name=name, artifact_path=bm25_path, count=len(propositions),
                       elapsed_s=time.time() - t0,
                       notes=[f"bm25 -> {bm25_path}"])


def stage_upsert(
    opts: PipelineOptions,
    propositions: list[dict] | None = None,
    chunks: list[dict] | None = None,
    embed_tracker: EmbeddingCostTracker | None = None,
) -> StageResult:
    """Embed propositions and upsert into Qdrant with the v1 payload schema."""
    name = "upsert"
    if not opts.is_active(name):
        return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                           skipped=True, notes=["skipped"])

    props_path = _artifact_path(opts, "propositions")
    chunks_path = _artifact_path(opts, "chunks")

    if opts.dry_run:
        n = 0
        if propositions is not None:
            n = len(propositions)
        elif props_path.exists():
            n = sum(1 for _ in props_path.open() if _.strip())
        return StageResult(name=name, artifact_path=None, count=n,
                           elapsed_s=0.0,
                           notes=[f"dry-run: would upsert {n} points "
                                  f"(fresh={opts.fresh})"])

    if propositions is None:
        if not props_path.exists():
            return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                               skipped=True,
                               notes=[f"no propositions at {props_path}"])
        propositions = _read_jsonl(props_path)
    if chunks is None:
        if not chunks_path.exists():
            return StageResult(name=name, artifact_path=None, count=0, elapsed_s=0.0,
                               skipped=True,
                               notes=[f"no chunks at {chunks_path}"])
        chunks = _read_jsonl(chunks_path)

    # Lazy imports — qdrant + openai clients
    from openai import OpenAI
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    from config import cfg
    from ingestion.core.qdrant import (
        ensure_collection, iter_payload_records, PROMPT_VERSION_DEFAULT,
    )

    if embed_tracker is None:
        embed_tracker = EmbeddingCostTracker(model=opts.embed_model)

    openai_client = OpenAI()
    qdrant = QdrantClient(host=cfg.memory.qdrant_host, port=cfg.memory.qdrant_port)
    collection = cfg.memory.kb_collection
    vector_size = int(cfg.qdrant.vector_size)

    ensure_collection(qdrant, collection, vector_size, fresh=opts.fresh)

    chunks_by_id = {c["chunk_id"]: c for c in chunks}
    t0 = time.time()
    EMBED_BATCH = 100
    UPSERT_BATCH = 200
    points_buffer: list[PointStruct] = []
    upserted = 0

    # Iterate propositions in batches; one OpenAI embed call per batch.
    for i in range(0, len(propositions), EMBED_BATCH):
        batch = propositions[i : i + EMBED_BATCH]
        texts = [p["text"] for p in batch]
        resp = openai_client.embeddings.create(model=opts.embed_model, input=texts)
        embed_tracker.record(getattr(resp.usage, "total_tokens", 0) or 0)
        vectors = [d.embedding for d in resp.data]

        for prop, vec, (_pid, payload) in zip(
            batch, vectors,
            iter_payload_records(batch, chunks_by_id,
                                 textbook_id=opts.source_name,
                                 prompt_version=PROMPT_VERSION_DEFAULT),
        ):
            points_buffer.append(PointStruct(
                id=prop["proposition_id"], vector=vec, payload=payload,
            ))
            if len(points_buffer) >= UPSERT_BATCH:
                qdrant.upsert(collection_name=collection, points=points_buffer)
                upserted += len(points_buffer)
                points_buffer = []

    if points_buffer:
        qdrant.upsert(collection_name=collection, points=points_buffer)
        upserted += len(points_buffer)

    return StageResult(
        name=name, artifact_path=None, count=upserted,
        elapsed_s=time.time() - t0,
        notes=[f"upserted {upserted} points -> {collection}",
               f"embed_cost=${embed_tracker.total_cost:.4f}"],
    )


# ── Top-level orchestration ──────────────────────────────────────────────────

async def run_pipeline(opts: PipelineOptions) -> list[StageResult]:
    """Run the full pipeline. Returns per-stage results for reporting."""
    print(f"\n=== Pipeline run: source={opts.source_name} ===")
    print(f"  PDF:       {opts.pdf_path}")
    print(f"  Output:    {opts.output_dir}")
    if opts.limit:
        print(f"  Limit:     first {opts.limit} chunks (pilot mode)")
    if opts.dry_run:
        print(f"  DRY RUN — no API calls")

    source = load_source(opts.source_name)

    multi = MultiTracker()
    props_tracker = CostTracker(model=opts.propositions_model)
    embed_tracker = EmbeddingCostTracker(model=opts.embed_model)
    multi.add(props_tracker)
    multi.add(embed_tracker)

    results: list[StageResult] = []

    # Sequential stage execution
    r = stage_parse(source, opts);                    results.append(r); _print_stage(r)
    r = stage_chunk(source, opts);                    results.append(r); _print_stage(r)
    r = stage_enrich(opts);                            results.append(r); _print_stage(r)
    r = await stage_dual_task(source, opts, tracker=props_tracker)
    results.append(r); _print_stage(r)
    r = stage_embed_and_bm25(opts);                    results.append(r); _print_stage(r)
    r = stage_upsert(opts, embed_tracker=embed_tracker)
    results.append(r); _print_stage(r)

    print("\n=== Pipeline summary ===")
    for r in results:
        flag = "[skip]" if r.skipped else "[done]"
        print(f"  {flag} {r.name:<14} count={r.count:>6} elapsed={r.elapsed_s:.1f}s "
              f"{' | '.join(r.notes) if r.notes else ''}")
    if not opts.dry_run:
        print()
        print(multi.summary())

    return results


def _print_stage(r: StageResult) -> None:
    flag = "[skip]" if r.skipped else "[done]"
    print(f"  {flag} {r.name:<14} count={r.count:>6} elapsed={r.elapsed_s:.1f}s")
    for note in r.notes:
        print(f"           - {note}")
