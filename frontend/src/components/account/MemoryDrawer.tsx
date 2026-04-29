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

// Group memory entries into sessions keyed by (session_date, topic_path).
// mem0 stores each session's writes as multiple atomized facts; this
// surfaces the natural unit (one card per actual tutoring session)
// instead of the storage-layer atom-by-atom flat list.
//
// Pre-metadata legacy entries (no session_date) all bucket under one
// "Older memories" group so they don't multiply.
//
// Categories within a session render in a stable order:
//   session_summary → topics_covered → misconception →
//   open_thread → learning_style_cue → (anything else)
const CATEGORY_ORDER: Record<string, number> = {
  session_summary: 0,
  topics_covered: 1,
  misconception: 2,
  open_thread: 3,
  learning_style_cue: 4,
};

interface SessionGroup {
  key: string;             // unique key for React
  date: string;            // ISO date or "" for legacy
  subsection: string;      // human-readable subsection title
  chapterNum: number;      // 0 if absent
  outcome: string;         // "reached" | "not_reached" | ""
  entries: MemoryEntry[];  // sorted by category order
}

function groupBySession(entries: MemoryEntry[]): SessionGroup[] {
  const buckets = new Map<string, SessionGroup>();
  for (const e of entries) {
    const meta = (e.metadata as Record<string, unknown>) || {};
    const date = String(meta.session_date || "");
    const path = String(meta.topic_path || "");
    const key = `${date}::${path}`;
    let g = buckets.get(key);
    if (!g) {
      g = {
        key,
        date,
        subsection: "",
        chapterNum: 0,
        outcome: "",
        entries: [],
      };
      buckets.set(key, g);
    }
    g.entries.push(e);
    // Hydrate group-level fields from the FIRST entry that has them
    // (some atoms drop subsection_title even when siblings have it).
    if (!g.subsection && meta.subsection_title) {
      g.subsection = String(meta.subsection_title);
    }
    if (!g.chapterNum && typeof meta.chapter_num === "number") {
      g.chapterNum = meta.chapter_num as number;
    }
    if (!g.outcome && meta.outcome) g.outcome = String(meta.outcome);
  }
  // Sort entries within each group by category, then by created_at desc.
  for (const g of buckets.values()) {
    g.entries.sort((a, b) => {
      const ca = String((a.metadata as Record<string, unknown>)?.category || "");
      const cb = String((b.metadata as Record<string, unknown>)?.category || "");
      const oa = CATEGORY_ORDER[ca] ?? 99;
      const ob = CATEGORY_ORDER[cb] ?? 99;
      if (oa !== ob) return oa - ob;
      return (b.created_at || "").localeCompare(a.created_at || "");
    });
  }
  // Newest sessions first; legacy "" date bucket goes to the bottom.
  return Array.from(buckets.values()).sort((a, b) => {
    if (a.date && !b.date) return -1;
    if (!a.date && b.date) return 1;
    return b.date.localeCompare(a.date);
  });
}

function categoryLabel(cat: string): string {
  switch (cat) {
    case "session_summary": return "Summary";
    case "topics_covered": return "Topic";
    case "misconception": return "Misconception";
    case "open_thread": return "Open thread";
    case "learning_style_cue": return "Learning style";
    default: return cat || "Note";
  }
}

function SessionCard({
  group,
  defaultOpen,
}: {
  group: SessionGroup;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const title =
    group.subsection ||
    (group.entries.length === 1 && group.entries[0].text) ||
    "Older memories";
  const subtitle = [
    group.date,
    group.chapterNum ? `Ch${group.chapterNum}` : "",
    group.outcome === "reached"
      ? "Reached target"
      : group.outcome === "not_reached"
        ? "Did not reach"
        : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="rounded-card border border-border bg-bg">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full px-4 py-3 flex items-start gap-3 text-left hover:bg-panel transition"
        aria-expanded={open}
      >
        <span className="text-muted text-sm w-4 shrink-0 mt-0.5">
          {open ? "▾" : "▸"}
        </span>
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate">{title}</div>
          {subtitle && (
            <div className="text-xs text-muted mt-0.5">{subtitle}</div>
          )}
        </div>
        <span className="text-xs text-muted shrink-0">
          {group.entries.length} {group.entries.length === 1 ? "atom" : "atoms"}
        </span>
      </button>
      {open && (
        <div className="border-t border-border divide-y divide-border">
          {group.entries.map((e, idx) => {
            const cat = String(
              (e.metadata as Record<string, unknown>)?.category || ""
            );
            return (
              <div key={e.id ?? `e-${idx}`} className="px-4 py-3 text-sm">
                <div className="text-xs text-muted uppercase tracking-wide mb-1">
                  {categoryLabel(cat)}
                </div>
                <div>{e.text}</div>
                {e.created_at && (
                  <div className="mt-1 text-xs text-muted">
                    {formatDate(e.created_at)}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
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
            entries.length > 0 &&
            groupBySession(entries).map((g, idx) => (
              <SessionCard key={g.key} group={g} defaultOpen={idx === 0} />
            ))}

          {/* Hidden flat-list fallback (kept for future debug, never
              rendered now; the grouped view above replaces it). */}
          {false &&
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
