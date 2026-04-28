"""
ingestion/core/qdrant.py
------------------------
Qdrant payload schema + window-navigation metadata for the dual-task pipeline.

Why this module exists separately from `core/index.py`:
  index.py has accumulated mixed concerns (BM25 + Qdrant + diagrams + tests).
  qdrant.py is the single source of truth for the v1 payload schema and the
  helpers that produce a payload from a (chunk, proposition) pair. The B.7
  pipeline orchestrator wires this together with the batch embed/upsert logic.

Schema contract — every Qdrant point's payload contains:

  Identity
    proposition_id           UUID, the embedded unit
    chunk_id                 UUID of the parent chunk this proposition came from
    parent_chunk_text        full text of the parent chunk (for retrieval display)
    text                     proposition text (also embedded)
    textbook_id              e.g. "openstax_anatomy"
    chunk_type               "paragraph" | "paragraph_overlap" | "table" | "figure_caption"
    domain                   legacy alias for textbook_id (kept for back-compat with retriever)

  Hierarchy
    chapter_num              int
    chapter_title            str
    section_num              str   ("20.1")
    section_title            str   (canonical, post un-mash)
    subsection_title         str   (may be empty)
    subsection_id            str   composite "<textbook_id>:<section_num>:<subsection_norm>"

  Window navigation
    sequence_index           int   0-indexed position within subsection_id
    prev_chunk_id            str | None   chunk_id of previous chunk in same subsection
    next_chunk_id            str | None   chunk_id of next chunk in same subsection
    subsection_chunk_count   int   total chunks in this subsection

  Provenance
    page                     int
    prompt_version           str   tags the dual-task prompt version
    ingested_at              str   ISO-8601 UTC timestamp

  Forward hooks (null in v1)
    subsection_summary_id    str | None   populated in Phase D if summaries are added

The BM25 path (build_bm25_only in core/index.py) consumes the same proposition
records so payloads stay in sync between dense and sparse indexes.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

# ── Schema constants ─────────────────────────────────────────────────────────

PROMPT_VERSION_DEFAULT = "v1"
PAYLOAD_FIELDS: tuple[str, ...] = (
    # Identity
    "proposition_id", "chunk_id", "parent_chunk_text", "text",
    "textbook_id", "chunk_type", "domain",
    # Hierarchy
    "chapter_num", "chapter_title",
    "section_num", "section_title",
    "subsection_title", "subsection_id",
    # Window navigation
    "sequence_index", "prev_chunk_id", "next_chunk_id", "subsection_chunk_count",
    # Provenance
    "page", "prompt_version", "ingested_at",
    # Forward hooks
    "subsection_summary_id",
)

VALID_CHUNK_TYPES: frozenset[str] = frozenset({
    "paragraph", "paragraph_overlap", "table", "figure_caption",
})


# ── Subsection ID ────────────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_subsection_id(
    textbook_id: str,
    section_num: str,
    subsection_title: str,
) -> str:
    """
    Build a stable, lookup-friendly composite ID for a subsection.

    Format: "<textbook_id>:<section_num>:<subsection_norm>"

    subsection_norm is lowercase, ASCII-alphanumeric only, with runs of any
    other character collapsed to a single '_'. Empty subsection_title yields
    an empty trailing component (chunk lives at section-level, not in a
    specific subsection).

    Examples:
        ("openstax_anatomy", "20.1", "Shared Structure of Vessels")
            -> "openstax_anatomy:20.1:shared_structure_of_vessels"

        ("openstax_anatomy", "1.3", "")
            -> "openstax_anatomy:1.3:"
    """
    sub_raw = (subsection_title or "").strip().lower()
    sub_norm = _NORM_RE.sub("_", sub_raw).strip("_")
    section = (section_num or "").strip()
    return f"{textbook_id}:{section}:{sub_norm}"


# ── Window-navigation metadata ───────────────────────────────────────────────

def compute_window_nav_metadata(chunks: list[dict]) -> dict[str, dict]:
    """
    Given the full list of chunks emitted by the chunker, compute window
    navigation metadata so the retriever can do W=1 / W=2 expansion via
    prev_chunk_id / next_chunk_id walks.

    Inputs (per chunk dict):
      chunk_id         (str, required)
      subsection_id    (str, required — call normalize_subsection_id first)
      sequence_index   (numeric, required — global ordering from the chunker)
      chunk_type       (str, optional — only "paragraph" + "paragraph_overlap"
                        contribute to navigation; "table" / "figure_caption"
                        get sequence_index but no prev/next chain since they
                        don't appear in the inline narrative flow)

    Returns:
      dict mapping chunk_id -> {
        "sequence_index": int (0-indexed within subsection),
        "prev_chunk_id":  str | None,
        "next_chunk_id":  str | None,
        "subsection_chunk_count": int,
      }

    Sort order within a subsection: by the input chunk's `sequence_index`
    ascending (which reflects the chunker's natural output order). Ties are
    broken by chunk_id for determinism.
    """
    # Group by subsection_id, but only chain navigable chunk_types.
    navigable_types = {"paragraph", "paragraph_overlap"}

    by_subsection: dict[str, list[dict]] = defaultdict(list)
    for c in chunks:
        sub_id = c.get("subsection_id")
        if not sub_id:
            continue
        # All chunk_types appear in subsection_chunk_count, but only navigable
        # types receive prev/next links. Keep this distinction by flagging.
        c_type = c.get("chunk_type", "paragraph")
        c["_navigable"] = c_type in navigable_types
        by_subsection[sub_id].append(c)

    nav: dict[str, dict] = {}
    for sub_id, group in by_subsection.items():
        # Determinism: stable sort on (sequence_index, chunk_id).
        group.sort(key=lambda c: (c.get("sequence_index", 0), c.get("chunk_id", "")))

        navigable = [c for c in group if c.get("_navigable")]

        for i, c in enumerate(group):
            entry = {
                "sequence_index": i,
                "prev_chunk_id": None,
                "next_chunk_id": None,
                "subsection_chunk_count": len(group),
            }
            if c.get("_navigable"):
                # Find this chunk's position in the navigable list and set
                # prev/next from that.
                try:
                    nav_idx = navigable.index(c)
                except ValueError:
                    nav_idx = -1
                if nav_idx > 0:
                    entry["prev_chunk_id"] = navigable[nav_idx - 1]["chunk_id"]
                if 0 <= nav_idx < len(navigable) - 1:
                    entry["next_chunk_id"] = navigable[nav_idx + 1]["chunk_id"]
            nav[c["chunk_id"]] = entry

        # Drop the temporary marker we added.
        for c in group:
            c.pop("_navigable", None)

    return nav


# ── Payload assembly ─────────────────────────────────────────────────────────

def build_payload(
    proposition: dict,
    chunk: dict,
    nav: dict,
    *,
    textbook_id: str,
    prompt_version: str = PROMPT_VERSION_DEFAULT,
    ingested_at: str | None = None,
) -> dict:
    """
    Assemble a Qdrant payload for one proposition derived from one chunk.

    Args:
        proposition  dict with at least {proposition_id, text}.
        chunk        the parent chunk dict (must include chunk_id, chunk_type,
                     chapter/section metadata, page).
        nav          the window-nav entry for this chunk_id (from
                     compute_window_nav_metadata).
        textbook_id  e.g. "openstax_anatomy".
        prompt_version  tag for the dual-task prompt that produced the
                     proposition. Default: PROMPT_VERSION_DEFAULT.
        ingested_at  ISO-8601 UTC timestamp; defaults to now.

    Returns:
        dict whose keys are exactly PAYLOAD_FIELDS.

    Raises:
        ValueError if a required field is missing or chunk_type is invalid.
    """
    if "proposition_id" not in proposition or "text" not in proposition:
        raise ValueError("proposition must include proposition_id and text")
    if "chunk_id" not in chunk:
        raise ValueError("chunk must include chunk_id")
    chunk_type = chunk.get("chunk_type", chunk.get("element_type", "paragraph"))
    if chunk_type not in VALID_CHUNK_TYPES:
        raise ValueError(
            f"invalid chunk_type {chunk_type!r}; must be one of {sorted(VALID_CHUNK_TYPES)}"
        )

    timestamp = ingested_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    sub_id = chunk.get("subsection_id") or normalize_subsection_id(
        textbook_id, chunk.get("section_num", ""), chunk.get("subsection_title", "")
    )

    payload = {
        # Identity
        "proposition_id":       proposition["proposition_id"],
        "chunk_id":             chunk["chunk_id"],
        "parent_chunk_text":    chunk.get("text", ""),
        "text":                 proposition["text"],
        "textbook_id":          textbook_id,
        "chunk_type":           chunk_type,
        # Legacy alias kept so retrieval/retriever.py's hard domain filter
        # still works without a parallel migration. New code should read
        # textbook_id; both fields carry the same value.
        "domain":               textbook_id,

        # Hierarchy
        "chapter_num":          chunk.get("chapter_num", 0),
        "chapter_title":        chunk.get("chapter_title", ""),
        "section_num":          chunk.get("section_num", ""),
        "section_title":        chunk.get("section_title", ""),
        "subsection_title":     chunk.get("subsection_title", ""),
        "subsection_id":        sub_id,

        # Window navigation
        "sequence_index":       nav.get("sequence_index", 0),
        "prev_chunk_id":        nav.get("prev_chunk_id"),
        "next_chunk_id":        nav.get("next_chunk_id"),
        "subsection_chunk_count": nav.get("subsection_chunk_count", 0),

        # Provenance
        "page":                 chunk.get("page", 0),
        "prompt_version":       prompt_version,
        "ingested_at":          timestamp,

        # Forward hooks
        "subsection_summary_id": None,
    }

    # Final shape check: keys must equal PAYLOAD_FIELDS exactly.
    missing = set(PAYLOAD_FIELDS) - set(payload.keys())
    extra = set(payload.keys()) - set(PAYLOAD_FIELDS)
    if missing or extra:
        raise ValueError(
            f"payload schema drift: missing={sorted(missing)} extra={sorted(extra)}"
        )
    return payload


# ── Bulk metadata enrichment ─────────────────────────────────────────────────

def enrich_chunks_with_subsection_id(
    chunks: list[dict],
    *,
    textbook_id: str,
) -> None:
    """In-place: populate `subsection_id` on every chunk that doesn't have one.
    Idempotent: chunks that already have a subsection_id are left alone."""
    for c in chunks:
        if c.get("subsection_id"):
            continue
        c["subsection_id"] = normalize_subsection_id(
            textbook_id,
            c.get("section_num", ""),
            c.get("subsection_title", ""),
        )


def enrich_chunks_with_window_nav(chunks: list[dict]) -> None:
    """In-place: compute and attach window-nav fields to every chunk.

    Calls compute_window_nav_metadata then writes the four nav fields
    (sequence_index, prev_chunk_id, next_chunk_id, subsection_chunk_count)
    onto each chunk. Run this AFTER enrich_chunks_with_subsection_id."""
    nav = compute_window_nav_metadata(chunks)
    for c in chunks:
        entry = nav.get(c["chunk_id"], {})
        c["sequence_index"] = entry.get("sequence_index", 0)
        c["prev_chunk_id"] = entry.get("prev_chunk_id")
        c["next_chunk_id"] = entry.get("next_chunk_id")
        c["subsection_chunk_count"] = entry.get("subsection_chunk_count", 0)


# ── Collection lifecycle ─────────────────────────────────────────────────────

def ensure_collection(
    client,
    name: str,
    vector_size: int,
    *,
    fresh: bool = False,
) -> None:
    """
    Make sure a Qdrant collection exists with the right vector size.

    fresh=True wipes and recreates. fresh=False creates only if missing.
    Raises if the existing collection has a different vector_size.
    """
    from qdrant_client.models import Distance, VectorParams

    try:
        exists = bool(client.collection_exists(name))
    except Exception:
        names = {c.name for c in client.get_collections().collections}
        exists = name in names

    if fresh and exists:
        client.delete_collection(name)
        exists = False

    if not exists:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        return

    # Verify dimension matches if collection already exists.
    info = client.get_collection(name)
    actual = getattr(getattr(getattr(info, "config", None), "params", None), "vectors", None)
    actual_dim = getattr(actual, "size", None)
    if actual_dim is not None and int(actual_dim) != int(vector_size):
        raise ValueError(
            f"collection {name!r} exists with vector_size={actual_dim}, "
            f"but caller passed vector_size={vector_size}. "
            "Pass fresh=True to recreate."
        )


# ── Iteration helper ─────────────────────────────────────────────────────────

def iter_payload_records(
    propositions: Iterable[dict],
    chunks_by_id: dict[str, dict],
    *,
    textbook_id: str,
    prompt_version: str = PROMPT_VERSION_DEFAULT,
    ingested_at: str | None = None,
):
    """
    Yield (proposition_id, payload) pairs for batched upsert.

    Skips propositions whose parent chunk_id can't be resolved (with a warning).
    Caller must have already enriched the chunks with subsection_id and window-nav.
    """
    timestamp = ingested_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    skipped = 0
    for prop in propositions:
        chunk_id = prop.get("parent_chunk_id") or prop.get("chunk_id")
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            skipped += 1
            continue
        nav = {
            "sequence_index": chunk.get("sequence_index", 0),
            "prev_chunk_id": chunk.get("prev_chunk_id"),
            "next_chunk_id": chunk.get("next_chunk_id"),
            "subsection_chunk_count": chunk.get("subsection_chunk_count", 0),
        }
        payload = build_payload(
            prop, chunk, nav,
            textbook_id=textbook_id,
            prompt_version=prompt_version,
            ingested_at=timestamp,
        )
        yield prop["proposition_id"], payload
    if skipped:
        print(f"  [qdrant] skipped {skipped} propositions with unknown parent_chunk_id")
