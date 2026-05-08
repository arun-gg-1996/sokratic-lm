"""
scripts/_clean_comments_v2.py
AST + tokenize-aware comment / docstring cleaner. Strips noise tokens
from Python source files SAFELY — only touches `#` comments and the
module/class/function docstring strings; never code, never other
string literals.

Run:
 python3 scripts/_clean_comments_v2.py path/to/file.py
 python3 scripts/_clean_comments_v2.py --all
"""
from __future__ import annotations

import ast
import re
import sys
import tokenize
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_DIRS = ["backend", "conversation", "memory", "retrieval", "ingestion",
               "evaluation", "tools", "scripts"]
EXCLUDE = {"__pycache__", "node_modules", "dist", ".venv"}

# ── Noise patterns (applied to comment and docstring text content only) ────

# Whole-token noise to strip; leftover whitespace/punctuation collapses below.
NOISE_PATTERNS: list[tuple[str, str]] = [
    # Sprint / track / block / phase labels
    (r"\bTrack\s+\d+(?:\.\d+)*[a-z]?\b", ""),
    (r"\(Track\s+\d+(?:\.\d+)*[a-z]?\)", ""),
    (r"\btrack\s+\d+(?:\.\d+)*[a-z]?\b", ""),
    (r"\bD\.\d+[a-z]*(?:-\d+)?\b", ""),
    (r"\bBlock\s+[A-Z]\b", ""),
    (r"\bBLOCK\s+\d+\s*\(REAL-Q\d+\)\s*[—-]?\s*", ""),
    (r"\bBLOCK\s+\d+\s*[—-]?\s*", ""),
    (r"\bREAL-Q\d+\b", ""),

    # Bare ticket IDs:
    # Anchored to word boundary, allowing optional "/" combos like " / "
    (r"\bL\d{1,3}\b", ""),
    (r"\bN\d{1,3}\b", ""),
    (r"\bF\d{1,3}\b", ""),
    (r"\bM\d{1,3}\b", ""),
    (r"\bA\d{1,3}\b", ""),
    (r"\bQ\d{1,3}\b", ""),
    (r"\bR\d{1,3}\b", ""),
    (r"\bB\d{1,3}\b", ""),
    (r"\bS\d{1,3}\b", ""),
    (r"\bM-T\d{1,3}\b", ""),
    (r"\bM-FB\b", ""),

    # Date stamps and date-tagged paths
    (r"20\d{2}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", ""),
    (r"\b20\d{2}-\d{2}-\d{2}", ""),
    # — drop the dated subdir reference
    (r"data/artifacts/[a-z_]+/[^\s,]+", ""),

    # Cross-doc references
    (r"\bPOST_DEMO_FIXES\.md\b", ""),
    (r"\bSWEEP_REPORT[_-]?\S*\.md\b", ""),
    (r"\bDEMO_FLOW[_-]?\S*\.md\b", ""),
    (r"\bHANDOFF[_-]?\S*\.md\b", ""),
    (r"\bPRE_DEMO[_-]?\S*\.md\b", ""),
    (r"\bARCHITECTURE_PATTERNS\.md\b", ""),

    # Personal names (first names that appear in the codebase)
    (r"\bNidhi(?:'s)?\b", ""),
    (r"\bArun(?:'s)?\b", ""),
    (r"\bCodex\b", ""),

    # Co-authored / commit attributions
    (r"\bCo-Authored-By:[^\n]*", ""),

    # "" / "" / "" / ""
    (r"\bdemo[ -]day\b", ""),
    (r"\bdemo-(?:feedback|prep)\b", ""),

    # Meta attributions
    (r"\bbumped\s+\d+\s*[-→]\s*\d+\b", ""),
    (r"\bwas\s+bumped\b", ""),

    # "per eval" / "per " / "per X"
    (r"\bper\s+[A-Z]\d+(?:'s)?\s+\w+\s*", ""),
    (r"\bper\s+[A-Z]\d+\b\s*", ""),

    # "Sprint X" / "sprint X"
    (r"\b[Ss]print\s+\d+\b", ""),
    (r"\bRound\s+\d+\b", ""),
    (r"\bWave\s+\d+\b", ""),

    # "B-cell differentiation" demo-spec references — leave (real anatomy)
]

# Comment-block kill markers: if a contiguous # block starts with any of
# these phrases (after the # and any leading whitespace), drop the WHOLE
# block. These are change-history / regression-explanation comments that
# don't describe what the code does today.
KILL_BLOCK_PREFIXES = [
    "Without this",
    "without this",
    "previously",
    "Previously",
    "Was previously",
    "was previously",
    "Earlier this",
    "Earlier version",
    "Sim ",
    "Behavior preserved verbatim",
    "Was previously",
    ": this used to be",
    ": previously this gate",
    ": previously",
    "We used to",
    "It used to",
    "Used to",
    "This was",
    "This used to",
    "Originally",
    "Earlier ",
]

def _clean_text(text: str) -> str:
    """Apply noise removal + cleanup of leftover punctuation."""
    out = text
    for pat, repl in NOISE_PATTERNS:
        out = re.sub(pat, repl, out)
    # Cleanups for cosmetic damage from removals:
    # 1. Collapse runs of spaces
    out = re.sub(r"  +", " ", out)
    # 2. Strip lines that are now just "/ " or "—" leftovers, e.g. ":" -> ":" -> ""
    out = re.sub(r"^\s*[/—-]+\s*", "", out, flags=re.M)
    # 3. Collapse " / " (was " ") to nothing if surrounded by drop-tokens
    out = re.sub(r"\s+/\s+(?:/\s+)+", " ", out)
    # 4. Trailing-only "/ " or "—" at end of line, possibly before a colon/period
    out = re.sub(r"\s*/\s*([:.,])", r"\1", out)
    out = re.sub(r"\s*—\s*([:.,])", r"\1", out)
    # 5. ": —" / ": — " leftovers
    out = re.sub(r":\s*[—-]+\s*$", ":", out, flags=re.M)
    # 6. Empty parens ""
    out = re.sub(r"\(\s*\)", "", out)
    # 7. Empty brackets ""
    out = re.sub(r"\[\s*\]", "", out)
    # 8. Multiple commas: "A, B" -> "A, B"
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r",\s*\)", ")", out)
    out = re.sub(r",\s*$", "", out, flags=re.M)
    # 9. Trailing whitespace
    out = re.sub(r"[ \t]+$", "", out, flags=re.M)
    # 10. Collapse 3+ blank lines to 2
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out

def _is_meaningful_after_clean(comment_text: str) -> bool:
    """After cleanup, is there any real content left?"""
    stripped = re.sub(r"^#\s*", "", comment_text).strip()
    return bool(re.search(r"[a-zA-Z]{3,}", stripped))

def clean_python_source(src: str) -> str:
    """Tokenize source, rewrite COMMENT and module/class/function docstring
 tokens, leave everything else alone. Returns the modified source."""
    # Step 1: identify docstring positions via AST (line numbers).
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # Don't try to clean a file that doesn't parse; just return it.
        return src

    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                # Mark every line of the docstring
                start = body[0].lineno
                end = body[0].end_lineno or start
                for ln in range(start, end + 1):
                    docstring_lines.add(ln)

    # Step 2: tokenize and rebuild.
    out_tokens = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src

    for tok in tokens:
        ttype, tstring, start, end, line = tok
        if ttype == tokenize.COMMENT:
            new = _clean_text(tstring)
            # Drop the line entirely if comment now empty; replace with empty
            # comment (same prefix to keep position valid). Actually simplest:
            # if the cleaned content is just '#' or whitespace, replace with
            # '# ' so untokenize doesn't choke on a totally empty comment.
            stripped = new.strip()
            if stripped == "#" or stripped == "":
                # Replace with empty-content comment (whole line will be
                # collapsed via post-pass below)
                new = "# "
            out_tokens.append((ttype, new, start, end, line))
            continue
        if ttype == tokenize.STRING and start[0] in docstring_lines:
            # This is a docstring — clean inner content but preserve quote style.
            quote = None
            for q in ('"""', "'''", '"', "'"):
                if tstring.startswith(q) and tstring.endswith(q):
                    quote = q
                    break
            if quote:
                inner = tstring[len(quote):-len(quote)] if tstring.endswith(quote) else tstring[len(quote):]
                cleaned_inner = _clean_text(inner)
                new_string = quote + cleaned_inner + quote
                out_tokens.append((ttype, new_string, start, end, line))
                continue
        out_tokens.append(tok)

    try:
        result = tokenize.untokenize(out_tokens)
    except Exception:
        # Untokenize can be picky; fall back to original
        return src

    # Post-pass 1: drop fully-empty comment lines ("# " on its own) AND
    # individual comment lines that contain change-history phrases.
    line_kill_re = re.compile(
        r"\b(Without this|without this|previously|Previously|originally|"
        r"Originally|used to be|used to have|Earlier this|Earlier version|"
        r"This was originally|This used to|We used to|It used to|"
        r"was bumped|This commit|This fix|This PR)\b"
    )
    cleaned_lines = []
    for line in result.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.rstrip() in ("#", "# "):
            continue
        # Only drop COMMENT lines (lines that start with #), never code.
        if stripped.startswith("#") and line_kill_re.search(line):
            continue
        cleaned_lines.append(line)
    result = "".join(cleaned_lines)

    # Post-pass 2: drop multi-line comment BLOCKS that start with a
    # change-history kill marker. A "block" = consecutive lines whose
    # only non-whitespace prefix is "#". The block ends at the first
    # non-comment line (code or blank).
    out_lines: list[str] = []
    src_lines = result.splitlines(keepends=True)
    i = 0
    n = len(src_lines)

    def _is_comment_line(s: str) -> bool:
        s = s.lstrip()
        return s.startswith("#")

    def _comment_body(s: str) -> str:
        return re.sub(r"^\s*#\s*", "", s).rstrip("\n")

    while i < n:
        line = src_lines[i]
        if _is_comment_line(line):
            # Collect the comment block.
            j = i
            while j < n and _is_comment_line(src_lines[j]):
                j += 1
            block = src_lines[i:j]
            first_body = _comment_body(block[0]).strip()
            if any(first_body.startswith(p) for p in KILL_BLOCK_PREFIXES):
                # Drop the whole block.
                i = j
                continue
            out_lines.extend(block)
            i = j
            continue
        out_lines.append(line)
        i += 1
    result = "".join(out_lines)

    # Collapse 3+ blank lines to 2.
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result

def walk_sources() -> list[Path]:
    out: list[Path] = []
    for sub in SOURCE_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if any(part in EXCLUDE for part in p.parts):
                continue
            out.append(p)
    return out

def main() -> int:
    args = sys.argv[1:]
    if args == ["--all"]:
        targets = walk_sources()
    elif args == ["--dry-run"]:
        targets = walk_sources()
    else:
        targets = [Path(a) for a in args]

    n_changed = 0
    n_broken = 0
    for path in targets:
        path = path.resolve()
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:
            continue
        cleaned = clean_python_source(src)
        if cleaned == src:
            continue
        # Verify the cleaned source still parses.
        try:
            ast.parse(cleaned)
        except SyntaxError:
            print(f"  SKIP (cleanup broke syntax): {path}")
            n_broken += 1
            continue
        path.write_text(cleaned, encoding="utf-8")
        n_changed += 1
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = path
        print(f"  cleaned  {rel}")
    print(f"\nDONE: {n_changed} cleaned / {n_broken} would-have-broken-skipped / {len(targets)} scanned")
    return 0

if __name__ == "__main__":
    sys.exit(main())
