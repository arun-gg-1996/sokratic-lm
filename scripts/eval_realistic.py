"""
scripts/eval_realistic.py
-------------------------
Run the realistic-student-profile retrieval eval against the v1 retriever.

Each row in data/eval/rag_qa_realistic_v1.jsonl carries:
  - question:            student-style query (S1..S6 profiles or OOD)
  - expected_section:    canonical section_title in the corpus (or "" for OOD)
  - expected_subsection: canonical subsection_title (often "" — section-only)
  - chapter_num:         expected chapter
  - profile:             S1 | S2 | S3 | S4 | S5 | S6 | OOD
  - type:                factual / mechanism / comparison / vague_layman /
                         leading_assertion / terse_concept / hesitant_multipart /
                         abbreviation / clinical_alias / typo / long_multipart /
                         ood_off_topic / ood_gibberish / ood_borderline / ood_empty

Scoring (per query, against the top-k chunks the retriever returns):
  - in-scope query (profile != OOD):
      hit_subsection : any chunk's subsection_title (case-insensitive) ==
                       expected_subsection — only scored when label is set.
      hit_section    : any chunk's section_title       ==  expected_section.
      hit_chapter    : any chunk's chapter_num         ==  expected_chapter.
      MRR is computed at the section level (1 / first rank with section hit).
  - OOD query (profile == OOD):
      Correct outcome = retriever returned [] (refused).

Window expansion is left at the config default (W=2 per base.yaml). The score
considers the whole returned payload — primaries plus expansion neighbors —
since the LLM downstream sees that whole set.

Usage:
  .venv/bin/python scripts/eval_realistic.py
  .venv/bin/python scripts/eval_realistic.py --top-k 5 --window 2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from retrieval.retriever import Retriever  # noqa: E402

EVAL_PATH = ROOT / "data/eval/rag_qa_realistic_v1.jsonl"
OUT_DIR = ROOT / "data/eval"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path)]


def norm(s: str) -> str:
    return (s or "").strip().lower()


def score_row(row: dict, chunks: list[dict]) -> dict:
    """Return per-query scoring details. `chunks` is the full retriever payload
    (primaries + window neighbors), in rank order."""
    profile = row.get("profile", "").upper()
    if profile == "OOD":
        # Empty-result is the correct behavior for off-topic queries.
        return {
            "ood": True,
            "ood_correct": len(chunks) == 0,
            "n_returned": len(chunks),
        }

    expected_sec = norm(row.get("expected_section", ""))
    expected_sub = norm(row.get("expected_subsection", ""))
    expected_chap = row.get("chapter_num")

    # Build per-rank flags. "Rank" here counts only PRIMARY chunks so MRR is
    # well-defined; neighbors count toward "any-hit" but not toward rank.
    primary_seen = 0
    section_rank = -1
    subsection_rank = -1
    chapter_rank = -1
    any_section = False
    any_subsection = False
    any_chapter = False

    for c in chunks:
        role = c.get("_window_role", "primary")
        if role == "primary":
            primary_seen += 1
        sec = norm(c.get("section_title", ""))
        sub = norm(c.get("subsection_title", ""))
        chap = c.get("chapter_num")

        if expected_sec and sec == expected_sec:
            any_section = True
            if section_rank < 0 and role == "primary":
                section_rank = primary_seen
        if expected_sub and sub == expected_sub:
            any_subsection = True
            if subsection_rank < 0 and role == "primary":
                subsection_rank = primary_seen
        if expected_chap is not None and chap == expected_chap:
            any_chapter = True
            if chapter_rank < 0 and role == "primary":
                chapter_rank = primary_seen

    return {
        "ood": False,
        "n_returned": len(chunks),
        "n_primary": primary_seen,
        "hit_section": any_section,
        "hit_subsection": any_subsection if expected_sub else None,
        "hit_chapter": any_chapter,
        "section_rank": section_rank,
        "subsection_rank": subsection_rank,
        "chapter_rank": chapter_rank,
    }


def hit_at_k(scores: list[dict], k: int, key: str) -> float:
    """Fraction of in-scope queries whose `key`_rank is in [1..k]."""
    in_scope = [s for s in scores if not s["ood"]]
    if not in_scope:
        return 0.0
    rank_key = key + "_rank"
    return sum(1 for s in in_scope if 1 <= s.get(rank_key, -1) <= k) / len(in_scope)


def mrr(scores: list[dict], key: str = "section") -> float:
    in_scope = [s for s in scores if not s["ood"]]
    if not in_scope:
        return 0.0
    rank_key = key + "_rank"
    return sum(1.0 / s[rank_key] for s in in_scope if s.get(rank_key, -1) > 0) / len(in_scope)


def fmt_pct(x: float) -> str:
    return f"{x*100:5.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=None,
                    help="Override cfg.retrieval.top_chunks_final. Default: config.")
    ap.add_argument("--window", type=int, default=None,
                    help="Override window_size. Default: cfg.retrieval.window_size.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Run only the first N rows (smoke test).")
    ap.add_argument("--profile", default=None,
                    help="Filter to one profile (e.g. S1, OOD).")
    args = ap.parse_args()

    rows = load_jsonl(EVAL_PATH)
    if args.profile:
        rows = [r for r in rows if r.get("profile", "").upper() == args.profile.upper()]
    if args.limit:
        rows = rows[: args.limit]

    print(f"Loaded {len(rows)} rows from {EVAL_PATH.name}", flush=True)
    retriever = Retriever()
    print(f"Retriever ready (domain={retriever.default_domain}, "
          f"top_k={args.top_k or 'cfg'}, window={args.window if args.window is not None else 'cfg'})\n",
          flush=True)

    scores: list[dict] = []
    latencies: list[int] = []
    timings_log: list[dict] = []
    per_profile: dict[str, list[dict]] = defaultdict(list)
    per_type: dict[str, list[dict]] = defaultdict(list)
    fail_examples: list[dict] = []

    t_start = time.time()
    for i, row in enumerate(rows, 1):
        t0 = time.time()
        try:
            chunks = retriever.retrieve(
                row["question"],
                top_k=args.top_k,
                window_size=args.window,
            )
        except Exception as e:
            print(f"  [{i:>3}] ERROR: {type(e).__name__}: {e}", flush=True)
            chunks = []
        latencies.append(int((time.time() - t0) * 1000))
        # Capture per-stage timings populated by Retriever.retrieve().
        tcap = dict(getattr(retriever, "last_timings", {}) or {})
        tcap["question"] = row["question"]
        tcap["profile"] = row.get("profile", "")
        tcap["type"] = row.get("type", "")
        timings_log.append(tcap)
        s = score_row(row, chunks)
        s["question"] = row["question"]
        s["profile"] = row.get("profile", "")
        s["type"] = row.get("type", "")
        s["expected_section"] = row.get("expected_section", "")
        s["expected_subsection"] = row.get("expected_subsection", "")
        s["expected_chapter"] = row.get("chapter_num")
        # capture top retrieved sections for diagnostics
        s["got_top_sections"] = [
            (c.get("section_title", ""), c.get("subsection_title", ""), c.get("chapter_num"))
            for c in chunks if c.get("_window_role", "primary") == "primary"
        ][:5]
        scores.append(s)
        per_profile[s["profile"]].append(s)
        per_type[s["type"]].append(s)

        # Print per-query terse line
        if s["ood"]:
            mark = "✓" if s["ood_correct"] else "✗"
            print(f"  [{i:>3}] {mark} OOD  n_ret={s['n_returned']:>2}  "
                  f"{row['question'][:60]}", flush=True)
        else:
            sec_mark = "✓" if s["hit_section"] else "·"
            sub_mark = "✓" if s.get("hit_subsection") else ("·" if s.get("hit_subsection") is False else "—")
            cha_mark = "✓" if s["hit_chapter"] else "·"
            print(f"  [{i:>3}] sec{sec_mark} sub{sub_mark} ch{cha_mark}  "
                  f"r={s['section_rank']:>2}  {s['profile']:>3}/{s['type']:<18}  "
                  f"{row['question'][:55]}", flush=True)
            if not s["hit_section"] and not s["hit_chapter"]:
                fail_examples.append(s)

    elapsed = int(time.time() - t_start)

    # ----- Aggregate report -----
    print("\n" + "=" * 88)
    print(f"REALISTIC EVAL — {len(rows)} queries — {elapsed}s wall   "
          f"(p50={sorted(latencies)[len(latencies)//2]}ms, "
          f"p95={sorted(latencies)[int(len(latencies)*0.95)] if latencies else 0}ms)")
    print("=" * 88)

    in_scope = [s for s in scores if not s["ood"]]
    ood = [s for s in scores if s["ood"]]
    n_with_sub = sum(1 for s in in_scope if s.get("hit_subsection") is not None)

    print(f"\n  In-scope queries: {len(in_scope)}    OOD queries: {len(ood)}")
    print(f"\n  --- Section-level ---")
    print(f"  hit@1   = {fmt_pct(hit_at_k(scores, 1, 'section'))}")
    print(f"  hit@3   = {fmt_pct(hit_at_k(scores, 3, 'section'))}")
    print(f"  hit@5   = {fmt_pct(hit_at_k(scores, 5, 'section'))}")
    print(f"  any-hit = {fmt_pct(sum(1 for s in in_scope if s.get('hit_section'))/max(len(in_scope),1))}  "
          f"(includes any window-neighbor)")
    print(f"  MRR     = {mrr(scores, 'section'):.3f}")

    print(f"\n  --- Subsection-level ({n_with_sub} queries with subsection label) ---")
    if n_with_sub:
        sub_in = [s for s in in_scope if s.get("hit_subsection") is not None]
        h1 = sum(1 for s in sub_in if 1 <= s.get("subsection_rank", -1) <= 1) / len(sub_in)
        h3 = sum(1 for s in sub_in if 1 <= s.get("subsection_rank", -1) <= 3) / len(sub_in)
        h5 = sum(1 for s in sub_in if 1 <= s.get("subsection_rank", -1) <= 5) / len(sub_in)
        anyhit = sum(1 for s in sub_in if s.get("hit_subsection")) / len(sub_in)
        print(f"  hit@1   = {fmt_pct(h1)}")
        print(f"  hit@3   = {fmt_pct(h3)}")
        print(f"  hit@5   = {fmt_pct(h5)}")
        print(f"  any-hit = {fmt_pct(anyhit)}")

    print(f"\n  --- Chapter-level (loose) ---")
    print(f"  any-hit = {fmt_pct(sum(1 for s in in_scope if s.get('hit_chapter'))/max(len(in_scope),1))}")

    if ood:
        ood_correct = sum(1 for s in ood if s["ood_correct"]) / len(ood)
        print(f"\n  --- OOD ---")
        print(f"  refusal-rate = {fmt_pct(ood_correct)}  ({sum(1 for s in ood if s['ood_correct'])}/{len(ood)})")
        # Per-OOD-type breakdown
        ood_types: dict[str, list[dict]] = defaultdict(list)
        for s in ood:
            ood_types[s["type"]].append(s)
        for t, ss in sorted(ood_types.items()):
            r = sum(1 for x in ss if x["ood_correct"]) / len(ss)
            print(f"    {t:18} {fmt_pct(r)}  ({len(ss)} queries)")

    # ----- Per-profile -----
    print(f"\n  --- Per profile (section any-hit) ---")
    print(f"  {'profile':<8} {'n':>4}  {'sec_any':>8}  {'sec@5':>7}  {'chap_any':>9}  {'MRR':>5}")
    for prof in ["S1", "S2", "S3", "S4", "S5", "S6", "OOD"]:
        ss = per_profile.get(prof, [])
        if not ss:
            continue
        if prof == "OOD":
            r = sum(1 for s in ss if s["ood_correct"]) / len(ss)
            print(f"  {prof:<8} {len(ss):>4}  {'refusal':>8} {fmt_pct(r):>9}")
            continue
        sec_any = sum(1 for s in ss if s.get("hit_section")) / len(ss)
        sec_at5 = sum(1 for s in ss if 1 <= s.get("section_rank", -1) <= 5) / len(ss)
        chap_any = sum(1 for s in ss if s.get("hit_chapter")) / len(ss)
        ranks = [s["section_rank"] for s in ss if s.get("section_rank", -1) > 0]
        mrr_p = sum(1.0 / r for r in ranks) / len(ss) if ss else 0.0
        print(f"  {prof:<8} {len(ss):>4}  {fmt_pct(sec_any):>8}  {fmt_pct(sec_at5):>7}  "
              f"{fmt_pct(chap_any):>9}  {mrr_p:>5.3f}")

    # ----- Per-type -----
    print(f"\n  --- Per query type (in-scope only) ---")
    print(f"  {'type':<22} {'n':>4}  {'sec_any':>8}  {'chap_any':>9}")
    for t in sorted(per_type.keys()):
        ss = [s for s in per_type[t] if not s["ood"]]
        if not ss:
            continue
        sec_any = sum(1 for s in ss if s.get("hit_section")) / len(ss)
        chap_any = sum(1 for s in ss if s.get("hit_chapter")) / len(ss)
        print(f"  {t:<22} {len(ss):>4}  {fmt_pct(sec_any):>8}  {fmt_pct(chap_any):>9}")

    # ----- Failure examples -----
    if fail_examples:
        print(f"\n  --- Failure examples (no section, no chapter hit) — first 12 of {len(fail_examples)} ---")
        for s in fail_examples[:12]:
            top = s["got_top_sections"][:3]
            print(f"  [{s['profile']}/{s['type']}] {s['question'][:70]}")
            print(f"     expected: ch{s['expected_chapter']} | {s['expected_section']!r} | {s['expected_subsection']!r}")
            print(f"     got top:  {top}")

    # ----- Per-stage latency breakdown -----
    if timings_log:
        STAGES = [
            "ontology_ms", "alias_ms", "embed_orig_ms", "qdrant_orig_ms", "bm25_ms",
            "hyde_rewrite_ms", "hyde_embed_ms", "hyde_qdrant_ms",
            "rrf_merge_ms", "expand_parents_ms", "ce_rerank_ms",
            "in_scope_ms", "window_expand_ms",
        ]
        n = len(timings_log)
        n_hyde = sum(1 for tt in timings_log if tt.get("hyde_fired"))
        n_ood_short = sum(1 for tt in timings_log if tt.get("ood_short_circuited"))

        def pct(xs: list[float], p: float) -> float:
            if not xs: return 0.0
            xs = sorted(xs)
            return xs[min(int(len(xs) * p), len(xs) - 1)]

        def mean(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        print(f"\n  --- Per-stage latency (ms) — {n} queries ---")
        print(f"  HyDE fired: {n_hyde}/{n} ({n_hyde/n*100:.1f}%)   "
              f"OOD short-circuit (no HyDE): {n_ood_short}/{n} ({n_ood_short/n*100:.1f}%)")
        print(f"  {'stage':<22} {'mean':>8} {'p50':>8} {'p95':>8} {'max':>8}   "
              f"{'mean_when_fired':>18}")
        for stage in STAGES:
            vals = [float(tt.get(stage, 0.0)) for tt in timings_log]
            fired = [v for v in vals if v > 0]
            if not any(vals):
                continue  # skip never-run stages
            mwf = mean(fired) if fired else 0.0
            print(f"  {stage:<22} {mean(vals):>8.1f} {pct(vals, 0.50):>8.1f} "
                  f"{pct(vals, 0.95):>8.1f} {max(vals):>8.1f}   "
                  f"{mwf:>13.1f} ({len(fired):>3}/{n})")

        totals = [float(tt.get("total_ms", 0.0)) for tt in timings_log]
        print(f"  {'total_ms':<22} {mean(totals):>8.1f} {pct(totals, 0.50):>8.1f} "
              f"{pct(totals, 0.95):>8.1f} {max(totals):>8.1f}")

        # HyDE delta — show what HyDE-fired queries cost vs. not
        no_hyde_totals = [float(tt.get("total_ms", 0.0)) for tt in timings_log if not tt.get("hyde_fired")]
        hyde_totals = [float(tt.get("total_ms", 0.0)) for tt in timings_log if tt.get("hyde_fired")]
        if hyde_totals and no_hyde_totals:
            print(f"\n  HyDE-fired queries:    mean total = {mean(hyde_totals):>7.0f} ms (n={len(hyde_totals)})")
            print(f"  HyDE-skipped queries:  mean total = {mean(no_hyde_totals):>7.0f} ms (n={len(no_hyde_totals)})")
            print(f"  HyDE adds ~{mean(hyde_totals) - mean(no_hyde_totals):.0f} ms when it fires.")

        # Cosine / CE diagnostics — to inform OOD threshold tuning.
        in_scope_t = [tt for tt in timings_log
                      if (tt.get("profile") or "").upper() != "OOD"]
        ood_t = [tt for tt in timings_log
                 if (tt.get("profile") or "").upper() == "OOD"]

        def stats(xs):
            xs = sorted(xs)
            if not xs:
                return (0, 0, 0, 0, 0)
            n = len(xs)
            return (xs[0], xs[n // 4], xs[n // 2], xs[(3 * n) // 4], xs[-1])

        if in_scope_t and ood_t:
            print(f"\n  --- Cosine / CE distribution (for OOD threshold tuning) ---")
            print(f"  {'group':<28} {'min':>7} {'q25':>7} {'p50':>7} {'q75':>7} {'max':>7}")
            for label, group in (("IN-SCOPE max_cosine", [t.get("max_cosine", 0) for t in in_scope_t]),
                                 ("OOD       max_cosine", [t.get("max_cosine", 0) for t in ood_t]),
                                 ("IN-SCOPE max_ce    ", [t.get("max_ce", 0) for t in in_scope_t]),
                                 ("OOD       max_ce    ", [t.get("max_ce", 0) for t in ood_t])):
                mn, q1, m, q3, mx = stats(group)
                print(f"  {label:<28} {mn:>7.3f} {q1:>7.3f} {m:>7.3f} {q3:>7.3f} {mx:>7.3f}")
            # Per-OOD-type cosine — useful for knowing if the off_topic vs out_of_corpus split needs different thresholds
            from collections import defaultdict as _dd
            by_type: dict = _dd(list)
            for tt in ood_t:
                by_type[tt.get("type", "")].append(tt.get("max_cosine", 0))
            for t_name, vals in sorted(by_type.items()):
                mn, q1, m, q3, mx = stats(vals)
                print(f"  OOD/{t_name:<24} {mn:>7.3f} {q1:>7.3f} {m:>7.3f} {q3:>7.3f} {mx:>7.3f}  (n={len(vals)})")

    # ----- Save JSON -----
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = OUT_DIR / f"realistic_eval_{stamp}.json"
    with open(out_path, "w") as f:
        json.dump({
            "n_queries": len(rows),
            "elapsed_secs": elapsed,
            "latency_p50_ms": sorted(latencies)[len(latencies)//2] if latencies else 0,
            "latency_p95_ms": sorted(latencies)[int(len(latencies)*0.95)] if latencies else 0,
            "section_hit_at_1": hit_at_k(scores, 1, 'section'),
            "section_hit_at_3": hit_at_k(scores, 3, 'section'),
            "section_hit_at_5": hit_at_k(scores, 5, 'section'),
            "section_any_hit": sum(1 for s in in_scope if s.get('hit_section'))/max(len(in_scope),1),
            "chapter_any_hit": sum(1 for s in in_scope if s.get('hit_chapter'))/max(len(in_scope),1),
            "section_mrr": mrr(scores, 'section'),
            "ood_refusal_rate": (sum(1 for s in ood if s['ood_correct'])/len(ood)) if ood else None,
            "scores": scores,
            "timings": timings_log,
        }, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
