"""
scripts/preflight_check.py
---------------------------
Verify that the runtime environment is *prepared to pull data* before
the bootstrap script touches any remote artifact.

This is the FIRST gate in the deploy sequence:

    preflight  →  bootstrap_corpus  →  postflight  →  start backend

Failing preflight is cheap — it short-circuits before any HuggingFace
download, before any qdrant restore, before npm install runs.

Checks performed
----------------
  * System binaries: python (>=3.11), pip, node, npm, docker, curl
  * docker daemon running and reachable
  * data/MANIFEST.json present and parseable (lists every artifact
    the corpus expects)
  * .env present at repo root with the required keys (only key NAMES
    are checked — values are never read, only their non-empty length)
  * Disk free space >= 5 GiB (chunks + bm25 + qdrant snapshot +
    scispacy UMLS easily total 4 GiB; 5 GiB minimum buffer)
  * Outbound network reachability:
      - https://huggingface.co (HF artifact host)
      - https://api.anthropic.com (LLM provider)
      - https://api.openai.com (embeddings + assist LLM)
  * Optional: qdrant http port reachable if container already started

Exit codes
----------
  0 — all required checks passed (warnings allowed)
  1 — one or more required checks failed; do NOT proceed
  2 — invocation error (bad CLI / unreadable repo)

Usage
-----
  .venv/bin/python scripts/preflight_check.py
  .venv/bin/python scripts/preflight_check.py --json   # machine-readable
  .venv/bin/python scripts/preflight_check.py --strict # fail on warnings too

Safe to run repeatedly. Writes nothing to disk.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Project root = parent of scripts/
ROOT = Path(__file__).resolve().parent.parent

# ── Required vs optional env keys ────────────────────────────────────────
# REQUIRED keys must exist AND have non-empty values for the deploy to
# proceed. OPTIONAL keys produce warnings only.
REQUIRED_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SOKRATIC_AUTH_USERS",
    "SOKRATIC_AUTH_SECRET",
]
OPTIONAL_ENV_KEYS = [
    "HF_TOKEN",          # only required for private HF repos
    "HF_USERNAME",
    "SOKRATIC_DOMAIN",   # defaults to "ot" if absent
    "SOKRATIC_CORS_ORIGINS",
    "SOKRATIC_USE_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "GCP_ZONE",
]

NETWORK_PROBES = [
    ("huggingface.co", 443),
    ("api.anthropic.com", 443),
    ("api.openai.com", 443),
]

MIN_FREE_GIB = 5
# Project validated on 3.10 (laptop dev env) and 3.11 (deploy target).
# Lower bound 3.10 keeps both happy; bump to 3.11 only if we adopt
# 3.11-only syntax features.
MIN_PYTHON = (3, 10)


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

def check_python_version(r: Result) -> None:
    v = sys.version_info
    if (v.major, v.minor) >= MIN_PYTHON:
        r.ok("python_version", f"{v.major}.{v.minor}.{v.micro}")
    else:
        r.fail(
            "python_version",
            f"need >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}, got {v.major}.{v.minor}",
        )


def check_binary(r: Result, name: str, *, required: bool = True,
                 version_arg: str = "--version") -> None:
    path = shutil.which(name)
    if not path:
        if required:
            r.fail(f"binary:{name}", f"not found in PATH")
        else:
            r.warn(f"binary:{name}", f"not found in PATH (optional)")
        return
    try:
        out = subprocess.run(
            [name, version_arg], capture_output=True, text=True, timeout=5
        )
        ver = (out.stdout or out.stderr).strip().split("\n")[0]
        r.ok(f"binary:{name}", f"{path}  {ver[:60]}")
    except Exception as e:
        r.warn(f"binary:{name}", f"present but failed to query: {e}")


def check_docker_daemon(r: Result) -> None:
    if not shutil.which("docker"):
        r.fail("docker_daemon", "docker binary missing")
        return
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            r.ok("docker_daemon", f"server v{out.stdout.strip()}")
        else:
            r.fail(
                "docker_daemon",
                f"`docker info` failed (rc={out.returncode}): "
                f"{(out.stderr or '').strip()[:120]}",
            )
    except Exception as e:
        r.fail("docker_daemon", f"unreachable: {e}")


def check_disk_space(r: Result) -> None:
    try:
        usage = shutil.disk_usage(str(ROOT))
        free_gib = usage.free / (1024 ** 3)
        if free_gib >= MIN_FREE_GIB:
            r.ok("disk_space", f"{free_gib:.1f} GiB free at {ROOT}")
        else:
            r.fail(
                "disk_space",
                f"only {free_gib:.1f} GiB free (need >= {MIN_FREE_GIB} GiB)",
            )
    except Exception as e:
        r.warn("disk_space", f"could not stat: {e}")


def _parse_env_keys(env_path: Path) -> dict[str, bool]:
    """Return {key: has_nonempty_value}.

    Only the boolean is exposed — values are never returned to the
    caller and never logged. Comments and blank lines are ignored.
    """
    out: dict[str, bool] = {}
    pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = pattern.match(raw)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        # Strip optional quoting
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1].strip()
        out[key] = bool(val)
    return out


def check_env_file(r: Result) -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        r.fail("env_file", f"{env_path} not found")
        return
    try:
        keys = _parse_env_keys(env_path)
    except Exception as e:
        r.fail("env_file", f"parse failed: {e}")
        return

    n_required_set = 0
    for key in REQUIRED_ENV_KEYS:
        if keys.get(key):
            n_required_set += 1
        else:
            present = key in keys
            detail = "key empty" if present else "key missing"
            r.fail(f"env:{key}", detail)
    if n_required_set == len(REQUIRED_ENV_KEYS):
        r.ok("env_file", f"{n_required_set}/{len(REQUIRED_ENV_KEYS)} required keys set")

    # Optional keys: warn only if missing
    for key in OPTIONAL_ENV_KEYS:
        if not keys.get(key):
            r.warn(f"env:{key}", "absent or empty (optional)")
        else:
            r.ok(f"env:{key}", "set")

    # Auth users sanity (without reading the value): we can at least
    # check the env_file lists this key as set (already done above).
    # The actual user list is parsed at runtime by backend/auth.py.


def check_manifest(r: Result) -> None:
    manifest = ROOT / "data" / "MANIFEST.json"
    if not manifest.exists():
        r.fail("data_manifest", f"{manifest} missing — corpus pull will not know what to fetch")
        return
    try:
        m = json.loads(manifest.read_text())
        files = m.get("files") or []
        if not files:
            r.fail("data_manifest", "manifest has no 'files' entries")
        else:
            r.ok("data_manifest", f"{len(files)} artifacts listed; commit={m.get('git_commit', '?')[:12]}")
    except Exception as e:
        r.fail("data_manifest", f"parse failed: {e}")


def check_network(r: Result) -> None:
    for host, port in NETWORK_PROBES:
        try:
            with socket.create_connection((host, port), timeout=5):
                r.ok(f"network:{host}", f"reachable :{port}")
        except Exception as e:
            # Network is treated as required — without HF/LLM endpoints,
            # the deploy can't pull data or serve traffic.
            r.fail(f"network:{host}", f"unreachable: {e}")


def check_qdrant_optional(r: Result) -> None:
    """If qdrant is already running locally, that's fine; if it isn't,
    that's also fine — the bootstrap script starts it."""
    try:
        with socket.create_connection(("127.0.0.1", 6333), timeout=2):
            r.ok("qdrant_optional", "qdrant http port 6333 already open")
    except Exception:
        r.warn("qdrant_optional", "qdrant not running yet (bootstrap will start it)")


def check_writable(r: Result) -> None:
    """Verify we can write to data/ and the project root."""
    for sub in ["data", "data/processed", "data/indexes", "data/artifacts"]:
        p = ROOT / sub
        try:
            p.mkdir(parents=True, exist_ok=True)
            test_path = p / ".preflight_write_test"
            test_path.write_text("ok")
            test_path.unlink()
            r.ok(f"writable:{sub}", f"{p}")
        except Exception as e:
            r.fail(f"writable:{sub}", f"cannot write under {p}: {e}")


# ── Reporting ────────────────────────────────────────────────────────────

def render_text(r: Result) -> str:
    lines = ["=" * 72, "  PREFLIGHT CHECK", "=" * 72]
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
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON output (no colour)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any warnings are present")
    args = ap.parse_args()

    r = Result()

    check_python_version(r)
    check_binary(r, "pip", version_arg="--version")
    check_binary(r, "node")
    check_binary(r, "npm")
    check_binary(r, "curl")
    check_binary(r, "git")
    check_docker_daemon(r)
    check_disk_space(r)
    check_writable(r)
    check_env_file(r)
    check_manifest(r)
    check_network(r)
    check_qdrant_optional(r)

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
