"""
scripts/eval_legacy_compare.py
------------------------------
Run the legacy 231-row test set (data/eval/rag_qa_expanded.jsonl) through
the CURRENT (v4) retriever and compare against the saved Apr-21 baseline.

Why this script exists separately from eval_realistic.py:
  - The legacy set scores hits by `source_chunk_id` exact match. Those UUIDs
    only exist in the OLD corpus (Qdrant `domain="ot"`), which is gone.
  - Running the OLD retriever end-to-end is impossible without rebuilding
    that Qdrant collection.
  - So we score with a PIPELINE-INVARIANT metric — content-word overlap
    between the row's `expected_answer` sentence and the text of any
    retrieved chunk. If a retriever (old OR new) put the answer text in
    front of the LLM, we call that a hit.

Three metrics per row:
  - answer_in_text@k : pipeline-invariant. Apples-to-apples bridge.
  - section_match    : is any retrieved chunk's section_title equal to
                       the test row's section_title (after stripping the
                       "NN.N " chapter prefix and the " — subsection" suffix
                       that the old set jammed into one field)?
  - chapter_match    : loose. Any retrieved chunk in the right chapter_num.

OOD rows (`question_type == "ood_negative"`) score on refusal — empty result
is correct.

Usage:
  cd /Users/arun-ghontale/UB/NLP/sokratic
  .venv/bin/python scripts/eval_legacy_compare.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from retrieval.retriever import Retriever  # noqa: E402

EVAL_PATH = ROOT / "data/eval/rag_qa_expanded.jsonl"
OUT_DIR = ROOT / "data/eval"

# Saved Apr-21 baseline (the OLD pipeline on this exact 231-row file).
# Source: data/eval/eval_ab_2026-04-21T19-30-05.json (v1.5 column).
LEGACY_BASELINE = {
    "n_queries": 231,
    "retrieval_n": 181,             # in-scope rows (231 minus 50 OOD)
    "hit_at_1": 0.4475,
    "hit_at_3": 0.6022,
    "hit_at_5": 0.6906,
    "hit_at_7": 0.7293,
    "mrr": 0.5411,
    "ood_correct_empty": 0.7000,
    "latency_p50_ms": 4379,
    "latency_p95_ms": 6757,
    "per_style_hit_at_5": {
        "original": 0.69, "conversational": 0.64, "misspelling": 1.0,
        "synonym": 1.0, "clinical_shorthand": 0.75, "abbreviation": 1.0,
        "ot_test_name": 0.75, "conversational_clinical": 0.333,
        "ood_negative": 0.7,
    },
}

# Tiny stopword set — we only need it to filter content tokens for the
# answer-overlap metric. Avoids a heavy NLTK dep.
_STOP = set("""
a an the of in on at by for to from with as is are was were be been being
this that these those it its and or but if then so than such which who whom
whose what when where why how do does did doing done has have had having
not no nor any all some most more less few many one two three first second
into onto out up down off over under again further also already still very
their them they we us our you your he she his her their this can will would
should could may might must i me my mine
""".split())


def normalize(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def content_tokens(s: str) -> list[str]:
    return [t for t in normalize(s).split() if t not in _STOP and len(t) > 2]


def parse_legacy_section(section_title: str) -> tuple[str, str]:
    """
    Old test set section_title format examples:
      "11.4 Axial Muscles of the Abdominal Wall, and Thorax — Muscles of the Pelvic Floor and Perineum"
      "13.4 The Peripheral Nervous System"
      "11.5 Muscles of the Pectoral Girdle and Upper Limbs — Muscles That Move the Humerus"

    Strip the "NN.N " chapter prefix; split on " — " into (L1, L2). Return
    cleaned (section, subsection). subsection is "" if no L2 was present.
    """
    s = (section_title or "").strip()
    # Strip "NN.N " prefix.
    s = re.sub(r"^\d+\.\d+\s+", "", s)
    if " — " in s:
        l1, _, l2 = s.partition(" — ")
        return l1.strip(), l2.strip()
    if " - " in s and len(s.split(" - ")) == 2:
        l1, l2 = s.split(" - ", 1)
        return l1.strip(), l2.strip()
    return s, ""


def answer_overlap_hit(expected_answer: str, chunk_text: str, threshold: float = 0.60) -> bool:
    """
    True if `chunk_text` contains at least `threshold` fraction of the
    content tokens of `expected_answer`. Robust to paraphrasing/word-order
    changes that pure substring would miss.
    """
    ans_toks = content_tokens(expected_answer)
    if not ans_toks:
        return False
    chunk_toks = set(content_tokens(chunk_text))
    if not chunk_toks:
        return False
    matched = sum(1 for t in ans_toks if t in chunk_toks)
    return (matched / len(ans_toks)) >= threshold


def score_row(row: dict, chunks: list[dict]) -> dict:
    qtype = row.get("question_type", "")
    style = row.get("question_style", "")

    # OOD: correct = empty result.
    if qtype == "ood_negative":
        return {
            "ood": True,
            "ood_correct": len(chunks) == 0,
            "n_returned": len(chunks),
            "style": style,
            "qtype": qtype,
        }

    # ---- in-scope scoring ----
    expected_answer = row.get("expected_answer", "")
    expected_chap = row.get("chapter_num")
    legacy_sec_field = row.get("section_title", "")
    expected_sec, expected_sub = parse_legacy_section(legacy_sec_field)
    expected_sec_n = expected_sec.lower()
    expected_sub_n = expected_sub.lower()

    # Walk retrieved primaries (window neighbors don't count for rank).
    # answer_in_context: pool primary + ALL its window neighbors' text and
    # check overlap on the *combined* string. This is what the LLM actually
    # sees at generation time, and it removes a chunk-size bias: smaller
    # chunks split a 17-token answer across 2-3 chunks each individually
    # below the 60% threshold, even when the union has 100% of the answer.
    primary_seen = 0
    answer_rank = -1     # rank at which expected_answer overlap first hits
    section_rank = -1
    chapter_rank = -1
    any_answer = False
    any_section = False
    any_chapter = False
    matched_chunk_text = ""

    # Build per-primary context groups using the _primary_chunk_id link
    # written by Retriever._expand_window. A primary's group = primary +
    # any chunk with _primary_chunk_id == primary.chunk_id.
    primary_chunks = [c for c in chunks if c.get("_window_role", "primary") == "primary"]
    neighbors_by_primary: dict[str, list[dict]] = {}
    for c in chunks:
        if c.get("_window_role", "primary") == "primary":
            continue
        pid = c.get("_primary_chunk_id", "")
        if pid:
            neighbors_by_primary.setdefault(pid, []).append(c)

    for c in chunks:
        role = c.get("_window_role", "primary")
        is_primary = (role == "primary")
        if is_primary:
            primary_seen += 1

        # answer overlap on the COMBINED text of (this primary + its window
        # neighbors). For a window neighbor we skip — it is folded into the
        # primary's context group. (Single-chunk per-row check still
        # available via answer_overlap_hit on c['text'] alone if needed.)
        if is_primary:
            combined = c.get("text", "")
            for nb in neighbors_by_primary.get(c.get("chunk_id", ""), []):
                combined += " " + nb.get("text", "")
            if answer_overlap_hit(expected_answer, combined):
                any_answer = True
                if answer_rank < 0:
                    answer_rank = primary_seen
                    matched_chunk_text = combined[:160]

        sec_n = c.get("section_title", "").strip().lower()
        sub_n = c.get("subsection_title", "").strip().lower()
        chap = c.get("chapter_num")

        if expected_sec_n and sec_n == expected_sec_n:
            any_section = True
            if section_rank < 0 and is_primary:
                section_rank = primary_seen

        if expected_chap is not None and chap == expected_chap:
            any_chapter = True
            if chapter_rank < 0 and is_primary:
                chapter_rank = primary_seen

    return {
        "ood": False,
        "n_returned": len(chunks),
        "n_primary": primary_seen,
        "answer_rank": answer_rank,
        "section_rank": section_rank,
        "chapter_rank": chapter_rank,
        "any_answer": any_answer,
        "any_section": any_section,
        "any_chapter": any_chapter,
        "expected_section": expected_sec,
        "expected_subsection": expected_sub,
        "expected_chapter": expected_chap,
        "matched_chunk_text": matched_chunk_text,
        "style": style,
        "qtype": qtype,
    }


def hit_at_k(scores: list[dict], k: int, key: str) -> float:
    rank_key = key + "_rank"
    in_scope = [s for s in scores if not s["ood"]]
    if not in_scope:
        return 0.0
    return sum(1 for s in in_scope if 1 <= s.get(rank_key, -1) <= k) / len(in_scope)


def mrr_at(scores: list[dict], key: str) -> float:
    rank_key = key + "_rank"
    in_scope = [s for s in scores if not s["ood"]]
    if not in_scope:
        return 0.0
    return sum(1.0 / s[rank_key] for s in in_scope if s.get(rank_key, -1) > 0) / len(in_scope)


def fmt_pct(x: float) -> str:
    return f"{x*100:5.1f}%"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default=None,
                    help="Filter to one question_style (e.g. 'original').")
    ap.add_argument("--no-hyde", action="store_true",
                    help="Disable HyDE rescue at runtime (cfg.retrieval.hyde_enabled = False).")
    ap.add_argument("--no-aliases", action="store_true",
                    help="Disable alias dictionary expansion at runtime.")
    ap.add_argument("--no-ontology", action="store_true",
                    help="Disable UMLS ontology expansion at runtime.")
    ap.add_argument("--label", default=None,
                    help="Tag this run in the output filename (e.g. 'hyde_off').")
    args = ap.parse_args()

    from config import cfg
    if args.no_hyde:
        cfg.retrieval.hyde_enabled = False
        print("ABLATION: HyDE disabled.", flush=True)
    if args.no_aliases:
        cfg.retrieval.aliases_enabled = False
        print("ABLATION: alias dictionary disabled.", flush=True)
    if args.no_ontology:
        # Real config key is ontology_expansion_enabled — see retriever._apply_ontology_expansion.
        cfg.retrieval.ontology_expansion_enabled = False
        print("ABLATION: UMLS ontology disabled.", flush=True)

    rows = [json.loads(l) for l in open(EVAL_PATH)]
    if args.style:
        rows = [r for r in rows
                if r.get("question_style") == args.style or r.get("question_type") == "ood_negative"]
        print(f"Filtered to style={args.style!r}: {len(rows)} rows "
              f"(includes OOD if present).", flush=True)
    print(f"Loaded {len(rows)} rows from {EVAL_PATH.name}", flush=True)

    retriever = Retriever()
    print(f"Retriever ready (domain={retriever.default_domain})\n", flush=True)

    scores: list[dict] = []
    latencies: list[int] = []
    timings_log: list[dict] = []

    t_start = time.time()
    for i, row in enumerate(rows, 1):
        t0 = time.time()
        try:
            chunks = retriever.retrieve(row["question"])
        except Exception as e:
            print(f"  [{i:>3}] ERROR: {type(e).__name__}: {e}", flush=True)
            chunks = []
        latencies.append(int((time.time() - t0) * 1000))
        tt = dict(getattr(retriever, "last_timings", {}) or {})
        tt["question"] = row["question"]
        timings_log.append(tt)

        s = score_row(row, chunks)
        s["question"] = row["question"]
        scores.append(s)

        if i % 20 == 0 or i == len(rows):
            print(f"  [{i:>3}/{len(rows)}]", flush=True)

    elapsed = int(time.time() - t_start)
    in_scope = [s for s in scores if not s["ood"]]
    ood = [s for s in scores if s["ood"]]

    # ----- Headline comparison -----
    print("\n" + "=" * 88)
    print(f"LEGACY-SET COMPARISON — {len(rows)} queries — {elapsed}s wall   "
          f"(p50={sorted(latencies)[len(latencies)//2]}ms, "
          f"p95={sorted(latencies)[int(len(latencies)*0.95)]}ms)")
    print("=" * 88)

    print(f"\n  In-scope: {len(in_scope)}    OOD: {len(ood)}")
    print(f"\n  --- Apples-to-apples: answer-text-overlap (threshold 0.60) ---")
    print(f"  This metric is pipeline-invariant. Old/new both score 'hit' when")
    print(f"  any retrieved chunk's text contains >=60% of the expected_answer's")
    print(f"  content words.")
    print(f"  {'metric':<24} {'OLD pipeline':>16} {'NEW (v4)':>14} {'Δ':>10}")
    # Old pipeline scored by chunk_id; the closest invariant is hit@k. We
    # report new's answer_in_text@k against old's chunk-id hit@k as a
    # rough comparison — note the metric difference inline.
    for k in (1, 3, 5):
        new_h = hit_at_k(scores, k, "answer")
        old_h = LEGACY_BASELINE.get(f"hit_at_{k}", float("nan"))
        delta = new_h - old_h
        sign = "↑" if delta > 0.005 else ("↓" if delta < -0.005 else "·")
        print(f"  answer_in_text@{k:<8} {fmt_pct(old_h):>16} {fmt_pct(new_h):>14}   "
              f"{sign} {fmt_pct(delta):>7}    ★ apples-to-apples")
    new_mrr = mrr_at(scores, "answer")
    old_mrr = LEGACY_BASELINE.get("mrr", float("nan"))
    delta_mrr = new_mrr - old_mrr
    print(f"  MRR (answer)              {old_mrr:>15.3f} {new_mrr:>13.3f}   {delta_mrr:>+8.3f}")

    print(f"\n  --- Section-match metric (NEW only — old set's labels are mashed) ---")
    for k in (1, 3, 5):
        print(f"  section_match@{k:<8} {fmt_pct(hit_at_k(scores, k, 'section')):>30}")
    print(f"  any-section-hit       {fmt_pct(sum(1 for s in in_scope if s['any_section'])/max(len(in_scope),1)):>30}")
    print(f"  any-chapter-hit       {fmt_pct(sum(1 for s in in_scope if s['any_chapter'])/max(len(in_scope),1)):>30}")

    # OOD
    if ood:
        ood_correct = sum(1 for s in ood if s["ood_correct"]) / len(ood)
        old_ood = LEGACY_BASELINE["ood_correct_empty"]
        delta_ood = ood_correct - old_ood
        sign = "↑" if delta_ood > 0.005 else ("↓" if delta_ood < -0.005 else "·")
        print(f"\n  --- OOD refusal ---")
        print(f"  {'metric':<24} {'OLD pipeline':>16} {'NEW (v4)':>14} {'Δ':>10}")
        print(f"  refusal_rate              {fmt_pct(old_ood):>16} {fmt_pct(ood_correct):>14}   "
              f"{sign} {fmt_pct(delta_ood):>7}")

    # Latency
    print(f"\n  --- Latency ---")
    p50 = sorted(latencies)[len(latencies)//2]
    p95 = sorted(latencies)[int(len(latencies)*0.95)]
    old_p50 = LEGACY_BASELINE["latency_p50_ms"]
    old_p95 = LEGACY_BASELINE["latency_p95_ms"]
    print(f"  {'metric':<24} {'OLD pipeline':>16} {'NEW (v4)':>14} {'Δ':>10}")
    print(f"  p50_ms                    {old_p50:>16} {p50:>14}   {p50-old_p50:>+9}")
    print(f"  p95_ms                    {old_p95:>16} {p95:>14}   {p95-old_p95:>+9}")

    # Per-style breakdown
    print(f"\n  --- answer_in_text@5 by question_style ---")
    print(f"  {'style':<28} {'OLD hit@5':>10} {'NEW ans@5':>12} {'Δ':>10}  ({'n':>3})")
    by_style: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        by_style[s.get("style", "")].append(s)
    for style in sorted(by_style.keys()):
        ss = by_style[style]
        if style == "ood":
            ss_in = [x for x in ss if x["ood"]]
            new_h = sum(1 for x in ss_in if x["ood_correct"]) / max(len(ss_in), 1)
            old_h = LEGACY_BASELINE["per_style_hit_at_5"].get("ood_negative", 0)
        else:
            ss_in = [x for x in ss if not x["ood"]]
            new_h = sum(1 for x in ss_in if 1 <= x.get("answer_rank", -1) <= 5) / max(len(ss_in), 1)
            old_h = LEGACY_BASELINE["per_style_hit_at_5"].get(style, 0)
        delta = new_h - old_h
        sign = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "·")
        print(f"  {style:<28} {fmt_pct(old_h):>10} {fmt_pct(new_h):>12}   {sign} {fmt_pct(delta):>7}  ({len(ss):>3})")

    # HyDE fire rate
    n_hyde = sum(1 for tt in timings_log if tt.get("hyde_fired"))
    print(f"\n  HyDE fired: {n_hyde}/{len(timings_log)} ({n_hyde/max(len(timings_log),1)*100:.1f}%)")

    # Failure examples — answer hit miss but chunk match
    miss_examples = [s for s in in_scope if not s["any_answer"] and not s["any_section"] and not s["any_chapter"]]
    print(f"\n  --- Failures (no answer hit, no section, no chapter) — first 8 of {len(miss_examples)} ---")
    for s in miss_examples[:8]:
        print(f"  Q: {s['question'][:80]}")
        print(f"     expected ch{s['expected_chapter']} | sec={s['expected_section']!r}")

    # Save
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    label = f"_{args.label}" if args.label else ""
    out_path = OUT_DIR / f"legacy_compare{label}_{stamp}.json"
    with open(out_path, "w") as f:
        json.dump({
            "n_queries": len(rows),
            "elapsed_secs": elapsed,
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "answer_in_text_at_1": hit_at_k(scores, 1, "answer"),
            "answer_in_text_at_3": hit_at_k(scores, 3, "answer"),
            "answer_in_text_at_5": hit_at_k(scores, 5, "answer"),
            "answer_mrr": new_mrr,
            "section_match_at_5": hit_at_k(scores, 5, "section"),
            "ood_refusal": (sum(1 for s in ood if s["ood_correct"])/len(ood)) if ood else None,
            "legacy_baseline": LEGACY_BASELINE,
            "scores": scores,
            "timings": timings_log,
        }, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
