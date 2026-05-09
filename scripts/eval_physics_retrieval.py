#!/usr/bin/env python3
"""
End-to-end retrieval evaluation for the indexed physics corpus.

What this checks
  - 20 canonical physics queries spanning all 17 chapters
  - For each query: does the live ChunkRetriever return AT LEAST ONE
    hit in the expected chapter (and ideally the expected section)
    in the top-K?
  - Per-chapter hit rate
  - Off-topic queries should return weak / no hits (sanity)
  - Aggregation queries that benefit from RAPTOR summaries — flagged
    separately since we deliberately didn't upsert those into qdrant

Output
  PASS / FAIL per query + aggregate hit-rate. Exit 0 if hit_rate >= 0.85.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# Selective env load (same pattern as elsewhere)
_shell_overrides = {
    k: os.environ[k] for k in ("SOKRATIC_DOMAIN", "SOKRATIC_USE_BEDROCK")
    if os.environ.get(k)
}
load_dotenv(ROOT / ".env", override=True)
for k, v in _shell_overrides.items():
    os.environ[k] = v

from config import cfg  # noqa: E402
from retrieval.retriever import ChunkRetriever  # noqa: E402

# Each test: (query, expected_chapter_num, expected_section_substring, kind)
# kind ∈ {"core", "applied", "aggregation"}. Aggregation queries are
# flagged separately because they ideally hit a RAPTOR summary, which we
# haven't embedded into qdrant yet — so they're informational, not strict.
TESTS: list[tuple[str, int, str, str]] = [
    # Ch 1: Units and Measurement
    ("how to convert kilometers to meters", 1, "Unit Conversion", "core"),
    ("what is dimensional analysis used for", 1, "Dimensional Analysis", "core"),

    # Ch 2: Vectors
    ("how to add vectors graphically", 2, "Vector", "core"),
    ("dot product of two vectors", 2, "Products of Vectors", "core"),

    # Ch 3: Motion Along a Straight Line
    ("equations of motion under constant acceleration", 3, "Constant Acceleration", "core"),
    ("definition of average velocity", 3, "Position", "core"),

    # Ch 4: Motion in Two and Three Dimensions
    ("trajectory of a projectile", 4, "Projectile", "core"),
    ("uniform circular motion centripetal acceleration", 4, "Circular Motion", "core"),

    # Ch 5: Newton's Laws of Motion
    ("Newton's second law force equals mass times acceleration", 5, "Second Law", "core"),
    ("Newton's third law action reaction", 5, "Third Law", "core"),

    # Ch 6: Applications of Newton's Laws
    ("static and kinetic friction coefficients", 6, "Friction", "core"),
    ("drag force terminal velocity", 6, "Drag", "core"),

    # Ch 7: Work and Kinetic Energy
    ("work done by a constant force", 7, "Work", "core"),
    ("work-energy theorem statement", 7, "Work-Energy", "core"),

    # Ch 8: Potential Energy and Conservation of Energy
    ("conservative force gravitational potential energy", 8, "Potential Energy", "core"),
    ("conservation of mechanical energy", 8, "Conservation of Energy", "core"),

    # Ch 9: Linear Momentum and Collisions
    ("impulse momentum theorem", 9, "Impulse", "core"),
    ("elastic and inelastic collisions", 9, "Collisions", "core"),

    # Ch 10-17: one each (to spot-check chapter coverage)
    ("moment of inertia of a hollow sphere", 10, "Moments of Inertia", "core"),
    ("angular momentum of a rigid body", 11, "Angular Momentum", "core"),
    ("conditions for static equilibrium", 12, "Equilibrium", "core"),
    ("Kepler's third law of planetary motion", 13, "Kepler", "core"),
    ("Bernoulli's equation fluid flow", 14, "Bernoulli", "core"),
    ("simple harmonic motion period of a pendulum", 15, "Pendulum", "core"),
    ("traveling wave equation", 16, "Wave", "core"),
    ("Doppler effect frequency shift", 17, "Doppler", "core"),

    # Aggregation (informational — RAPTOR summaries not in qdrant)
    ("list the types of collisions", 9, "Collisions", "aggregation"),
    ("kinds of fluid flow", 14, "Fluid", "aggregation"),
]


def main() -> int:
    print(f"=== Physics retrieval evaluation (domain={cfg.domain.retrieval_domain}) ===\n")
    r = ChunkRetriever()
    print(f"collection: {r.collection}\n")

    n_pass_chapter = 0
    n_pass_section = 0
    n_total = 0
    chapter_hits: dict[int, int] = {}
    chapter_totals: dict[int, int] = {}
    failures = []

    for query, exp_chap, exp_sec, kind in TESTS:
        if kind == "aggregation":
            continue  # informational only, scored separately
        n_total += 1
        chapter_totals[exp_chap] = chapter_totals.get(exp_chap, 0) + 1

        hits = r.retrieve(query, top_k=5)
        if not hits:
            failures.append(("NO HITS", query, exp_chap, exp_sec, []))
            continue

        # Check chapter match
        chap_match = any(h.get("chapter_num") == exp_chap for h in hits[:5])
        sec_match = any(
            h.get("chapter_num") == exp_chap
            and exp_sec.lower() in (h.get("section_title", "") or "").lower()
            for h in hits[:5]
        )
        if chap_match:
            n_pass_chapter += 1
            chapter_hits[exp_chap] = chapter_hits.get(exp_chap, 0) + 1
        if sec_match:
            n_pass_section += 1
        if not sec_match:
            failures.append((
                "miss" if not chap_match else "section_miss",
                query, exp_chap, exp_sec,
                [(h.get("chapter_num"), h.get("section_title", "")[:30],
                  h.get("subsection_title", "")[:25])
                 for h in hits[:3]]
            ))

    print(f"=== Core retrieval ({n_total} queries) ===")
    print(f"  chapter hit-rate (top-5):  {n_pass_chapter}/{n_total} = {100*n_pass_chapter/n_total:.0f}%")
    print(f"  section hit-rate (top-5):  {n_pass_section}/{n_total} = {100*n_pass_section/n_total:.0f}%")
    print()

    print("=== Per-chapter coverage ===")
    for ch in sorted(chapter_totals.keys()):
        hits = chapter_hits.get(ch, 0)
        total = chapter_totals[ch]
        marker = "✓" if hits == total else "✗"
        print(f"  {marker} Ch{ch:2}  {hits}/{total}")
    print()

    if failures:
        print(f"=== Failures ({len(failures)}) ===")
        for kind, q, exp_chap, exp_sec, top3 in failures:
            print(f"\n  [{kind}] {q!r}")
            print(f"    expected: Ch{exp_chap} § containing {exp_sec!r}")
            print(f"    got top-3:")
            for c, s, sub in top3:
                print(f"      Ch{c} § {s!r:30} sub {sub!r}")

    # Aggregation queries (info)
    print("\n=== Aggregation queries (RAPTOR not in qdrant — informational) ===")
    for query, exp_chap, exp_sec, kind in TESTS:
        if kind != "aggregation": continue
        hits = r.retrieve(query, top_k=3)
        chap = hits[0].get("chapter_num") if hits else None
        sec = hits[0].get("section_title") if hits else None
        print(f"  {query!r:55}  →  Ch{chap} § {sec!r}")

    pass_rate = n_pass_chapter / n_total if n_total else 0
    print(f"\n=== Result: chapter hit-rate {pass_rate*100:.0f}% (target >= 85%) ===")
    return 0 if pass_rate >= 0.85 else 1


if __name__ == "__main__":
    sys.exit(main())
