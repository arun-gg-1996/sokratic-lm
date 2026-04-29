/**
 * MemoryDrawer
 * ============
 * Right-side drawer that lets the user inspect and clear what the tutor
 * remembers about them across sessions.
 *
 * Wires to:
 *   GET    /api/memory/{student_id}  → list entries
 *   DELETE /api/memory/{student_id}  → wipe per-student memory
 *
 * Why a drawer (not a modal):
 *   The list can be long (10+ entries after a few sessions) and the user
 *   may want to keep referring back to it while reading the chat. A
 *   drawer leaves the chat visible.
 *
 * Why "Forget everything" requires a confirm step:
 *   This action is irreversible (mem0 doesn't keep deleted entries).
 *   The button has a two-stage UI: first click stages "Confirm forget?",
 *   second click within 5s actually deletes. Click outside or wait → reset.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { forgetMemory, getMemory } from "../../api/client";
import type { MemoryEntry } from "../../types";

interface Props {
  studentId: string;
  open: boolean;
  onClose: () => void;
}

function formatDate(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

export function MemoryDrawer({ studentId, open, onClose }: Props) {
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [available, setAvailable] = useState<boolean>(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Two-stage confirm: null=idle, "armed"=waiting for second click
  const [forgetState, setForgetState] = useState<"idle" | "armed" | "deleting">(
    "idle"
  );
  const armTimer = useRef<number | null>(null);
  const drawerRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    if (!studentId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getMemory(studentId);
      setEntries(data.entries || []);
      setAvailable(data.available);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [studentId]);

  // Reload when opening
  useEffect(() => {
    if (open) {
      void load();
      setForgetState("idle");
    }
  }, [open, load]);

  // Esc to close
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Click-outside (the dimmed backdrop closes the drawer)
  const onBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
      onClose();
    }
  };

  const handleForget = async () => {
    if (forgetState === "idle") {
      // Stage 1: arm. Auto-disarm after 5s.
      setForgetState("armed");
      if (armTimer.current) window.clearTimeout(armTimer.current);
      armTimer.current = window.setTimeout(
        () => setForgetState("idle"),
        5000
      ) as unknown as number;
      return;
    }
    if (forgetState === "armed") {
      // Stage 2: actually delete
      if (armTimer.current) window.clearTimeout(armTimer.current);
      setForgetState("deleting");
      try {
        await forgetMemory(studentId);
        setEntries([]);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to delete");
      } finally {
        setForgetState("idle");
      }
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50"
      onClick={onBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-labelledby="memory-drawer-title"
    >
      <div
        ref={drawerRef}
        className="absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto bg-panel shadow-2xl border-l border-border flex flex-col"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 bg-panel border-b border-border px-4 py-3 flex items-center justify-between">
          <div>
            <h2 id="memory-drawer-title" className="text-base font-semibold">
              What I remember about you
            </h2>
            <div className="text-xs text-muted">
              Student: <span className="font-mono">{studentId}</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg border border-border px-2 py-1 text-sm hover:border-accent"
            aria-label="Close memory panel"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 px-4 py-4 space-y-3">
          {!available && (
            <div className="rounded-card border border-border bg-bg p-3 text-sm text-muted">
              Memory service is not available right now (mem0 / Qdrant offline).
              Sessions still work normally — they just won't reference past
              context.
            </div>
          )}

          {loading && (
            <div className="text-sm text-muted">Loading memories…</div>
          )}

          {error && (
            <div className="rounded-card border border-red-500/40 bg-red-500/10 p-3 text-sm">
              Error: {error}
            </div>
          )}

          {!loading && !error && entries.length === 0 && available && (
            <div className="rounded-card border border-border bg-bg p-4 text-sm text-muted">
              <div className="font-medium mb-1">No memories yet</div>
              <div>
                Your tutor will save session highlights here as you study.
                Things like topics covered, misconceptions worked through,
                and learning-style cues. Nothing is shared with other users.
              </div>
            </div>
          )}

          {!loading &&
            !error &&
            entries.map((m, idx) => (
              <div
                key={m.id ?? `mem-${idx}`}
                className="rounded-card border border-border bg-bg p-3 text-sm"
              >
                <div>{m.text}</div>
                {m.created_at && (
                  <div className="mt-1 text-xs text-muted">
                    {formatDate(m.created_at)}
                  </div>
                )}
              </div>
            ))}
        </div>

        {/* Footer actions */}
        <div className="sticky bottom-0 bg-panel border-t border-border px-4 py-3 space-y-2">
          <button
            onClick={() => void load()}
            disabled={loading}
            className="w-full rounded-lg border border-border px-3 py-2 text-sm hover:border-accent disabled:opacity-50"
          >
            Refresh
          </button>
          <button
            onClick={() => void handleForget()}
            disabled={
              forgetState === "deleting" || (entries.length === 0 && forgetState === "idle")
            }
            className={`w-full rounded-lg px-3 py-2 text-sm transition disabled:opacity-50 ${
              forgetState === "armed"
                ? "border border-red-500 bg-red-500/15 text-red-400 hover:bg-red-500/25"
                : "border border-border hover:border-accent"
            }`}
          >
            {forgetState === "idle" && "Forget everything about me"}
            {forgetState === "armed" && "Click again to confirm — this is permanent"}
            {forgetState === "deleting" && "Deleting…"}
          </button>
          <div className="text-xs text-muted">
            Memory is per-user. Forgetting only affects your own history.
          </div>
        </div>
      </div>
    </div>
  );
}
