"""
scripts/publish_corpus.py
-------------------------
Publish expensive-to-regenerate artifacts (processed JSONL, BM25 index, topic
index, textbook structure, optionally a Qdrant snapshot) to a HuggingFace
dataset repo, so teammates can bootstrap from a single command.

Safe by default:
  - Skips copyrighted source PDFs (data/raw/*.pdf).
  - Skips ephemeral artifacts (data/artifacts/).
  - Idempotent: only uploads files whose sha256 differs from the remote manifest.

Usage:
  python scripts/publish_corpus.py --manifest-only           # plumbing test
  python scripts/publish_corpus.py --dry-run                 # preview what would be uploaded
  python scripts/publish_corpus.py --tag v0-messy-metadata   # real publish with a tag

Env vars (from .env):
  HF_TOKEN     — HuggingFace write token
  HF_USERNAME  — HF account name (default repo owner)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import HfApi, create_repo

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

DEFAULT_REPO_NAME = "sokratic-anatomy-corpus"

# Files that are expensive to regenerate and safe to publish.
# Paths are relative to repo root.
INCLUDE_FILES: list[str] = [
    "data/processed/propositions_ot.jsonl",
    "data/processed/chunks_ot.jsonl",
    "data/processed/raw_elements_ot.jsonl",
    "data/processed/raw_sections_ot.jsonl",
    "data/indexes/bm25_ot.pkl",
    "data/textbook_structure.json",
    "data/topic_index.json",
]

# Never upload these — either copyrighted, secret, or easy to regenerate.
EXCLUDE_PATTERNS: tuple[str, ...] = (
    "data/raw/",
    "data/artifacts/",
    ".env",
    ".git/",
    ".claude/",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _build_manifest(files: list[Path]) -> dict:
    entries = []
    for p in files:
        rel = str(p.relative_to(ROOT))
        entries.append({
            "path": rel,
            "size": p.stat().st_size,
            "sha256": _sha256(p),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "files": entries,
    }


def _fetch_remote_manifest(api: HfApi, repo_id: str) -> dict | None:
    try:
        path = api.hf_hub_download(
            repo_id=repo_id, repo_type="dataset", filename="MANIFEST.json",
        )
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _readme_body(repo_id: str, manifest: dict, tag: str | None) -> str:
    lines = [
        "---",
        "license: cc-by-4.0",
        "tags:",
        "- rag",
        "- tutoring",
        "- anatomy",
        "---",
        "",
        f"# {repo_id}",
        "",
        "Processed corpus + retrieval indexes for the Sokratic AI Tutor thesis project.",
        "Contents are regenerated artifacts from the OpenStax *Anatomy & Physiology 2e* textbook.",
        "The source PDF is **not** included (grab it from OpenStax).",
        "",
        "## Bootstrapping",
        "",
        "```bash",
        "python scripts/bootstrap_corpus.py",
        "```",
        "",
        "See `SETUP.md` in the code repo for the full flow.",
        "",
        f"- Generated: {manifest['generated_at']}",
        f"- Git commit: `{manifest['git_commit']}`",
    ]
    if tag:
        lines.append(f"- Tag: `{tag}`")
    lines.extend([
        "",
        "## Files",
        "",
        "| Path | Size | SHA-256 (first 12) |",
        "|------|------|---------------------|",
    ])
    for f in manifest["files"]:
        size_mb = f["size"] / (1024 * 1024)
        lines.append(
            f"| `{f['path']}` | {size_mb:.1f} MB | `{f['sha256'][:12]}` |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo", default=None,
        help=f"HF repo id (default: $HF_USERNAME/{DEFAULT_REPO_NAME})",
    )
    ap.add_argument(
        "--manifest-only", action="store_true",
        help="Only publish MANIFEST + README (plumbing test).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview only, no upload.")
    ap.add_argument("--force", action="store_true", help="Re-upload even if remote sha matches.")
    ap.add_argument("--tag", default=None, help="Optional revision tag to create.")
    ap.add_argument(
        "--private", action="store_true", help="Create repo as private (default: public).",
    )
    args = ap.parse_args()

    token = os.getenv("HF_TOKEN")
    user = os.getenv("HF_USERNAME")
    if not token:
        print("ERR: HF_TOKEN not set in .env", file=sys.stderr)
        return 2
    if not user and not args.repo:
        print("ERR: HF_USERNAME not set and --repo not passed", file=sys.stderr)
        return 2
    repo_id = args.repo or f"{user}/{DEFAULT_REPO_NAME}"

    # Collect files that exist locally.
    present: list[Path] = []
    missing: list[str] = []
    for rel in INCLUDE_FILES:
        p = ROOT / rel
        (present if p.exists() else missing).append(p if p.exists() else rel)

    if missing:
        print(f"[warn] skipping missing files: {missing}")

    # Build manifest: in --manifest-only mode record an empty file list so a
    # subsequent real publish doesn't see a "remote already has everything"
    # false positive and skip all uploads.
    manifest = _build_manifest([] if args.manifest_only else present)
    manifest["files_on_disk"] = [
        {"path": str(p.relative_to(ROOT)), "size": p.stat().st_size}
        for p in present
    ]

    print(f"Repo: {repo_id}  visibility: {'private' if args.private else 'public'}")
    print(f"Files to consider: {len(present)}  total size: "
          f"{sum(e['size'] for e in manifest['files_on_disk']) / (1024*1024):.1f} MB")

    api = HfApi(token=token)

    if not args.dry_run:
        # Idempotent: create_repo with exist_ok doesn't error if already there.
        create_repo(
            repo_id, repo_type="dataset", private=args.private,
            exist_ok=True, token=token,
        )

    # Decide which files to actually upload: those whose sha differs from remote.
    remote = _fetch_remote_manifest(api, repo_id) or {"files": []}
    remote_hashes = {f["path"]: f["sha256"] for f in remote.get("files", [])}

    to_upload: list[Path] = []
    if not args.manifest_only:
        # We need local shas even when the manifest is empty (manifest-only
        # skips building them for remote); compute on the fly.
        local_shas = {
            str(p.relative_to(ROOT)): _sha256(p) for p in present
        }
        # Re-build manifest now that we know we're uploading real files.
        manifest = _build_manifest(present)
        manifest["files_on_disk"] = [
            {"path": str(p.relative_to(ROOT)), "size": p.stat().st_size}
            for p in present
        ]
        for p in present:
            rel = str(p.relative_to(ROOT))
            if args.force or remote_hashes.get(rel) != local_shas[rel]:
                to_upload.append(p)

    print(f"Files needing upload: {len(to_upload)}")
    for p in to_upload:
        size_mb = p.stat().st_size / (1024 * 1024)
        print(f"  {p.relative_to(ROOT)}  ({size_mb:.1f} MB)")

    if args.dry_run:
        print("[dry-run] exiting without upload.")
        return 0

    # Always write MANIFEST.json + README.md (these are cheap).
    manifest_path = ROOT / "data" / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    readme_path = ROOT / "data" / "HF_README.md"
    readme_path.write_text(_readme_body(repo_id, manifest, args.tag))

    print("Uploading MANIFEST.json + README.md ...")
    api.upload_file(
        path_or_fileobj=manifest_path, path_in_repo="MANIFEST.json",
        repo_id=repo_id, repo_type="dataset", token=token,
    )
    api.upload_file(
        path_or_fileobj=readme_path, path_in_repo="README.md",
        repo_id=repo_id, repo_type="dataset", token=token,
    )

    for p in to_upload:
        rel = str(p.relative_to(ROOT))
        print(f"Uploading {rel} ...")
        api.upload_file(
            path_or_fileobj=p, path_in_repo=rel,
            repo_id=repo_id, repo_type="dataset", token=token,
        )

    if args.tag:
        print(f"Creating tag: {args.tag}")
        try:
            api.create_tag(
                repo_id=repo_id, repo_type="dataset", tag=args.tag, token=token,
            )
        except Exception as e:
            # Tag already exists — idempotent-friendly: don't fail the publish.
            print(f"  [tag] already exists or failed: {e}")

    print(f"Done. https://huggingface.co/datasets/{repo_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
