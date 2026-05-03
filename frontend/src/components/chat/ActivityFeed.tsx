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

  // Live mode: render in the same shape as a tutor message bubble
  // (avatar + card) so it visually slots into the conversation rather
  // than feeling like a popup. Latest stage is the prominent "currently
  // doing X" line with a spinner; previous stages are checked-off
  // sub-items collapsed below.
  const latest = labels[labels.length - 1];
  const previous = labels.slice(0, -1);
  return (
    <div className="flex items-start gap-3 fade-in">
      <img
        src="/sokratic_bot_icon.png"
        alt="Sokratic Tutor"
        className="h-8 w-8 rounded-md mt-1 shrink-0 opacity-70"
      />
      <div className="rounded-card border border-border bg-panel px-4 py-3 flex-1 max-w-md">
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-3 w-3 rounded-full border-2 border-muted border-t-accent animate-spin shrink-0"
            aria-label="In progress"
          />
          <span className="text-sm text-text font-medium animate-pulse">
            {latest}
          </span>
        </div>
        {previous.length > 0 && (
          <ul className="mt-2 text-xs text-muted space-y-0.5 border-l-2 border-border pl-3">
            {previous.map((label, idx) => (
              <li
                key={`${idx}-${label}`}
                className="flex items-center gap-2"
              >
                <span className="text-accent shrink-0" aria-label="Done">✓</span>
                <span>{label}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
