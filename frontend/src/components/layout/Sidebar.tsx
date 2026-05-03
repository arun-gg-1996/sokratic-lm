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
  const prelockLoopCount =
    typeof (debug as Record<string, unknown> | null)?.prelock_loop_count === "number"
      ? ((debug as Record<string, unknown>).prelock_loop_count as number)
      : 0;
  const topicSelection =
    typeof (debug as Record<string, unknown> | null)?.topic_selection === "string"
      ? String((debug as Record<string, unknown>).topic_selection)
      : "";

  // Pull chapter / section / subsection from locked_topic so the UI can show
  // the full path in a collapsible (the previous one-line truncated display
  // was hard to read on long topic names). Falls back to topic_selection
  // when locked_topic isn't populated yet.
  const lockedTopic = (debug as Record<string, unknown> | null)?.locked_topic;
  const lockedTopicObj = typeof lockedTopic === "object" && lockedTopic !== null
    ? (lockedTopic as Record<string, unknown>)
    : null;
  const chapterName = typeof lockedTopicObj?.chapter === "string"
    ? String(lockedTopicObj.chapter) : "";
  const sectionName = typeof lockedTopicObj?.section === "string"
    ? String(lockedTopicObj.section) : "";
  const subsectionName = typeof lockedTopicObj?.subsection === "string"
    ? String(lockedTopicObj.subsection) : topicSelection;
  const lockedQuestionText = typeof (debug as Record<string, unknown> | null)?.locked_question === "string"
    ? String((debug as Record<string, unknown>).locked_question) : "";

  // Change 4 / 5.1 (2026-04-30): conversation-health counters surfaced
  // for debug. Goes amber at strike >= threshold-2, red at threshold-1.
  // Tooltip explains the threshold action (advance hint vs terminate).
  const dbg = (debug as Record<string, unknown> | null) ?? null;
  const helpAbuseCount = typeof dbg?.help_abuse_count === "number" ? (dbg.help_abuse_count as number) : 0;
  const helpAbuseThreshold = typeof dbg?.help_abuse_threshold === "number" ? (dbg.help_abuse_threshold as number) : 4;
  const offTopicCount = typeof dbg?.off_topic_count === "number" ? (dbg.off_topic_count as number) : 0;
  const offTopicThreshold = typeof dbg?.off_topic_threshold === "number" ? (dbg.off_topic_threshold as number) : 4;
  const totalLowEffort = typeof dbg?.total_low_effort_turns === "number" ? (dbg.total_low_effort_turns as number) : 0;
  const totalOffTopic = typeof dbg?.total_off_topic_turns === "number" ? (dbg.total_off_topic_turns as number) : 0;
  const showStrikePills = topicConfirmed && (
    helpAbuseCount > 0 || offTopicCount > 0 || totalLowEffort > 0 || totalOffTopic > 0
  );

  const strikeColor = (count: number, threshold: number): string => {
    if (count >= threshold) return "text-red-500";
    if (count >= threshold - 1) return "text-amber-400";
    if (count >= threshold - 2 && threshold >= 3) return "text-amber-300";
    return "text-muted";
  };

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
          {/*
            "Session overview" was a legacy weak/strong-topic page
            built before /mastery existed — it reads the stub
            weak_topics state field which is never populated since
            we replaced it with the per-concept MasteryStore. The
            route is left registered in App.tsx in case we want to
            repurpose the file later (e.g. as a chats history
            list), but hidden from nav so users don't land on the
            "Unknown topic" placeholder cards.
          */}
          <Link className="rounded-lg px-3 py-2 hover:bg-bg transition" to="/chat">
            Chats
          </Link>
          <Link className="rounded-lg px-3 py-2 hover:bg-bg transition" to="/mastery">
            My mastery
          </Link>
        </nav>

        <div className="rounded-card border border-border bg-bg px-3 py-3 space-y-1 text-sm">
          <div className="text-muted">
            {topicConfirmed ? `Turn ${turnCount}/${maxTurns}` : `Pre-lock ${prelockLoopCount}/7`}
          </div>
          <div className="text-muted">
            {hintsExhausted ? "Hints exhausted" : `Hints ${displayHint}/${maxHints}`}
          </div>
          {topicConfirmed && (subsectionName || topicSelection) && (
            <details className="text-muted text-xs group">
              <summary className="cursor-pointer list-none flex items-center gap-1 hover:text-fg transition">
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                  className="w-3 h-3 group-open:rotate-90 transition"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                </svg>
                <span className="truncate" title={subsectionName || topicSelection}>
                  Topic: {subsectionName || topicSelection}
                </span>
              </summary>
              <div className="pl-4 pt-1 space-y-0.5 text-muted/80">
                {chapterName && <div><span className="text-muted/60">Chapter:</span> {chapterName}</div>}
                {sectionName && <div><span className="text-muted/60">Section:</span> {sectionName}</div>}
                {subsectionName && <div><span className="text-muted/60">Subsection:</span> {subsectionName}</div>}
                {lockedQuestionText && (
                  <div className="pt-1">
                    <span className="text-muted/60">Question:</span>
                    <div className="italic">{lockedQuestionText}</div>
                  </div>
                )}
              </div>
            </details>
          )}

          {/* Change 4 / 5.1 (2026-04-30): conversation-health debug pills.
              Hidden until the student has actually accumulated some
              strikes; counters reset on engaged turns so this section
              comes and goes naturally. The total_* counters never reset
              and feed the mastery scorer. */}
          {showStrikePills && (
            <div className="pt-2 mt-2 border-t border-border space-y-1">
              <div className="text-xs text-muted/70">Conversation health</div>
              <div
                className={`text-xs ${strikeColor(helpAbuseCount, helpAbuseThreshold)}`}
                title={`Consecutive low-effort turns. At ${helpAbuseThreshold} the dean advances the hint level (with narrated transition). Counter resets on any genuine attempt.`}
              >
                Help-abuse: {helpAbuseCount}/{helpAbuseThreshold}
              </div>
              <div
                className={`text-xs ${strikeColor(offTopicCount, offTopicThreshold)}`}
                title={`Consecutive off-DOMAIN turns (NOT counting in-domain tangents). At ${offTopicThreshold} the session terminates with a polite farewell. Counter resets on any engaged turn.`}
              >
                Off-topic: {offTopicCount}/{offTopicThreshold}
              </div>
              {(totalLowEffort > 0 || totalOffTopic > 0) && (
                <div
                  className="text-xs text-muted/70 pt-1"
                  title="Session-wide telemetry. Mastery scorer reads these to penalize patterns even when no consecutive chain hit threshold."
                >
                  Total: {totalLowEffort} low / {totalOffTopic} off
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex-1" />

        <AccountPopover studentId={studentId} />
      </div>
    </aside>
  );
}
