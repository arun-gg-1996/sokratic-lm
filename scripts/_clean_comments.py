"""
scripts/_clean_comments.py
Conservative comment cleaner. Strips pure-noise tokens from source files
(date stamps, ticket refs, doc cross-links) without touching code.

Run:
 python3 scripts/_clean_comments.py path/to/file.py [more files...]
 python3 scripts/_clean_comments.py --all # walk the canonical source tree
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files / dirs to walk when --all is passed.
SOURCE_DIRS = ["backend", "conversation", "memory", "retrieval", "ingestion",
               "evaluation", "tools", "scripts", "frontend/src", "tests"]
EXCLUDE = {"__pycache__", "node_modules", "dist", ".venv"}
EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".sh"}

def clean(text: str) -> str:
    """Strip noise patterns. Safe ordering: drop whole-line noise first
 then trim mid-comment refs, then collapse leftover punctuation."""

    # 1. Whole-line removals — when the line is *only* a noise comment.
    whole_line_patterns = [
        # "# "
        r"^\s*#\s*POST_DEMO_FIXES\.md[^\n]*\n",
        # "# "
        r"^\s*#\s*SWEEP_REPORT[^\n]*\n",
        # "#
        r"^\s*#\s*Co-Authored-By:[^\n]*\n",
    ]
    for p in whole_line_patterns:
        text = re.sub(p, "", text, flags=re.M)

    # 2. Inline-fragment removals: (pattern, replacement) tuples.
    inline_patterns: list[tuple[str, str]] = [
        # "" → ""
        (r"\b[A-Z]\d+\s*\(POST_DEMO_FIXES\.md,?\s*\d{4}-\d{2}-\d{2}\):\s*", ""),
        # "" → ""
        (r"\s*\(POST_DEMO_FIXES\.md,?\s*\d{4}-\d{2}-\d{2}\)", ""),
        # "" / "" → ""
        (r"\s*POST_DEMO_FIXES\.md,?", ""),
        # "" → ""
        (r"\bBLOCK\s+\d+\s*\(REAL-Q\d+\)\s*[—-]\s*", ""),
        # "" → ""
        (r"\bBLOCK\s+\d+\s*[—-]\s*", ""),
        # "" → ""
        (r"\bREAL-Q\d+\s*[—-]\s*", ""),
        # Date stamps like "" → ""
        (r"\b\d{4}-\d{2}-\d{2}\s+(?:demo-(?:feedback|prep)|follow-up)\s*:?\s*", ""),
        # "" inline parenthetical
        (r"\s*\(2026-\d{2}-\d{2}\)", ""),
        # ""
        (r",\s*2026-\d{2}-\d{2}\b", ""),
        # " shipped/added/integrated/wrote ..."
        (r"\bCodex\s+(?:shipped|added|integrated|wrote|landed|provided)\s+", ""),
        # Bare ticket refs at start of comment line: "# — ", "# — ", "# — "
        (r"^(\s*#\s*)[A-Z]{1,3}\d{1,3}\s*[—-]\s*", r"\1"),
        # Same for docstring lines: "..."
        (r"^(\s+)[A-Z]{1,3}\d{1,3}\s*[—-]\s+", r"\1"),
        # "clarification" → ""
        (r"\bper\s+[A-Z]\d{1,3}'s\s+", ""),
        # "" → ""
        (r"\bper\s+[A-Z]\d{1,3}\b\s*", ""),
    ]
    for pat, repl in inline_patterns:
        text = re.sub(pat, repl, text, flags=re.M)

    # 3. Collapse double-blank-lines back to single (cleanups can leave gaps)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 4. Strip lines that are now just "# " or "#" after the trim
    text = re.sub(r"^\s*#\s*$\n", "", text, flags=re.M)

    # 5. Strip trailing whitespace
    text = re.sub(r"[ \t]+\n", "\n", text)

    return text

def walk_sources() -> list[Path]:
    out: list[Path] = []
    for sub in SOURCE_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE for part in p.parts):
                continue
            if p.suffix not in EXTENSIONS:
                continue
            out.append(p)
    return out

def main() -> int:
    args = sys.argv[1:]
    if args == ["--all"]:
        targets = walk_sources()
    else:
        targets = [Path(a) for a in args]
    n_changed = 0
    for path in targets:
        path = path.resolve()
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:
            continue
        cleaned = clean(src)
        if cleaned != src:
            path.write_text(cleaned, encoding="utf-8")
            n_changed += 1
            try:
                rel = path.relative_to(ROOT)
            except ValueError:
                rel = path
            print(f"  cleaned  {rel}")
    print(f"\nDONE: {n_changed} file(s) modified out of {len(targets)} scanned")
    return 0

if __name__ == "__main__":
    sys.exit(main())
