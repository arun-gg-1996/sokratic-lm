"""
scripts/unmash_chunks_metadata.py
---------------------------------
Phase B.1 — Un-mash `section_title` and `subsection_title` in chunks_ot.jsonl.

Current state: section_title is mashed like "1.2 Title — Subsection",
and subsection_title duplicates the section_num + section_title prefix.

Target state: section_title is the canonical clean section name (no number,
no em-dash); subsection_title is the actual subsection heading (the part
after the em-dash).

Verified deterministic on 2026-04-28: same section_num always maps to the
same clean section_title across all chunks (0 ambiguities).

Usage:
  python scripts/unmash_chunks_metadata.py --dry-run    # preview changes
  python scripts/unmash_chunks_metadata.py              # apply in place
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNKS_PATH = ROOT / "data/processed/chunks_openstax_anatomy.jsonl"


def unmash(section_title: str, section_num: str) -> tuple[str, str]:
    """Return (clean_section_title, subsection_title).

    - Always strip leading "<section_num> " from prefix if present.
    - Split on " — " when present; everything after becomes subsection.
    """
    section_title = section_title or ""
    section_num = section_num or ""
    if " — " in section_title:
        prefix, _, subheading = section_title.partition(" — ")
    else:
        prefix, subheading = section_title, ""
    if section_num and prefix.startswith(section_num + " "):
        prefix = prefix[len(section_num) + 1 :]
    return prefix.strip(), subheading.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats and a sample of changes without writing the file.",
    )
    ap.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing a .bak copy before overwriting.",
    )
    args = ap.parse_args()

    if not CHUNKS_PATH.exists():
        print(f"ERR: {CHUNKS_PATH} not found", file=sys.stderr)
        return 2

    rows: list[dict] = []
    with CHUNKS_PATH.open() as f:
        for line in f:
            rows.append(json.loads(line))

    changed = 0
    unchanged = 0
    section_title_was_mashed = 0
    subsection_was_overwritten = 0
    secnum_to_clean = defaultdict(set)
    samples = []

    out_rows: list[dict] = []
    for r in rows:
        old_st = r.get("section_title") or ""
        old_sub = r.get("subsection_title") or ""
        sn = r.get("section_num") or ""
        new_st, new_sub = unmash(old_st, sn)

        if " — " in old_st:
            section_title_was_mashed += 1
        if old_sub and new_sub and old_sub != new_sub:
            subsection_was_overwritten += 1

        if (new_st, new_sub) != (old_st, old_sub):
            changed += 1
            if len(samples) < 10:
                samples.append({
                    "old_section_title": old_st,
                    "new_section_title": new_st,
                    "old_subsection": old_sub,
                    "new_subsection": new_sub,
                    "section_num": sn,
                })
        else:
            unchanged += 1

        if sn:
            secnum_to_clean[sn].add(new_st)

        new_row = dict(r)
        new_row["section_title"] = new_st
        new_row["subsection_title"] = new_sub
        out_rows.append(new_row)

    ambiguous = {k: v for k, v in secnum_to_clean.items() if len(v) > 1}

    print(f"Total chunks:           {len(rows)}")
    print(f"  Changed:              {changed}")
    print(f"  Unchanged:            {unchanged}")
    print(f"  Mashed (had em-dash): {section_title_was_mashed}")
    print(f"  Subsection rewritten: {subsection_was_overwritten}")
    print(
        f"  Determinism check:    {len(ambiguous)} section_nums with >1 clean title"
        + ("  ✓ deterministic" if not ambiguous else "  ✗ NON-DETERMINISTIC")
    )
    if ambiguous:
        print("  Ambiguous samples:")
        for k, v in list(ambiguous.items())[:5]:
            print(f"    {k}: {v}")

    print("\nSample transforms:")
    for s in samples[:8]:
        print(
            f"  sn={s['section_num']:>5}  "
            f"'{s['old_section_title'][:50]}' → '{s['new_section_title'][:30]}'  "
            f"sub: '{s['old_subsection'][:30]}' → '{s['new_subsection'][:40]}'"
        )

    if args.dry_run:
        print("\n[dry-run] no file written.")
        return 0

    if ambiguous:
        print(
            "\nABORT: non-deterministic transform — please investigate before writing.",
            file=sys.stderr,
        )
        return 1

    if not args.no_backup:
        backup = CHUNKS_PATH.with_suffix(
            CHUNKS_PATH.suffix + f".bak.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        )
        shutil.copyfile(CHUNKS_PATH, backup)
        print(f"\nBackup written: {backup.relative_to(ROOT)}")

    with CHUNKS_PATH.open("w") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote: {CHUNKS_PATH.relative_to(ROOT)}  ({len(out_rows)} rows)")

    # Post-write verification
    em_dash_remaining = sum(1 for r in out_rows if " — " in (r.get("section_title") or ""))
    print(f"Verification: chunks with ' — ' in section_title after rewrite: {em_dash_remaining}")
    if em_dash_remaining > 0:
        print("  WARNING: some em-dashes survived. Investigate.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
