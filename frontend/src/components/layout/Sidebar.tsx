import { Link, useNavigate } from "react-router-dom";
import { useSessionStore } from "../../stores/sessionStore";
import { useUserStore } from "../../stores/userStore";
import { AccountPopover } from "../account/AccountPopover";

export function Sidebar() {
  const navigate = useNavigate();
  const studentId = useUserStore((s) => s.studentId);
  const resetSession = useSessionStore((s) => s.reset);
  const debug = useSessionStore((s) => s.debug);
  const turnCount =
    typeof (debug as Record<string, unknown> | null)?.turn_count === "number"
      ? ((debug as Record<string, unknown>).turn_count as number)
      : 0;
  const hintLevel =
    typeof (debug as Record<string, unknown> | null)?.hint_level === "number"
      ? ((debug as Record<string, unknown>).hint_level as number)
      : 0;
  const maxTurns =
    typeof (debug as Record<string, unknown> | null)?.max_turns === "number"
      ? ((debug as Record<string, unknown>).max_turns as number)
      : 25;
  const maxHints =
    typeof (debug as Record<string, unknown> | null)?.max_hints === "number"
      ? ((debug as Record<string, unknown>).max_hints as number)
      : 3;
  const displayHint = Math.min(hintLevel, maxHints);
  const hintsExhausted = hintLevel > maxHints;
  const topicConfirmed = Boolean((debug as Record<string, unknown> | null)?.topic_confirmed);
  const topicSelection =
    typeof (debug as Record<string, unknown> | null)?.topic_selection === "string"
      ? String((debug as Record<string, unknown>).topic_selection)
      : "";

  const startNew = () => {
    resetSession();
    navigate("/chat");
  };

  return (
    <aside className="w-sidebar shrink-0 border-r border-border bg-panel h-screen overflow-y-auto">
      <div className="h-full flex flex-col p-4 gap-4">
        <div className="flex items-center gap-3 px-2 pt-1">
          <img src="/sokratic_bot_icon.png" alt="Sokratic" className="h-8 w-8 rounded-md" />
          <div className="text-2xl font-semibold">Sokratic</div>
        </div>

        <button
          onClick={startNew}
          className="w-full rounded-card border border-border bg-bg px-4 py-2 text-left hover:border-accent transition"
        >
          + New chat
        </button>

        <nav className="flex flex-col gap-1 text-sm">
          <Link className="rounded-lg px-3 py-2 hover:bg-bg transition" to="/overview">
            Session overview
          </Link>
          <Link className="rounded-lg px-3 py-2 hover:bg-bg transition" to="/chat">
            Chats
          </Link>
        </nav>

        <div className="rounded-card border border-border bg-bg px-3 py-3 space-y-1 text-sm">
          <div className="text-muted">Turn {turnCount}/{maxTurns}</div>
          <div className="text-muted">
            {hintsExhausted ? "Hints exhausted" : `Hints ${displayHint}/${maxHints}`}
          </div>
          {topicConfirmed && topicSelection && (
            <div className="text-muted truncate" title={topicSelection}>
              Topic: {topicSelection}
            </div>
          )}
        </div>

        <div className="flex-1" />

        <AccountPopover studentId={studentId} />
      </div>
    </aside>
  );
}
