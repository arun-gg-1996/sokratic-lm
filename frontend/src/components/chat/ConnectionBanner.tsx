/**
 * ConnectionBanner — L80.f WebSocket lifecycle indicator.
 *
 * Renders a slim banner at the top of the chat surface when the
 * WebSocket isn't healthy. Hidden during the normal "connected"
 * state so it doesn't add visual noise.
 *
 * State map (driven by useWebSocket → sessionStore.connection):
 *   "connecting"   — first connect; brief, usually under 500ms
 *   "connected"    — banner hidden
 *   "reconnecting" — onclose just fired; auto-reconnect in flight
 *                    with 1.2s backoff up to 25 attempts (~30s)
 *   "lost"         — burned through all reconnects; user must refresh
 *
 * No new dependencies — pure CSS via a small inline pulse on
 * "reconnecting" so the banner reads as live, not stuck.
 */
import { useSessionStore } from "../../stores/sessionStore";

export function ConnectionBanner() {
  const connection = useSessionStore((s) => s.connection);

  if (connection === "connected") return null;

  const config = {
    connecting: {
      text: "Connecting…",
      cls: "bg-muted/15 text-muted border-border",
    },
    reconnecting: {
      text: "Reconnecting…",
      cls: "bg-amber-500/15 text-amber-300 border-amber-500/30 animate-pulse",
    },
    lost: {
      text: "Connection lost — refresh the page to continue.",
      cls: "bg-red-500/15 text-red-300 border-red-500/40",
    },
  }[connection];

  return (
    <div
      className={`shrink-0 border-b ${config.cls} px-4 py-1.5 text-xs text-center font-medium transition-colors duration-200`}
      role="status"
      aria-live="polite"
    >
      {config.text}
    </div>
  );
}
