/**
 * Sidebar — phase badge + phase-contextual counters + tooltips
 * (L80.a + L80.c + L80.h from the UX polish pass)
 *
 * Counter strategy per L80.a:
 *   - Pre-lock (topic not confirmed): show prelock_loop_count/7 only
 *   - Tutoring (topic locked, phase=tutoring): turn_count, hint_level,
 *     conversation-health strike pills as they accumulate
 *   - Clinical (phase=assessment, assessment_turn>=2): clinical_turn_count
 *     plus the same strike pills (counters tick during clinical per L70
 *     but do not escalate)
 *   - memory_update / wrap-up: counters fade out
 *
 * Phase badge per L80.c renders at the top of the sidebar with a
 * color-coded label so the student always knows what mode they're in.
 *
 * Tooltips per L80.h are attached to every counter pill via the
 * native `title` attribute (no extra dependencies). Each tooltip
 * explains what the counter measures + what triggers escalation.
 */
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useSessionStore } from "../../stores/sessionStore";
import { useUserStore } from "../../stores/userStore";
import { AccountPopover } from "../account/AccountPopover";

type DebugRecord = Record<string, unknown> | null;

function num(d: DebugRecord, key: string, fallback: number): number {
  const v = d?.[key];
  return typeof v === "number" ? v : fallback;
}

function str(d: DebugRecord, key: string, fallback = ""): string {
  const v = d?.[key];
  return typeof v === "string" ? v : fallback;
}

function bool(d: DebugRecord, key: string): boolean {
  return Boolean(d?.[key]);
}

type PhaseKind = "rapport" | "tutoring" | "clinical" | "wrap";

const PHASE_LABEL: Record<PhaseKind, string> = {
  rapport: "Rapport",
  tutoring: "Tutoring",
  clinical: "Clinical",
  wrap: "Wrapping up",
};

const PHASE_BADGE_CLASS: Record<PhaseKind, string> = {
  rapport: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  tutoring: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  clinical: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  wrap: "bg-muted/15 text-muted border-border",
};

function derivePhase(d: DebugRecord, topicConfirmed: boolean, assessmentTurn: number): PhaseKind {
  const phase = str(d, "phase");
  if (phase === "memory_update") return "wrap";
  if (phase === "assessment" && assessmentTurn >= 2) return "clinical";
  // assessment_turn 0/1 = opt-in flow; show as tutoring-tail rather than
  // a separate badge so the transition feels smooth.
  if (topicConfirmed) return "tutoring";
  return "rapport";
}

export function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  // Phase badge + counters only make sense in the chat view — they're
  // metadata for the active session, not global app state.
  const isChatView = location.pathname.startsWith("/chat");
  const studentId = useUserStore((s) => s.studentId);
  const resetSession = useSessionStore((s) => s.reset);
  const debug = useSessionStore((s) => s.debug) as DebugRecord;

  const turnCount = num(debug, "turn_count", 0);
  const maxTurns = num(debug, "max_turns", 25);
  const hintLevel = num(debug, "hint_level", 0);
  const maxHints = num(debug, "max_hints", 3);
  const displayHint = Math.min(hintLevel, maxHints);
  const hintsExhausted = hintLevel > maxHints;
  const topicConfirmed = bool(debug, "topic_confirmed");
  const assessmentTurn = num(debug, "assessment_turn", 0);
  const prelockLoopCount = num(debug, "prelock_loop_count", 0);
  const clinicalTurnCount = num(debug, "clinical_turn_count", 0);
  const clinicalMaxTurns = num(debug, "clinical_max_turns", 7);
  const topicSelection = str(debug, "topic_selection");
  const lockedTopicObj = (debug?.locked_topic && typeof debug.locked_topic === "object")
    ? (debug.locked_topic as Record<string, unknown>)
    : null;
  const chapterName = str(lockedTopicObj as DebugRecord, "chapter");
  const sectionName = str(lockedTopicObj as DebugRecord, "section");
  const subsectionName = str(lockedTopicObj as DebugRecord, "subsection") || topicSelection;
  const lockedQuestionText = str(debug, "locked_question");

  // Conversation-health counters
  const helpAbuseCount = num(debug, "help_abuse_count", 0);
  const helpAbuseThreshold = num(debug, "help_abuse_threshold", 4);
  const offTopicCount = num(debug, "off_topic_count", 0);
  const offTopicThreshold = num(debug, "off_topic_threshold", 4);
  const consecutiveLowEffort = num(debug, "consecutive_low_effort_count", 0);
  const lowEffortThreshold = num(debug, "low_effort_threshold", 4);
  const totalLowEffort = num(debug, "total_low_effort_turns", 0);
  const totalOffTopic = num(debug, "total_off_topic_turns", 0);

  const phase = derivePhase(debug, topicConfirmed, assessmentTurn);
  // N2: always surface counters during tutoring/clinical so the user can see
  // them tick before they hit thresholds (was gated on > 0 — confusing when
  // counters were silently incrementing in state but invisible in UI).
  const showStrikePills = (phase === "tutoring" || phase === "clinical");

  // Color escalation: green (default muted) → amber → red as cap nears.
  const counterColor = (count: number, max: number): string => {
    if (max <= 0) return "text-muted";
    const ratio = count / max;
    if (ratio >= 1) return "text-red-500";
    if (ratio >= 0.75) return "text-amber-400";
    if (ratio >= 0.5) return "text-amber-300";
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

        {/* L80.c — prominent phase badge.
            Phase + counters are session-scoped; only render in chat view
            so they don't leak onto Chats list / My Mastery / settings. */}
        {isChatView && (
          <div
            className={`rounded-card border px-3 py-2 text-center text-sm font-semibold tracking-wide uppercase transition-colors duration-300 ${PHASE_BADGE_CLASS[phase]}`}
            title={`Current session phase: ${PHASE_LABEL[phase]}`}
          >
            {PHASE_LABEL[phase]}
          </div>
        )}

        <button
          onClick={startNew}
          className="w-full rounded-card border border-border bg-bg px-4 py-2 text-left hover:border-accent transition"
        >
          + New chat
        </button>

        <nav className="flex flex-col gap-1 text-sm">
          <Link className="rounded-lg px-3 py-2 hover:bg-bg transition" to="/chat">
            Chats
          </Link>
          <Link className="rounded-lg px-3 py-2 hover:bg-bg transition" to="/mastery">
            My mastery
          </Link>
        </nav>

        {/* L80.a — phase-contextual counter panel (chat-view only) */}
        {isChatView && (
        <div className="rounded-card border border-border bg-bg px-3 py-3 space-y-2 text-sm transition-opacity duration-300">
          {phase === "rapport" && (
            <div
              className={`text-sm ${counterColor(prelockLoopCount, 7)}`}
              title="After 7 attempts to pick a topic, a guided picker appears. Currently at this many attempts."
            >
              Pre-lock: {prelockLoopCount}/7
            </div>
          )}

          {phase === "tutoring" && (
            <>
              <div
                className={`text-sm ${counterColor(turnCount, maxTurns)}`}
                title={`Tutoring sessions are capped at ${maxTurns} turns. Currently at this many turns.`}
              >
                Turn: {turnCount}/{maxTurns}
              </div>
              <div
                className="text-sm text-muted"
                title="Hints get more direct as the level rises. Cap is 3."
              >
                {hintsExhausted ? "Hints exhausted" : `Hint: ${displayHint}/${maxHints}`}
              </div>
            </>
          )}

          {phase === "clinical" && (
            <>
              <div
                className={`text-sm ${counterColor(clinicalTurnCount, clinicalMaxTurns)}`}
                title={`Clinical phase is capped at ${clinicalMaxTurns} turns. Counter ticks then closes naturally.`}
              >
                Clinical turn: {clinicalTurnCount}/{clinicalMaxTurns}
              </div>
              <div
                className="text-xs text-muted/70"
                title="Tutoring complete; clinical scenario is the bonus phase."
              >
                Tutoring done at turn {turnCount}
              </div>
            </>
          )}

          {phase === "wrap" && (
            <div className="text-xs text-muted/70" title="Session wrapping up — saving memory + scoring mastery.">
              Wrapping up — saving session
            </div>
          )}

          {topicConfirmed && (subsectionName || topicSelection) && phase !== "wrap" && (
            <details className="text-muted text-xs group pt-1">
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

          {/* Conversation-health pills — N2: always visible during tutoring/clinical
              so the student can see counters tick before they hit thresholds. */}
          {showStrikePills && (
            <div className="pt-2 mt-2 border-t border-border space-y-1">
              <div className="text-xs text-muted/70">Conversation health</div>
              <div
                className={`text-xs ${counterColor(consecutiveLowEffort, lowEffortThreshold)}`}
                title={`Consecutive passive 'i don't know' / 'idk' turns. At ${lowEffortThreshold}, the dean advances the hint level. Counter resets on any genuine attempt.`}
              >
                Low-effort: {consecutiveLowEffort}/{lowEffortThreshold}
              </div>
              <div
                className={`text-xs ${counterColor(helpAbuseCount, helpAbuseThreshold)}`}
                title={`Active 'just tell me' / 'skip' demands. At ${helpAbuseThreshold}, the dean force-advances the hint level. Counter resets on any genuine attempt.`}
              >
                Help-abuse: {helpAbuseCount}/{helpAbuseThreshold}
              </div>
              <div
                className={`text-xs ${counterColor(offTopicCount, offTopicThreshold)}`}
                title={`Consecutive off-DOMAIN turns (in-domain tangents don't count). At ${offTopicThreshold}, the session ends gracefully. Counter resets on any engaged turn.`}
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
        )}

        <div className="flex-1" />

        <AccountPopover studentId={studentId} />
      </div>
    </aside>
  );
}
