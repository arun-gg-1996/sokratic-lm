#!/usr/bin/env python3
"""
Produce a clean SQL seed dump for a fresh server install.

Strategy: dump only the static curriculum + abbreviation tables. Skip
every per-student / per-session table so the resulting dump never
contains user data.

Output: data/artifacts/sokratic_seed_<domain>.sql

Usage on the server:
    sqlite3 data/student_state/sokratic_<domain>.sqlite3 < sokratic_seed_<domain>.sql

This is run AFTER migrations are applied — the dump is INSERT statements
only, no schema. The migrations create the tables; the seed populates them.

Pair this with the existing scripts/bootstrap_corpus.py (which pulls the
qdrant collection + BM25 pickle from HF) for a complete fresh-server
bootstrap: clone repo → migrations → load seed → pull corpus → ready.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Tables that contain only static curriculum / lookup data — safe to dump.
SEED_TABLES = ["chapters", "sections", "subsections", "topic_abbreviations"]

# Tables explicitly excluded (per-student / per-session / runtime data).
EXCLUDED_TABLES = [
    "students", "sessions", "subsection_mastery",
    "observations", "messages", "schema_version",
    "sqlite_sequence",
]


def main() -> None:
    from config import cfg
    domain = cfg.domain.retrieval_domain
    db_path = REPO / "data" / "student_state" / f"sokratic_{domain}.sqlite3"
    out_path = REPO / "data" / "artifacts" / f"sokratic_seed_{domain}.sql"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        raise SystemExit(f"DB file not found: {db_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    lines: list[str] = []
    lines.append("-- Auto-generated seed dump from " + db_path.name)
    lines.append("-- Tables: " + ", ".join(SEED_TABLES))
    lines.append("-- Run AFTER migrations have created the schema.")
    lines.append("")
    lines.append("PRAGMA foreign_keys = OFF;")
    lines.append("BEGIN;")
    lines.append("")

    total_rows = 0
    for tbl in SEED_TABLES:
        cur = con.execute(f"SELECT * FROM {tbl}")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        if not rows:
            lines.append(f"-- {tbl}: 0 rows")
            continue
        lines.append(f"-- {tbl}: {len(rows)} rows")
        # Use INSERT OR REPLACE so re-applying the seed is idempotent on a
        # database that already has these rows (e.g. dev resets).
        col_list = ", ".join(cols)
        for row in rows:
            vals = []
            for v in row:
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                else:
                    s = str(v).replace("'", "''")
                    vals.append(f"'{s}'")
            vals_str = ", ".join(vals)
            lines.append(f"INSERT OR REPLACE INTO {tbl}({col_list}) VALUES ({vals_str});")
        total_rows += len(rows)
        lines.append("")

    lines.append("COMMIT;")
    lines.append("PRAGMA foreign_keys = ON;")
    out_path.write_text("\n".join(lines) + "\n")

    print(f"wrote {out_path.relative_to(REPO)}")
    print(f"  {total_rows} rows across {len(SEED_TABLES)} tables")
    print(f"  size: {out_path.stat().st_size:,} bytes")
    print()
    print("On a fresh server install:")
    print("  1. Clone repo, install deps")
    print("  2. python -c 'from memory.sqlite_store import SQLiteStore; SQLiteStore()'  # applies migrations")
    print(f"  3. sqlite3 data/student_state/sokratic_{domain}.sqlite3 < {out_path.relative_to(REPO)}")
    print("  4. python scripts/bootstrap_corpus.py  # pulls qdrant + BM25 from HF")


if __name__ == "__main__":
    main()
