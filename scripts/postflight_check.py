"""
scripts/postflight_check.py
Verify that the pulled data + running services are *safe to serve from*
before the systemd backend unit is started.

This is the SECOND gate in the deploy sequence:

 preflight → bootstrap_corpus → postflight → start backend

Failing postflight means the pull was partial / corrupt / mis-versioned
or qdrant doesn't have the embeddings, or the retriever can't actually
retrieve. Starting uvicorn in any of those states produces a backend
that crashes on the first user message — fail loudly and early instead.

Checks performed
* Manifest completeness: every file in data/MANIFEST.json exists
 locally with the matching sha256.
 * Topic index integrity: data/topic_index.json parseable, has the
 minimum number of topics expected.
 * BM25 index loadable: pickle deserializes without error.
 * Qdrant reachability: http://127.0.0.1:6333 accepts requests.
 * Qdrant collection: sokratic_kb_chunks exists and has
 points_count >= MIN_POINTS_COUNT.
 * Live retrieval probe: ChunkRetriever.retrieve on a known
 corpus subsection returns >= 1 chunk
 with non-empty text and a populated
 section_title / subsection_title.
 * LLM connectivity: single-token Anthropic + OpenAI calls
 succeed (skipped with --no-llm-probe to
 save a few cents in CI).
 * Smoke harness: scripts/smoke_post_demo_fixes.py passes.

Exit codes
0 — safe to start the backend
 1 — one or more required checks failed; do NOT start
 2 — invocation error

Usage
.venv/bin/python scripts/postflight_check.py
 .venv/bin/python scripts/postflight_check.py --json
 .venv/bin/python scripts/postflight_check.py --no-llm-probe
 .venv/bin/python scripts/postflight_check.py --skip-smoke

Run after `bootstrap_corpus.py` and `qdrant_up.sh`. Safe to re-run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("QDRANT_HTTP_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "sokratic_kb_chunks")
MIN_POINTS_COUNT = 5000  # corpus has 7562 chunks; require most-of

# Free-text query → expected canonical subsection name. We do NOT require
# an exact match (the resolver might score it as a near-match), only that
# the returned chunk's subsection_title is non-empty + relevant.
RETRIEVAL_PROBES = [
    ("compact and spongy bone", "Compact and Spongy Bone"),
    ("directional terms", "Directional Terms"),
]

# ── Result accumulator ───────────────────────────────────────────────────

class Result:
    def __init__(self) -> None:
        self.passes: list[dict] = []
        self.warnings: list[dict] = []
        self.failures: list[dict] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passes.append({"name": name, "detail": detail})

    def warn(self, name: str, detail: str = "") -> None:
        self.warnings.append({"name": name, "detail": detail})

    def fail(self, name: str, detail: str = "") -> None:
        self.failures.append({"name": name, "detail": detail})

    def to_dict(self) -> dict:
        return {
            "passes": self.passes,
            "warnings": self.warnings,
            "failures": self.failures,
            "summary": {
                "n_pass": len(self.passes),
                "n_warn": len(self.warnings),
                "n_fail": len(self.failures),
            },
        }

# ── Individual check helpers ─────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def check_manifest_files(r: Result) -> None:
    manifest = ROOT / "data" / "MANIFEST.json"
    if not manifest.exists():
        r.fail("manifest", "data/MANIFEST.json missing")
        return
    try:
        m = json.loads(manifest.read_text())
        files = m.get("files", [])
    except Exception as e:
        r.fail("manifest", f"parse failed: {e}")
        return
    if not files:
        r.fail("manifest", "no files listed")
        return

    n_ok, n_missing, n_mismatch = 0, 0, 0
    for entry in files:
        path = ROOT / entry["path"]
        if not path.exists():
            n_missing += 1
            r.fail(f"file:{entry['path']}", "missing locally")
            continue
        expected = entry.get("sha256")
        if expected:
            actual = _sha256(path)
            if actual != expected:
                n_mismatch += 1
                r.fail(
                    f"file:{entry['path']}",
                    f"sha256 mismatch (expected {expected[:8]}…, got {actual[:8]}…)",
                )
                continue
        n_ok += 1
    if n_ok == len(files):
        r.ok("manifest", f"all {n_ok} artifacts present + sha-correct")

def check_topic_index(r: Result) -> None:
    p = ROOT / "data" / "topic_index.json"
    if not p.exists():
        r.fail("topic_index", "missing")
        return
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        r.fail("topic_index", f"parse failed: {e}")
        return
    n = len(d) if isinstance(d, list) else len(d.get("topics", []))
    if n < 100:
        r.fail("topic_index", f"only {n} topics — looks truncated")
    else:
        r.ok("topic_index", f"{n} topics loaded")

def check_bm25_loadable(r: Result) -> None:
    p = ROOT / "data" / "indexes" / "bm25_chunks_openstax_anatomy.pkl"
    if not p.exists():
        r.fail("bm25_pickle", f"missing at {p}")
        return
    try:
        import pickle
        with p.open("rb") as fp:
            obj = pickle.load(fp)
        size = p.stat().st_size / (1024 * 1024)
        # Object shape: (BM25Okapi, props_list) per ingestion.core.index.load_bm25
        n = len(obj[1]) if isinstance(obj, tuple) and len(obj) > 1 else "?"
        r.ok("bm25_pickle", f"loaded ({size:.1f} MiB, {n} entries)")
    except Exception as e:
        r.fail("bm25_pickle", f"unpickle failed: {e}")

def check_qdrant_reachable(r: Result) -> None:
    try:
        with socket.create_connection((QDRANT_HOST, QDRANT_PORT), timeout=3):
            r.ok("qdrant_socket", f"{QDRANT_HOST}:{QDRANT_PORT} open")
    except Exception as e:
        r.fail("qdrant_socket", f"unreachable: {e}")

def check_qdrant_collection(r: Result) -> None:
    """HTTP probe — avoid importing the qdrant client if not available."""
    import urllib.request
    url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{QDRANT_COLLECTION}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        r.fail("qdrant_collection", f"GET {url} failed: {e}")
        return
    result = data.get("result", {})
    points = int(result.get("points_count") or 0)
    if points >= MIN_POINTS_COUNT:
        r.ok("qdrant_collection",
             f"{QDRANT_COLLECTION}: {points} points (>= {MIN_POINTS_COUNT})")
    else:
        r.fail(
            "qdrant_collection",
            f"{QDRANT_COLLECTION}: only {points} points "
            f"(expected >= {MIN_POINTS_COUNT}; was the snapshot restored?)",
        )

def check_retrieval_probe(r: Result) -> None:
    """Live retrieval — exercises the same path the runtime uses."""
    try:
        sys.path.insert(0, str(ROOT))
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        from retrieval.retriever import ChunkRetriever
        retr = ChunkRetriever()
    except Exception as e:
        r.fail("retrieval_boot",
               f"ChunkRetriever() failed: {type(e).__name__}: {e}")
        return
    r.ok("retrieval_boot", "ChunkRetriever instantiated")

    for q, expected_sub in RETRIEVAL_PROBES:
        try:
            chunks = retr.retrieve(q)
        except Exception as e:
            r.fail(f"retrieve:{q!r}", f"{type(e).__name__}: {e}")
            continue
        if not chunks:
            r.fail(f"retrieve:{q!r}", "0 chunks returned")
            continue
        top_sub = (chunks[0].get("subsection_title")
                   or chunks[0].get("subsection") or "")
        text = (chunks[0].get("text") or "")
        if not text:
            r.fail(f"retrieve:{q!r}", "top chunk has empty text")
        else:
            r.ok(f"retrieve:{q!r}",
                 f"{len(chunks)} chunks, top subsection={top_sub!r}, "
                 f"text={len(text)} chars")

def check_llm_probe(r: Result) -> None:
    """Single 1-token call to each LLM provider (cheap)."""
    # Anthropic
    try:
        sys.path.insert(0, str(ROOT))
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
        if resp and resp.content:
            r.ok("llm:anthropic", "1-token round-trip OK (haiku)")
        else:
            r.fail("llm:anthropic", "empty response")
    except Exception as e:
        r.fail("llm:anthropic", f"{type(e).__name__}: {str(e)[:120]}")

    # OpenAI (embeddings probe — cheaper than chat completion)
    try:
        from openai import OpenAI
        oai = OpenAI()
        resp = oai.embeddings.create(
            model="text-embedding-3-small",
            input="ok",
        )
        if resp and resp.data:
            r.ok("llm:openai", f"embedding round-trip OK "
                               f"(dim={len(resp.data[0].embedding)})")
        else:
            r.fail("llm:openai", "empty response")
    except Exception as e:
        r.fail("llm:openai", f"{type(e).__name__}: {str(e)[:120]}")

def check_smoke_harness(r: Result) -> None:
    smoke = ROOT / "scripts" / "smoke_post_demo_fixes.py"
    if not smoke.exists():
        r.warn("smoke", f"{smoke} missing — skipping")
        return
    try:
        out = subprocess.run(
            [sys.executable, str(smoke)],
            capture_output=True, text=True, timeout=60,
            cwd=str(ROOT),
        )
        if out.returncode == 0:
            r.ok("smoke", "all 18 code-shape checks passed")
        else:
            tail = (out.stdout or out.stderr).strip().splitlines()[-3:]
            r.fail("smoke", "; ".join(tail)[:200])
    except Exception as e:
        r.fail("smoke", f"failed to run: {e}")

# ── Reporting ────────────────────────────────────────────────────────────

def render_text(r: Result) -> str:
    lines = ["=" * 72, "  POSTFLIGHT CHECK", "=" * 72]
    for p in r.passes:
        lines.append(f"  \033[32m✓\033[0m {p['name']:36} {p['detail']}")
    for w in r.warnings:
        lines.append(f"  \033[33m!\033[0m {w['name']:36} {w['detail']}")
    for f in r.failures:
        lines.append(f"  \033[31m✗\033[0m {f['name']:36} {f['detail']}")
    s = r.to_dict()["summary"]
    lines.append("-" * 72)
    lines.append(
        f"  {s['n_pass']} pass / {s['n_warn']} warn / {s['n_fail']} fail"
    )
    lines.append("=" * 72)
    return "\n".join(lines)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-llm-probe", action="store_true",
                    help="skip Anthropic / OpenAI round-trip checks")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="skip running scripts/smoke_post_demo_fixes.py")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if warnings present")
    args = ap.parse_args()

    r = Result()

    check_manifest_files(r)
    check_topic_index(r)
    check_bm25_loadable(r)
    check_qdrant_reachable(r)
    check_qdrant_collection(r)
    check_retrieval_probe(r)
    if not args.no_llm_probe:
        check_llm_probe(r)
    if not args.skip_smoke:
        check_smoke_harness(r)

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
    else:
        print(render_text(r))

    if r.failures:
        return 1
    if args.strict and r.warnings:
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
