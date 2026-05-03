/**
 * ImageUploadCard — L77 image-driven session entry.
 *
 * Renders only on the rapport phase (no thread_id yet OR fresh chat
 * with zero student messages). The user picks an image; we POST it to
 * /api/vlm/upload, get back the canonical VLM JSON, stash it in
 * localStorage under the IMAGE_CONTEXT_KEY useSession reads, then
 * trigger a session reset so the bootstrap fires with the VLM JSON
 * attached. The backend seeds the image description as the first
 * student message so the v2 topic mapper resolves to the right TOC
 * subsection automatically.
 *
 * Hidden when cfg.domain.vlm.enabled is false (for now this is a UX
 * decision — we surface the affordance always and let the backend
 * 403 if disabled. A follow-up can read /api/health for the active
 * domain config and hide proactively).
 */
import { useRef, useState } from "react";
import { uploadVlmImage } from "../../api/client";
import { useSessionStore } from "../../stores/sessionStore";

const IMAGE_CONTEXT_KEY = "sokratic_pending_image_context";
const ACCEPT = ".png,.jpg,.jpeg,.webp";

export function ImageUploadCard() {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reset = useSessionStore((s) => s.reset);

  // We use a placeholder thread_id for the VLM call — the backend uses
  // it only to namespace the upload directory. Once the VLM call
  // returns, we stash the JSON in localStorage and reset the session;
  // the bootstrap then fires startSession() which mints the real
  // thread_id and attaches the VLM JSON via image_context.
  const upload = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const placeholder = `pending_${Math.random().toString(36).slice(2, 10)}`;
      const result = await uploadVlmImage(placeholder, file);
      if (result.route_decision === "refuse" || result.confidence < 0.3) {
        setError(
          "I couldn't recognize that image. Try a different anatomical " +
          "image (a labeled diagram works best) or skip and type your topic.",
        );
        setUploading(false);
        return;
      }
      try {
        localStorage.setItem(IMAGE_CONTEXT_KEY, JSON.stringify(result));
      } catch {
        // localStorage unavailable — fall back to noop; user can retry
      }
      // Reset triggers the useSession bootstrap, which reads the pending
      // image context and routes through /api/session/start with it.
      reset();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
      setUploading(false);
    }
  };

  const onPick = () => {
    if (uploading) return;
    inputRef.current?.click();
  };

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) void upload(f);
  };

  return (
    <div className="rounded-card border border-dashed border-border bg-panel/60 px-4 py-4 space-y-2">
      <div className="text-sm font-medium">📷 Start with an image</div>
      <div className="text-xs text-muted">
        Upload an anatomical diagram or photo (PNG / JPG / WebP, ≤ 5 MB).
        The tutor will identify what's visible and lock to the right
        subsection automatically.
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        onChange={onChange}
        className="hidden"
      />
      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={onPick}
          disabled={uploading}
          className={`rounded-lg border px-3 py-1.5 text-sm transition ${
            uploading
              ? "border-muted/40 text-muted/60 cursor-not-allowed"
              : "border-border hover:border-accent"
          }`}
        >
          {uploading ? "Analyzing image…" : "Choose image"}
        </button>
        {uploading && (
          <span
            className="inline-block h-3 w-3 rounded-full border-2 border-muted border-t-accent animate-spin"
            aria-label="Uploading"
          />
        )}
      </div>
      {error && (
        <div className="text-xs text-red-400" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
