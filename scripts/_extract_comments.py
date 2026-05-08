"""
scripts/_extract_comments.py

Pulls every comment and module/class/function docstring out of .py files
and prints them as `<path>:<line>:<kind>\\n<body>\\n---\\n` records so a
human (or another tool) can read just the prose without scrolling code.

Usage:
  python3 scripts/_extract_comments.py --module-docs backend/main.py
  python3 scripts/_extract_comments.py --all --kind=module
  python3 scripts/_extract_comments.py path/to/file.py
"""
from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_DIRS = ["backend", "conversation", "memory", "retrieval", "ingestion",
               "evaluation", "tools", "scripts"]
EXCLUDE = {"__pycache__", "node_modules", "dist", ".venv"}


def docstring_ranges(src: str) -> list[tuple[int, int, str, str]]:
    """Return list of (start_line, end_line, kind, text) for each doctring.

    kind is one of: module, class, function.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Module):
            kind = "module"
        elif isinstance(node, ast.ClassDef):
            kind = f"class:{node.name}"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = f"function:{node.name}"
        else:
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            ds = body[0]
            text = ds.value.value
            out.append((ds.lineno, ds.end_lineno or ds.lineno, kind, text))
    return out


def comment_blocks(src: str) -> list[tuple[int, int, str]]:
    """Return list of (start_line, end_line, joined_text) for contiguous
    comment blocks.

    A block = consecutive `#` lines with the same indent (we don't try to
    be clever about subtle continuations — we just group purely-comment
    lines that are adjacent).
    """
    out: list[tuple[int, int, str]] = []
    cur_start = None
    cur_lines: list[str] = []
    cur_end = None
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return out

    # Build a line → list of token types map.  A line is "comment-only" if
    # the only non-NL/INDENT/DEDENT token on that line is a COMMENT.
    line_kind: dict[int, str] = {}
    for tok in tokens:
        ttype, tstring, start, end, line_text = tok
        if ttype in (tokenize.NL, tokenize.NEWLINE,
                     tokenize.INDENT, tokenize.DEDENT,
                     tokenize.ENCODING, tokenize.ENDMARKER):
            continue
        ln = start[0]
        if ttype == tokenize.COMMENT:
            line_kind.setdefault(ln, "comment")
        else:
            line_kind[ln] = "code"

    # Now scan for comment-only line runs.
    src_lines = src.splitlines()
    for i, ln in enumerate(src_lines, start=1):
        if line_kind.get(i) == "comment":
            if cur_start is None:
                cur_start = i
                cur_lines = []
            cur_lines.append(ln)
            cur_end = i
        else:
            if cur_start is not None:
                out.append((cur_start, cur_end or cur_start,
                            "\n".join(cur_lines)))
                cur_start = None
                cur_lines = []
                cur_end = None
    if cur_start is not None:
        out.append((cur_start, cur_end or cur_start, "\n".join(cur_lines)))
    return out


def walk(targets: list[Path]) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        if t.is_dir():
            for p in t.rglob("*.py"):
                if any(part in EXCLUDE for part in p.parts):
                    continue
                out.append(p)
        elif t.suffix == ".py":
            out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--kind", choices=["module", "all", "comments", "long"],
                    default="all")
    ap.add_argument("--min-lines", type=int, default=2,
                    help="for --kind=long, only show blocks >= this many lines")
    args = ap.parse_args()

    if args.all:
        targets = [ROOT / d for d in SOURCE_DIRS if (ROOT / d).is_dir()]
    else:
        targets = [Path(p).resolve() for p in args.paths]
    files = walk(targets)
    files.sort()

    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = f.relative_to(ROOT) if f.is_relative_to(ROOT) else f

        if args.kind in ("module", "all"):
            for start, end, kind, text in docstring_ranges(src):
                if args.kind == "module" and kind != "module":
                    continue
                if args.kind == "long" and (end - start + 1) < args.min_lines:
                    continue
                print(f"\n=== {rel}:{start}-{end} [{kind}] ===")
                print(text.strip("\n"))
                print(f"--- end {kind} ---")

        if args.kind in ("comments", "all", "long"):
            for start, end, text in comment_blocks(src):
                if args.kind == "long" and (end - start + 1) < args.min_lines:
                    continue
                print(f"\n=== {rel}:{start}-{end} [comment-block] ===")
                print(text)
                print("--- end comment-block ---")

    return 0


if __name__ == "__main__":
    sys.exit(main())
