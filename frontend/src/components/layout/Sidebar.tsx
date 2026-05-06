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
  // F10 (POST_DEMO_FIXES.md, 2026-05-06): show CLINICAL chip starting at
  // assessment_turn=1 (opt-in rendered) instead of waiting for
  // assessment_turn=2 (clinical scenario). The opt-in IS part of the
  // assessment phase per backend (state.phase === "assessment"), and the
  // tutor message says "Nice work getting there! Clinical bonus?" — keeping
  // the sidebar at TUTORING during opt-in contradicts that prose.
  if (phase === "assessment" && assessmentTurn >= 1) return "clinical";
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

  const debugMode = useUserStore((s) => s.debugMode);
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
  // F6 (POST_DEMO_FIXES.md, 2026-05-06): non-resetting help-abuse counter
  // (consecutive `help_abuse_count` resets on engagement; this stays).
  const totalHelpAbuse = num(debug, "total_help_abuse_turns", 0);
  // N3 (POST_DEMO_FIXES.md, 2026-05-06): clinical phase mirror counters.
  // Surfaced separately from tutoring counters so the user can see how
  // engagement quality differs between core tutoring and the clinical
  // application loop.
  const clinicalHelpAbuse = num(debug, "clinical_help_abuse_count", 0);
  const clinicalLowEffort = num(debug, "clinical_low_effort_count", 0);
  const clinicalOffTopic = num(debug, "clinical_off_topic_count", 0);
  const clinicalStrikeThreshold = num(debug, "clinical_strike_threshold", 2);
  const totalClinicalLowEffort = num(debug, "total_clinical_low_effort_turns", 0);
  const totalClinicalOffTopic = num(debug, "total_clinical_off_topic_turns", 0);
  const totalClinicalHelpAbuse = num(debug, "total_clinical_help_abuse_turns", 0);

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

  // N6 (POST_DEMO_FIXES.md, 2026-05-06): hint level color escalation.
  // Different curve from counterColor — hint level signals scaffolding
  // depth, not strike accumulation. 0 = neutral; ≥1 = visible signal
  // that we're in scaffolded territory; max+1 = exhausted (red).
  const hintColor = (level: number, max: number): string => {
    if (level >= max + 1) return "text-red-500";       // exhausted
    if (level >= max) return "text-orange-400";          // last hint
    if (level >= 2) return "text-amber-300";             // mid-scaffold
    if (level >= 1) return "text-yellow-300";            // first hint shown
    return "text-muted";                                 // pristine
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
          <div className="space-y-1">
            <div
              className={`rounded-card border px-3 py-2 text-center text-sm font-semibold tracking-wide uppercase transition-colors duration-300 ${PHASE_BADGE_CLASS[phase]}`}
              title={`Current session phase: ${PHASE_LABEL[phase]}`}
            >
              {PHASE_LABEL[phase]}
            </div>
            {/* Block G (POST_DEMO_FIXES.md, 2026-05-06) — EXPLORING
                sub-badge. Only visible when Dean fired needs_exploration
                this turn. Shown to ALL users (not gated by debugMode)
                because it provides student-facing context for what
                the tutor is doing right now: searching adjacent
                content. Auto-clears on the next on-topic turn.
                Compact; one extra line; teal so it doesn't blend with
                phase chip colors (rapport=blue, tutoring=emerald,
                clinical=amber, wrap=muted). */}
            {phase === "tutoring" && bool(debug, "currently_exploring") && (
              <div
                className="rounded-card border border-teal-500/30 bg-teal-500/15 text-teal-300 px-3 py-1 text-[11px] tracking-wide"
                title="Tutor is fetching adjacent textbook content because the student asked a tangential question. Auto-clears on the next on-topic turn."
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium uppercase">↳ Exploring</span>
                  <span className="text-teal-300/80">
                    ({num(debug, "exploration_count", 0)})
                  </span>
                </div>
                {str(debug, "exploration_query") && (
                  <div className="text-teal-300/70 truncate text-[10.5px] mt-0.5 italic">
                    "{str(debug, "exploration_query")}"
                  </div>
                )}
              </div>
            )}
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
              className={`text-sm ${counterColor(prelockLoopCount, 10)}`}
              title="After 10 attempts to pick a topic, a guided picker appears. Currently at this many attempts."
            >
              Pre-lock: {prelockLoopCount}/10
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
                className={`text-sm ${hintColor(hintLevel, maxHints)}`}
                title="Hints get more direct as the level rises. Cap is 3; level 4 forces session close."
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
              so the student can see counters tick before they hit thresholds.
              N3 (POST_DEMO_FIXES.md, 2026-05-06): in clinical phase, show
              the clinical_* mirror counters instead of tutoring's. */}
          {showStrikePills && phase !== "clinical" && (
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
              {(totalLowEffort > 0 || totalOffTopic > 0 || totalHelpAbuse > 0) && (
                <div
                  className="text-xs text-muted/70 pt-1"
                  title="Session-wide telemetry. Mastery scorer reads these to penalize patterns even when no consecutive chain hit threshold."
                >
                  Total: {totalLowEffort} low / {totalOffTopic} off / {totalHelpAbuse} demand
                </div>
              )}
            </div>
          )}

          {/* N3 — Clinical phase counters (mirror of tutoring's). Visible
              ONLY in clinical so the user can compare engagement quality
              across phases. Termination semantics differ: clinical doesn't
              terminate on strike — only the natural CLINICAL_TURN_CAP ends
              the loop. The threshold display is for reference only. */}
          {showStrikePills && phase === "clinical" && (
            <div className="pt-2 mt-2 border-t border-border space-y-1">
              <div className="text-xs text-muted/70">Clinical health</div>
              <div
                className={`text-xs ${counterColor(clinicalLowEffort, clinicalStrikeThreshold)}`}
                title="Clinical-phase consecutive low-effort turns (telemetry only — clinical never terminates on strike, just on the natural turn cap)."
              >
                Low-effort: {clinicalLowEffort}/{clinicalStrikeThreshold}
              </div>
              <div
                className={`text-xs ${counterColor(clinicalHelpAbuse, clinicalStrikeThreshold)}`}
                title="Clinical-phase consecutive help-abuse turns (telemetry only)."
              >
                Help-abuse: {clinicalHelpAbuse}/{clinicalStrikeThreshold}
              </div>
              <div
                className={`text-xs ${counterColor(clinicalOffTopic, clinicalStrikeThreshold)}`}
                title="Clinical-phase consecutive off-domain turns (telemetry only)."
              >
                Off-topic: {clinicalOffTopic}/{clinicalStrikeThreshold}
              </div>
              {(totalClinicalLowEffort > 0 || totalClinicalOffTopic > 0 || totalClinicalHelpAbuse > 0) && (
                <div
                  className="text-xs text-muted/70 pt-1"
                  title="Session-wide clinical telemetry."
                >
                  Total: {totalClinicalLowEffort} low / {totalClinicalOffTopic} off / {totalClinicalHelpAbuse} demand
                </div>
              )}
            </div>
          )}

          {/* Block G (POST_DEMO_FIXES.md, 2026-05-06) — Engagement
              details panel. Visible ONLY when debugMode is on (the
              existing toggle persists in localStorage). Splits into
              Engagement / Hint-advance audit. Diagnostic only — these
              counters do NOT drive control logic; they're for
              testing + debugging visibility. Never shown to students
              by default to avoid clutter. */}
          {debugMode && showStrikePills && (
            <details className="text-xs pt-2 mt-2 border-t border-border" open>
              <summary className="cursor-pointer text-muted/70 hover:text-muted">
                Engagement details (debug)
              </summary>
              <div className="pl-2 pt-2 space-y-1.5">
                <div className="text-muted/60 uppercase tracking-wide text-[10px]">
                  Engagement quality
                </div>
                <div
                  className="text-xs text-muted/80"
                  title="Turns where the student engaged on-topic but did not reach the locked answer. Higher = more genuine effort despite missing the target."
                >
                  Engaged but wrong:{" "}
                  <span className="font-mono text-muted">
                    {num(debug, "engaged_wrong_count", 0)}
                  </span>
                </div>

                <div className="text-muted/60 uppercase tracking-wide text-[10px] pt-1">
                  Topic discipline
                </div>
                <div
                  className="text-xs text-muted/80"
                  title="Cumulative tangent retrievals fired this session. Tangent = student asked an OT-related sub-aspect outside the locked anchor's chunks."
                >
                  Tangents (cumulative):{" "}
                  <span className="font-mono text-muted">
                    {num(debug, "exploration_count", 0)}
                  </span>
                </div>

                <div className="text-muted/60 uppercase tracking-wide text-[10px] pt-1">
                  Hint advance audit
                </div>
                <div
                  className="text-xs text-muted/80"
                  title="Hint advances fired by Dean's TurnPlan signal (substantive-but-wrong attempts). Adds to the engaged-wrong count."
                >
                  Dean override:{" "}
                  <span className="font-mono text-muted">
                    {num(debug, "dean_hint_override_count", 0)}
                  </span>
                </div>
                <div
                  className="text-xs text-muted/80"
                  title="Hint advances forced by rule-based thresholds: help_abuse strike-4 OR consecutive_low_effort streak-4. Independent of Dean's vote."
                >
                  Rule advance:{" "}
                  <span className="font-mono text-muted">
                    {num(debug, "rule_hint_advance_count", 0)}
                  </span>
                </div>
              </div>
            </details>
          )}
        </div>
        )}

        <div className="flex-1" />

        <AccountPopover studentId={studentId} />
      </div>
    </aside>
  );
}
