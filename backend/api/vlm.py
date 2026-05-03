"""
backend/api/vlm.py
──────────────────
L77 — image upload + Sonnet vision endpoint.

Single endpoint:
  POST /api/vlm/upload
    multipart/form-data:
      thread_id: str (required)
      file:      UploadFile (required, ≤ 5 MB, png/jpg/webp)

Returns the canonical VLM JSON (per vlm/extract.py) plus the route_decision
that the frontend uses to decide what to show next:

  * "lock_immediately"  — strong topic guess, frontend pre-fills the
                          first chat message with the description so the
                          v2 pre-lock topic flow locks deterministically
  * "show_top_matches"  — borderline confidence, frontend offers a retry
                          button + "Type topic instead" fallback
  * "refuse"            — no usable identification, frontend shows retry

Defense in depth: when cfg.domain.vlm.enabled is False, the endpoint
returns 403 with a clear message so a malicious client can't sneak an
image through to Sonnet vision in a domain that opted out (per L77 +
L78 domain gating).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/api/vlm", tags=["vlm"])


class IdentifiedStructure(BaseModel):
    name: str
    location: str = ""
    confidence: float = 0.0


class VlmUploadResponse(BaseModel):
    thread_id: str
    image_id: str
    image_path: str
    identified_structures: list[IdentifiedStructure]
    image_type: str
    description: str
    best_topic_guess: str
    confidence: float
    route_decision: str          # lock_immediately | show_top_matches | refuse
    elapsed_ms: int
    error: str = ""


def _route_decision(result: dict) -> str:
    """Map VLM confidence + structure count to a frontend hint per L77."""
    conf = float(result.get("confidence") or 0.0)
    structs = result.get("identified_structures") or []
    if not structs or conf < 0.5:
        return "refuse"
    if conf >= 0.7:
        return "lock_immediately"
    return "show_top_matches"


@router.post("/upload", response_model=VlmUploadResponse)
async def upload_image(
    thread_id: str = Form(...),
    file: UploadFile = File(...),
) -> VlmUploadResponse:
    """L77 image upload + Sonnet vision extraction."""
    from config import cfg as _cfg

    # Per L77 + L78 — domain gate. When disabled, refuse the upload with
    # a clear message; defense in depth even though the frontend hides
    # the upload button in those domains.
    vlm_cfg = getattr(_cfg.domain, "vlm", None)
    enabled = bool(getattr(vlm_cfg, "enabled", False)) if vlm_cfg else False
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail="Image upload not supported for this domain.",
        )

    tid = (thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="thread_id required")

    suffix = Path(file.filename or "upload.png").suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(status_code=400, detail=f"unsupported extension: {suffix}")

    # Persist under data/uploads/{thread_id}/{image_id}.{ext} per L77.
    image_id = uuid.uuid4().hex[:12]
    upload_dir = Path("data/uploads") / tid
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{image_id}{suffix}"

    try:
        contents = await file.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")

    # Reject oversize before writing to disk.
    from vlm.extract import MAX_IMAGE_BYTES
    if len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large: {len(contents)} bytes (cap {MAX_IMAGE_BYTES})",
        )
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="empty file")

    path.write_bytes(contents)

    # Sonnet vision call — uses the per-domain prompt template per L77/L78.
    from conversation.llm_client import make_anthropic_client, resolve_model
    from vlm.extract import extract_image_context

    client = make_anthropic_client()
    model = resolve_model(getattr(_cfg.models, "vlm", None) or _cfg.models.dean)
    prompt = (
        getattr(vlm_cfg, "prompt_template", "") or
        "Identify all anatomical structures visible in this image."
    )

    result = extract_image_context(
        path,
        client=client,
        model=model,
        domain_prompt=prompt,
    )

    decision = _route_decision(result)

    return VlmUploadResponse(
        thread_id=tid,
        image_id=image_id,
        image_path=str(path),
        identified_structures=[
            IdentifiedStructure(**s) for s in result.get("identified_structures", [])
        ],
        image_type=result.get("image_type", "other"),
        description=result.get("description", ""),
        best_topic_guess=result.get("best_topic_guess", ""),
        confidence=float(result.get("confidence", 0.0) or 0.0),
        route_decision=decision,
        elapsed_ms=int(result.get("_elapsed_ms", 0) or 0),
        error=str(result.get("_error", "") or ""),
    )
