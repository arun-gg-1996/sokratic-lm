/**
 * ActivityFeed
 * ============
 * Renders a per-turn list of backend stage labels (Reading your
 * message, Searching textbook, Drafting response, etc.) in two modes:
 *
 *   live      — the current in-progress turn. Latest item shows an
 *               animated spinner; older items show ✓. Always
 *               expanded. Used by MessageList while isWaitingForTutor.
 *
 *   collapsed — the snapshot attached to a finalized tutor message.
 *               Renders as a small "Activity log (N steps) ▸" button
 *               that expands on click. All items show ✓ since the
 *               turn is complete. Used by MessageBubble.
 *
 * 2026-05-05: each entry can include an optional `detail` string
 * (hover tooltip via title=) — used during demos to explain WHY a
 * particular stage is happening (e.g. "Attempt 1 was rejected by
 * the leak verifier — Teacher is rewriting"). Backward-compatible
 * with plain string[] inputs (auto-converted to {label} entries).
 *
 * Visual language matches the rest of the app — same Tailwind tokens,
 * subtle border, no new design system. Spinner is a pure-CSS pulsing
 * dot (no SVG dependencies).
 */
import { useState } from "react";
import type { ActivityEntry } from "../../types";

interface Props {
  // Accept legacy string[] for backward compat with any caller that
  // hasn't been updated yet. Internally we normalize to ActivityEntry[].
  labels: ActivityEntry[] | string[];
  mode: "live" | "collapsed";
}

function StatusDot({ active }: { active: boolean }) {
  if (active) {
    return (
      <span
        className="inline-block h-3 w-3 rounded-full border-2 border-muted border-t-accent animate-spin"
        aria-label="In progress"
      />
    );
  }
  return (
    <span className="text-accent shrink-0" aria-label="Done">
      ✓
    </span>
  );
}

function normalize(input: ActivityEntry[] | string[]): ActivityEntry[] {
  if (input.length === 0) return [];
  if (typeof input[0] === "string") {
    return (input as string[]).map((label) => ({ label }));
  }
  return input as ActivityEntry[];
}

// Heuristic for highlighting verifier rejections / fallbacks visually.
// Backend prefixes such labels with ⚠ or "Falling back" so we can flag
// them without parsing structured data.
function isWarning(label: string): boolean {
  return label.startsWith("⚠") || label.toLowerCase().includes("falling back");
}

export function ActivityFeed({ labels, mode }: Props) {
  const entries = normalize(labels);
  const [open, setOpen] = useState(mode === "live");

  if (entries.length === 0) return null;

  if (mode === "collapsed") {
    return (
      <div className="mt-2">
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-xs text-muted hover:text-text transition flex items-center gap-1"
          aria-expanded={open}
        >
          <span>{open ? "▾" : "▸"}</span>
          <span>
            Activity log ({entries.length} step{entries.length === 1 ? "" : "s"})
          </span>
        </button>
        {open && (
          <ul className="mt-2 text-sm space-y-1 border-l-2 border-border pl-3">
            {entries.map((entry, idx) => (
              <li
                key={`${idx}-${entry.label}`}
                className={`flex items-center gap-2 ${
                  isWarning(entry.label) ? "text-amber-600 dark:text-amber-400" : "text-muted"
                }`}
                title={entry.detail || undefined}
              >
                <StatusDot active={false} />
                <span>{entry.label}</span>
                {entry.detail && (
                  <span className="text-[10px] text-muted/60 ml-1">ⓘ</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  // Live mode: latest entry is the prominent "currently doing X" line
  // with a spinner; previous entries are checked-off sub-items.
  const latest = entries[entries.length - 1];
  const previous = entries.slice(0, -1);
  const latestIsWarning = isWarning(latest.label);
  return (
    <div className="flex items-start gap-3 fade-in">
      <img
        src="/sokratic_bot_icon.png"
        alt="Sokratic Tutor"
        className="h-8 w-8 rounded-md mt-1 shrink-0 opacity-70"
      />
      <div className="rounded-card border border-border bg-panel px-4 py-3 flex-1 max-w-md">
        <div
          className="flex items-center gap-2"
          title={latest.detail || undefined}
        >
          <span
            className={`inline-block h-3 w-3 rounded-full border-2 border-muted ${
              latestIsWarning ? "border-t-amber-500" : "border-t-accent"
            } animate-spin shrink-0`}
            aria-label="In progress"
          />
          <span
            className={`text-sm font-medium animate-pulse ${
              latestIsWarning ? "text-amber-700 dark:text-amber-400" : "text-text"
            }`}
          >
            {latest.label}
          </span>
          {latest.detail && (
            <span className="text-[10px] text-muted/60 ml-1">ⓘ</span>
          )}
        </div>
        {previous.length > 0 && (
          <ul className="mt-2 text-xs text-muted space-y-0.5 border-l-2 border-border pl-3">
            {previous.map((entry, idx) => (
              <li
                key={`${idx}-${entry.label}`}
                className={`flex items-center gap-2 ${
                  isWarning(entry.label) ? "text-amber-600 dark:text-amber-400" : ""
                }`}
                title={entry.detail || undefined}
              >
                <span className="text-accent shrink-0" aria-label="Done">✓</span>
                <span>{entry.label}</span>
                {entry.detail && (
                  <span className="text-[10px] text-muted/60 ml-1">ⓘ</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
