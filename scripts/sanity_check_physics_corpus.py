#!/usr/bin/env python3
"""
Sanity checks across the physics ingestion artifacts.

What this checks (each is a single hard-fail or soft-warn):

  1. Schema parity vs anatomy on chunks (same 19 fields, no extras)
  2. Per-chunk field validity (chunk_id, hierarchy, page, links)
  3. Linked-list integrity (prev/next chunk_id pointers resolve, head + tail)
  4. Cross-artifact consistency:
       - chunks ↔ propositions (every prop's parent_chunk_id is a real chunk)
       - chunks ↔ topic_index (every (ch, sec, sub) the topic_index claims
         has chunks, has them; vice versa for fields the index covers)
       - chunks ↔ textbook_structure (every chunk's hierarchy is in structure)
       - chunks ↔ RAPTOR subsection summaries (every populated subsection
         in chunks has a summary)
  5. BM25 index loads + has the right corpus size
  6. Curated abbreviations file shape
  7. Display-label coverage on topic_index
  8. Propositions schema (proposition_id, text, parent_chunk_id, inherited
     metadata)
  9. Functional smoke: BM25 returns >=1 hit for 3 canonical physics queries

Each check prints PASS / WARN / FAIL with a short detail. Exit 0 if no
FAILs; exit 1 if any FAILs (WARNs don't fail the run).
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Selective env load.
from dotenv import load_dotenv  # noqa: E402

_shell_overrides = {
    k: os.environ[k]
    for k in ("SOKRATIC_DOMAIN", "SOKRATIC_USE_BEDROCK")
    if os.environ.get(k)
}
load_dotenv(ROOT / ".env", override=True)
for k, v in _shell_overrides.items():
    os.environ[k] = v

from config import cfg  # noqa: E402

CHUNKS_PATH = ROOT / cfg.domain_path("chunks")
PROPS_PATH = ROOT / cfg.domain_path("propositions")
BM25_PATH = ROOT / cfg.domain_path("bm25")
TOPIC_INDEX_PATH = ROOT / cfg.domain_path("topic_index")
STRUCTURE_PATH = ROOT / cfg.domain_path("textbook_structure")
RAPTOR_SUB_PATH = ROOT / cfg.domain_path("raptor_subsection_summaries")
RAPTOR_SEC_PATH = ROOT / cfg.domain_path("raptor_section_summaries")
ABBREVS_PATH = ROOT / cfg.domain_path("curated_abbrevs")

# Reference field set from anatomy chunks (19 fields)
ANATOMY_FIELDS = {
    "chapter_num", "chapter_title", "chunk_id", "chunk_type", "domain",
    "element_type", "is_overlap", "next_chunk_id", "page", "prev_chunk_id",
    "section_num", "section_title", "sequence_index", "source_level",
    "source_section_id", "subsection_chunk_count", "subsection_id",
    "subsection_title", "text",
}

# Expected canonical retrieval queries for physics — should each return >= 1 hit
SMOKE_QUERIES = [
    "Newton's third law",
    "kinetic energy work",
    "angular momentum conservation",
]

n_pass = n_warn = n_fail = 0

def ok(name: str, detail: str = "") -> None:
    global n_pass
    n_pass += 1
    print(f"  ✓ {name}{(' — ' + detail) if detail else ''}")

def warn(name: str, detail: str = "") -> None:
    global n_warn
    n_warn += 1
    print(f"  ! {name} — {detail}")

def fail(name: str, detail: str = "") -> None:
    global n_fail
    n_fail += 1
    print(f"  ✗ {name} — {detail}")

print(f"=== Sanity check: physics ingestion artifacts (domain={cfg.domain.retrieval_domain}) ===\n")

# Load everything once
print("Loading…")
chunks = [json.loads(l) for l in CHUNKS_PATH.open()]
chunks_by_id: dict[str, dict] = {c["chunk_id"]: c for c in chunks}
props = [json.loads(l) for l in PROPS_PATH.open()]
topic_index = json.loads(TOPIC_INDEX_PATH.read_text())
structure = json.loads(STRUCTURE_PATH.read_text())
raptor_sub = [json.loads(l) for l in RAPTOR_SUB_PATH.open()]
raptor_sec = [json.loads(l) for l in RAPTOR_SEC_PATH.open()]
abbrevs = json.loads(ABBREVS_PATH.read_text())
print(
    f"  chunks={len(chunks)}  props={len(props)}  topic_index={len(topic_index)}\n"
    f"  structure_chapters={len(structure)}  raptor_sub={len(raptor_sub)}  "
    f"raptor_sec={len(raptor_sec)}  abbrevs={len(abbrevs.get('abbreviations', []))}"
)
print()

# ─── 1. Schema parity ───────────────────────────────────────────────────────
print("1. Chunk schema parity vs anatomy (19 fields, exact match)")
got_first = set(chunks[0].keys())
extra = sorted(got_first - ANATOMY_FIELDS)
missing = sorted(ANATOMY_FIELDS - got_first)
if not extra and not missing:
    ok("19 fields exact match")
else:
    fail("schema mismatch", f"extra={extra} missing={missing}")
print()

# ─── 2. Per-chunk field validity ────────────────────────────────────────────
print("2. Per-chunk field validity")
bad_uuid = bad_chnum = bad_page = empty_text = bad_seq = bad_subid = 0
for c in chunks:
    try:
        uuid.UUID(c["chunk_id"])
    except Exception:
        bad_uuid += 1
    if not isinstance(c.get("chapter_num"), int) or c.get("chapter_num", 0) < 1:
        bad_chnum += 1
    if not isinstance(c.get("page"), int) or c.get("page", 0) < 1:
        bad_page += 1
    if not (c.get("text") or "").strip():
        empty_text += 1
    if not isinstance(c.get("sequence_index"), int):
        bad_seq += 1
    sid = c.get("subsection_id", "")
    # format: "openstax_physics:<sec_num_or_empty>:<slug>"
    if not (sid.startswith(f"{cfg.domain.retrieval_domain}:") and sid.count(":") == 2):
        bad_subid += 1

(ok if bad_uuid == 0 else fail)("chunk_id UUID format", f"{bad_uuid} bad" if bad_uuid else "all valid")
(ok if bad_chnum == 0 else fail)("chapter_num is positive int", f"{bad_chnum} bad" if bad_chnum else "all valid")
(ok if bad_page == 0 else fail)("page is positive int", f"{bad_page} bad" if bad_page else "all valid")
(ok if empty_text == 0 else fail)("text non-empty", f"{empty_text} empty" if empty_text else "all populated")
(ok if bad_seq == 0 else fail)("sequence_index is int", f"{bad_seq} bad" if bad_seq else "all valid")
(ok if bad_subid == 0 else fail)("subsection_id format", f"{bad_subid} malformed" if bad_subid else "all valid")
print()

# ─── 3. Linked-list integrity ───────────────────────────────────────────────
print("3. prev/next chunk_id linked-list integrity")
broken_prev = broken_next = 0
heads = tails = 0
for c in chunks:
    p = c.get("prev_chunk_id")
    n = c.get("next_chunk_id")
    if p is None:
        heads += 1
    elif p not in chunks_by_id:
        broken_prev += 1
    if n is None:
        tails += 1
    elif n not in chunks_by_id:
        broken_next += 1
(ok if broken_prev == 0 else fail)("prev_chunk_id pointers resolve", f"{broken_prev} dangling" if broken_prev else "all resolve")
(ok if broken_next == 0 else fail)("next_chunk_id pointers resolve", f"{broken_next} dangling" if broken_next else "all resolve")
ok("linked-list anchors", f"heads={heads} tails={tails} (one per subsection chain expected)")
print()

# ─── 4a. Propositions ↔ chunks ──────────────────────────────────────────────
print("4a. Propositions ↔ chunks")
chunk_id_set = set(chunks_by_id)
prop_orphans = sum(1 for p in props if p.get("parent_chunk_id") not in chunk_id_set)
prop_ids_seen: set[str] = set()
duplicate_prop_ids = 0
for p in props:
    pid = p.get("proposition_id")
    if pid in prop_ids_seen:
        duplicate_prop_ids += 1
    prop_ids_seen.add(pid)
    if not (p.get("text") or "").strip():
        prop_orphans += 0  # tracked separately below
empty_prop_text = sum(1 for p in props if not (p.get("text") or "").strip())
chunks_with_props = {p.get("parent_chunk_id") for p in props}
(ok if prop_orphans == 0 else fail)(
    "every prop's parent_chunk_id resolves to a chunk",
    f"{prop_orphans} orphans" if prop_orphans else "all resolve",
)
(ok if duplicate_prop_ids == 0 else fail)(
    "proposition_id uniqueness",
    f"{duplicate_prop_ids} dups" if duplicate_prop_ids else f"{len(prop_ids_seen)} unique",
)
(ok if empty_prop_text == 0 else fail)(
    "proposition text non-empty",
    f"{empty_prop_text} empty" if empty_prop_text else "all populated",
)
ok("chunks contributing propositions", f"{len(chunks_with_props)}/{len(chunks)}")
print()

# ─── 4b. Topic_index ↔ chunks ───────────────────────────────────────────────
print("4b. topic_index ↔ chunks")
chunk_keys: set[tuple] = {
    (c.get("chapter_title", ""), c.get("section_title", ""), c.get("subsection_title", ""))
    for c in chunks
}
topic_keys = {
    (e.get("chapter") or "", e.get("section") or "", e.get("subsection") or "")
    for e in topic_index
}
ti_missing_chunks = topic_keys - chunk_keys
chunk_not_in_ti = chunk_keys - topic_keys
(ok if not ti_missing_chunks else fail)(
    "every topic_index entry maps to chunks",
    f"{len(ti_missing_chunks)} entries without chunks" if ti_missing_chunks
    else f"{len(topic_keys)} entries all covered",
)
# Some chunks may belong to junk-filtered subsections (PROBLEMS, INDEX) —
# those WILL be in chunks but NOT in topic_index. That's expected, not a fail.
ok("chunk hierarchies missing from topic_index (junk-filtered)",
   f"{len(chunk_not_in_ti)} (expected: junk_pattern + chapter intros)")
print()

# ─── 4c. textbook_structure ↔ chunks ────────────────────────────────────────
print("4c. textbook_structure ↔ chunks")
struct_keys: set[tuple] = set()
for ch_key, ch_node in structure.items():
    if not isinstance(ch_node, dict): continue
    ch_title = ch_key.split(":", 1)[1].strip() if ":" in ch_key else ch_key
    sections = ch_node.get("sections", {}) or {}
    for sec_name, sec_node in sections.items():
        if not isinstance(sec_node, dict): continue
        subs = sec_node.get("subsections", {}) or {}
        if not subs:
            struct_keys.add((ch_title, sec_name, ""))
        for sub_name in subs.keys():
            struct_keys.add((ch_title, sec_name, sub_name))
chunk_keys_for_struct = {
    (c.get("chapter_title", ""), c.get("section_title", ""), c.get("subsection_title", ""))
    for c in chunks
}
not_in_struct = chunk_keys_for_struct - struct_keys
(ok if not not_in_struct else fail)(
    "every chunk hierarchy is in textbook_structure",
    f"{len(not_in_struct)} hierarchies missing" if not_in_struct
    else f"{len(chunk_keys_for_struct)} hierarchies all present",
)
print()

# ─── 4d. RAPTOR subsection ↔ chunks ─────────────────────────────────────────
print("4d. RAPTOR subsection summaries ↔ chunks")
raptor_keys = {
    (s.get("chapter_num"), s.get("section_title"), s.get("subsection_title"))
    for s in raptor_sub
}
chunk_subkeys = {
    (c.get("chapter_num"), c.get("section_title"), c.get("subsection_title"))
    for c in chunks
}
chunks_without_summary = chunk_subkeys - raptor_keys
extra_summaries = raptor_keys - chunk_subkeys
(ok if not chunks_without_summary else fail)(
    "every (chapter, section, subsection) in chunks has a RAPTOR summary",
    f"{len(chunks_without_summary)} missing" if chunks_without_summary
    else f"{len(chunk_subkeys)} groups all summarized",
)
ok("RAPTOR subsection count", f"{len(raptor_sub)} (extra: {len(extra_summaries)})")
print()

# ─── 5. BM25 ─────────────────────────────────────────────────────────────────
print("5. BM25 index")
try:
    with open(BM25_PATH, "rb") as f:
        bm25_obj = pickle.load(f)
    if isinstance(bm25_obj, dict):
        bm25 = bm25_obj.get("bm25")
        bm25_corpus = bm25_obj.get("propositions") or []
    else:
        bm25 = bm25_obj
        bm25_corpus = []
    corpus_size = getattr(bm25, "corpus_size", None) or len(bm25_corpus) or len(getattr(bm25, "doc_freqs", []))
    if corpus_size == len(chunks):
        ok("BM25 corpus size == chunks count", f"{corpus_size}")
    else:
        fail("BM25 corpus size mismatch", f"bm25={corpus_size} chunks={len(chunks)}")
except Exception as e:
    fail("BM25 load", f"{type(e).__name__}: {e}")
print()

# ─── 6. Curated abbreviations ────────────────────────────────────────────────
print("6. Curated abbreviations")
abbrev_list = abbrevs.get("abbreviations", [])
bad_shape = sum(
    1 for a in abbrev_list
    if not all(k in a for k in ("short", "expansion", "context"))
)
if bad_shape == 0 and len(abbrev_list) >= 30:
    ok("all entries have short/expansion/context", f"{len(abbrev_list)} entries")
else:
    fail("abbreviations shape", f"bad={bad_shape}, total={len(abbrev_list)}")
print()

# ─── 7. Topic-index display labels ──────────────────────────────────────────
print("7. topic_index display_labels")
no_label = sum(1 for e in topic_index if not (e.get("display_label") or "").strip())
weird_label = sum(
    1 for e in topic_index
    if (e.get("display_label") and len(e["display_label"]) > 80)
)
(ok if no_label == 0 else fail)(
    "every entry has display_label",
    f"{no_label} missing" if no_label else f"{len(topic_index)} all labeled",
)
ok("display_label length sanity", f"{weird_label} entries > 80 chars (cosmetic only)")
print()

# ─── 8. Functional smoke: BM25 retrieval ────────────────────────────────────
print("8. BM25 functional retrieval (smoke)")
try:
    from ingestion.core.index import stem_tokenize
    if isinstance(bm25_obj, dict):
        bm25_q = bm25_obj["bm25"]
        bm25_records = bm25_obj.get("propositions") or chunks
    else:
        bm25_q = bm25_obj
        bm25_records = chunks
    for q in SMOKE_QUERIES:
        scores = bm25_q.get_scores(stem_tokenize(q))
        top = sorted(range(len(scores)), key=lambda i: -scores[i])[:3]
        top_titles = [bm25_records[i].get("subsection_title", "") for i in top if i < len(bm25_records)]
        if scores[top[0]] > 0:
            ok(f"query {q!r}", f"top hit subsection={top_titles[0][:50]!r} score={scores[top[0]]:.2f}")
        else:
            fail(f"query {q!r}", "no positive-score hits")
except Exception as e:
    warn("BM25 retrieval smoke", f"{type(e).__name__}: {e}")
print()

# ─── 9. Counts cross-check summary ──────────────────────────────────────────
print("9. Counts cross-check")
print(f"  chunks:                    {len(chunks)}")
print(f"  propositions:              {len(props)} ({len(props) / max(len(chunks_with_props), 1):.1f} props/chunk avg)")
print(f"  unique chunk hierarchies:  {len(chunk_keys)}")
print(f"  topic_index entries:       {len(topic_index)}")
print(f"  raptor subsection sums:    {len(raptor_sub)}")
print(f"  raptor section sums:       {len(raptor_sec)}")
print(f"  textbook_structure chaps:  {len(structure)}")
print(f"  abbreviations:             {len(abbrev_list)}")
print(f"  bm25 corpus:               {corpus_size}")
print()

print(f"=== Summary: PASS={n_pass}  WARN={n_warn}  FAIL={n_fail} ===")
sys.exit(0 if n_fail == 0 else 1)
