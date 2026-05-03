"""
vlm/extract.py
──────────────
L77 — single Sonnet vision call on an uploaded image. Returns the
canonical VLM JSON the rest of the pipeline expects:

  {
    "identified_structures": [
      {"name": "...", "location": "...", "confidence": 0.0..1.0},
      ...
    ],
    "image_type": "x-ray" | "diagram" | "histology" | "gross_anatomy" | "model" | "other",
    "description": "<one paragraph>",
    "best_topic_guess": "<short text fed into L9 mapper>",
    "confidence": 0.0..1.0
  }

Always returns a dict — on any LLM/parse failure, returns a safe
fallback with confidence=0 + empty identified_structures so the caller
can show the L77 retry UI without crashing.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any


_PROMPT_TAIL = """\
Output STRICT JSON ONLY (no markdown fences, no prose) matching this
schema exactly:

{
  "identified_structures": [
    {"name": "<structure name>", "location": "<short positional phrase>", "confidence": 0.0}
  ],
  "image_type": "x-ray" | "diagram" | "histology" | "gross_anatomy" | "model" | "other",
  "description": "<one-paragraph summary including everything visible>",
  "best_topic_guess": "<short topic phrase a tutor could lock to>",
  "confidence": 0.0
}

If you cannot identify the image content (blurry, non-anatomical,
unrelated image), return:
  {"identified_structures": [], "image_type": "other",
   "description": "Could not identify anatomical content.",
   "best_topic_guess": "", "confidence": 0.0}
"""


_IMAGE_TYPES = {"x-ray", "diagram", "histology", "gross_anatomy", "model", "other"}

# Anthropic vision SDK accepts base64 image with one of these media types.
_MEDIA_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB cap per L77


def _media_type_for(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "image/png")


def _empty_result(reason: str) -> dict:
    return {
        "identified_structures": [],
        "image_type": "other",
        "description": f"VLM extraction failed: {reason}",
        "best_topic_guess": "",
        "confidence": 0.0,
        "_error": reason,
    }


def _parse_json_lenient(text: str) -> dict:
    """Strip markdown fences if any, then json.loads. Falls back to first
    JSON-object regex match. Raises ValueError on total failure."""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[-1].startswith("```"):
            s = "\n".join(lines[1:-1])
        else:
            s = "\n".join(lines[1:])
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise ValueError("no JSON object in response")
        return json.loads(m.group(0))


def _coerce(parsed: dict) -> dict:
    """Validate + normalize the parsed JSON into the canonical shape."""
    out: dict = {}
    raw_structs = parsed.get("identified_structures") or []
    structs = []
    if isinstance(raw_structs, list):
        for r in raw_structs[:30]:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name", "") or "").strip()
            if not name:
                continue
            try:
                conf = max(0.0, min(1.0, float(r.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                conf = 0.0
            structs.append({
                "name": name,
                "location": str(r.get("location", "") or "").strip(),
                "confidence": conf,
            })
    out["identified_structures"] = structs

    image_type = str(parsed.get("image_type", "") or "").strip().lower()
    out["image_type"] = image_type if image_type in _IMAGE_TYPES else "other"

    out["description"] = str(parsed.get("description", "") or "").strip()
    out["best_topic_guess"] = str(parsed.get("best_topic_guess", "") or "").strip()

    try:
        out["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    return out


def extract_image_context(
    image_path: Path,
    *,
    client: Any,
    model: str,
    domain_prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.0,
) -> dict:
    """One-shot Sonnet vision call. Returns the canonical VLM dict
    (always — never raises). Validates file size + extension.
    """
    p = Path(image_path)
    if not p.exists():
        return _empty_result(f"file not found: {p}")
    try:
        size = p.stat().st_size
    except OSError as e:
        return _empty_result(f"stat failed: {e}")
    if size > MAX_IMAGE_BYTES:
        return _empty_result(f"file too large: {size} bytes (cap {MAX_IMAGE_BYTES})")
    if p.suffix.lower() not in _MEDIA_TYPES:
        return _empty_result(f"unsupported extension: {p.suffix}")

    try:
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError as e:
        return _empty_result(f"read failed: {e}")

    instruction = (domain_prompt or "").strip() + "\n\n" + _PROMPT_TAIL

    t0 = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _media_type_for(p),
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": instruction},
                ],
            }],
        )
    except Exception as e:
        return _empty_result(f"{type(e).__name__}: {str(e)[:160]}")
    elapsed_ms = int((time.time() - t0) * 1000)

    raw_text = resp.content[0].text if resp.content else ""
    try:
        parsed = _parse_json_lenient(raw_text)
    except (ValueError, json.JSONDecodeError) as e:
        out = _empty_result(f"json parse: {e}")
        out["_raw"] = raw_text[:500]
        out["_elapsed_ms"] = elapsed_ms
        return out

    if not isinstance(parsed, dict):
        return _empty_result(f"non-object response: {type(parsed).__name__}")

    out = _coerce(parsed)
    out["_elapsed_ms"] = elapsed_ms
    usage = getattr(resp, "usage", None)
    out["_input_tokens"] = getattr(usage, "input_tokens", 0) or 0
    out["_output_tokens"] = getattr(usage, "output_tokens", 0) or 0
    return out
