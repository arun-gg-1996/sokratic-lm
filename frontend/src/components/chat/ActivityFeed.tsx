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
 * Visual language matches the rest of the app — same Tailwind tokens,
 * subtle border, no new design system. Spinner is a pure-CSS pulsing
 * dot (no SVG dependencies).
 */
import { useState } from "react";

interface Props {
  labels: string[];
  mode: "live" | "collapsed";
}

function StatusDot({ active }: { active: boolean }) {
  // Active: small spinning circle (pure-CSS via animate-spin on a
  // ring border). Done: ✓ glyph in accent color.
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

export function ActivityFeed({ labels, mode }: Props) {
  // Collapsed mode: closed by default; user clicks to open.
  const [open, setOpen] = useState(mode === "live");

  if (!labels || labels.length === 0) return null;

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
            Activity log ({labels.length} step{labels.length === 1 ? "" : "s"})
          </span>
        </button>
        {open && (
          <ul className="mt-2 text-sm space-y-1 border-l-2 border-border pl-3">
            {labels.map((label, idx) => (
              <li
                key={`${idx}-${label}`}
                className="flex items-center gap-2 text-muted"
              >
                <StatusDot active={false} />
                <span>{label}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  // Live mode: latest item gets the spinner, older items get ✓.
  return (
    <div className="rounded-card border border-border bg-panel px-4 py-3 max-w-md">
      <div className="text-xs text-muted uppercase tracking-wide mb-2">
        Working on it
      </div>
      <ul className="text-sm space-y-1">
        {labels.map((label, idx) => {
          const isLatest = idx === labels.length - 1;
          return (
            <li
              key={`${idx}-${label}`}
              className={`flex items-center gap-2 ${
                isLatest ? "text-text" : "text-muted"
              }`}
            >
              <StatusDot active={isLatest} />
              <span>{label}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
