"""
scripts/bootstrap_corpus.py
---------------------------
Teammate-facing: pulls processed corpus artifacts from a HuggingFace dataset
repo into the correct local paths. Skips files that already exist with the
matching sha256. Safe to re-run.

Usage:
  python scripts/bootstrap_corpus.py                       # default: pull to ./data
  python scripts/bootstrap_corpus.py --manifest-only       # just fetch the manifest (plumbing test)
  python scripts/bootstrap_corpus.py --target-dir /tmp/x   # pull into a scratch dir
  python scripts/bootstrap_corpus.py --force               # re-download even if local sha matches
  python scripts/bootstrap_corpus.py --version v0-messy-metadata   # pin to a tag

Env vars (from .env):
  HF_TOKEN     — optional (only needed for private repos)
  HF_USERNAME  — default repo owner
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import HfApi, hf_hub_download

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

DEFAULT_REPO_NAME = "sokratic-anatomy-corpus"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(repo_id: str, filename: str, revision: str | None, token: str | None) -> Path:
    return Path(hf_hub_download(
        repo_id=repo_id, filename=filename, repo_type="dataset",
        revision=revision, token=token,
    ))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo", default=None,
        help=f"HF repo id (default: $HF_USERNAME/{DEFAULT_REPO_NAME})",
    )
    ap.add_argument(
        "--target-dir", default=str(ROOT),
        help="Root directory for placing files (default: repo root).",
    )
    ap.add_argument(
        "--manifest-only", action="store_true",
        help="Only fetch MANIFEST.json (plumbing test).",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Re-download even if local sha256 matches.",
    )
    ap.add_argument(
        "--version", default=None,
        help="Pin to a specific tag/revision (default: latest on main).",
    )
    args = ap.parse_args()

    token = os.getenv("HF_TOKEN")  # optional for public repos
    user = os.getenv("HF_USERNAME")
    repo_id = args.repo or (f"{user}/{DEFAULT_REPO_NAME}" if user else None)
    if not repo_id:
        print("ERR: provide --repo or set HF_USERNAME in .env", file=sys.stderr)
        return 2

    target = Path(args.target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(f"Repo: {repo_id}  target: {target}  revision: {args.version or 'main'}")

    # Step 1: fetch manifest.
    manifest_src = _download(repo_id, "MANIFEST.json", args.version, token)
    manifest_dst = target / "data" / "MANIFEST.json"
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_src, manifest_dst)
    manifest = json.loads(manifest_dst.read_text())
    print(f"Manifest: {len(manifest.get('files', []))} files listed, "
          f"generated {manifest.get('generated_at', '?')}, "
          f"commit {manifest.get('git_commit', '?')[:8]}")

    if args.manifest_only:
        print(f"[manifest-only] wrote {manifest_dst}")
        return 0

    # Step 2: fetch each file, skipping when sha matches.
    fetched, skipped, failed = 0, 0, 0
    for entry in manifest.get("files", []):
        rel = entry["path"]
        expected_sha = entry["sha256"]
        dst = target / rel
        if dst.exists() and not args.force:
            try:
                if _sha256(dst) == expected_sha:
                    skipped += 1
                    continue
            except Exception:
                pass  # fall through and re-download
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            src = _download(repo_id, rel, args.version, token)
            shutil.copyfile(src, dst)
            fetched += 1
            print(f"  fetched {rel}")
        except Exception as e:
            failed += 1
            print(f"  FAILED {rel}: {e}")

    print(f"\nDone. fetched={fetched}  skipped={skipped}  failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
