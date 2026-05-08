/**
 * Sidebar — phase badge + phase-contextual counters + tooltips
 * (L80.a + L80.c + L80.h from the UX polish pass)
 *
 * Counter strategy .a:
 *   - Pre-lock (topic not confirmed): show prelock_loop_count/7 only
 *   - Tutoring (topic locked, phase=tutoring): turn_count, hint_level,
 *     conversation-health strike pills as they accumulate
 *   - Clinical (phase=assessment, assessment_turn>=2): clinical_turn_count
 *     plus the same strike pills (counters tick during clinical *     but do not escalate)
 *   - memory_update / wrap-up: counters fade out
 *
 * Phase badge .c renders at the top of the sidebar with a
 * color-coded label so the student always knows what mode they're in.
 *
 * Tooltips .h are attached to every counter pill via the
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
  // show CLINICAL chip starting at
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
  // non-resetting help-abuse counter
  // (consecutive `help_abuse_count` resets on engagement; this stays).
  const totalHelpAbuse = num(debug, "total_help_abuse_turns", 0);
  // clinical phase mirror counters.
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

  // hint level color escalation.
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
            {/* Block G — EXPLORING
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
              className={`text-sm flex items-baseline justify-between gap-2 ${counterColor(prelockLoopCount, 10)}`}
              title="After 10 attempts to pick a topic, a guided picker appears. Currently at this many attempts."
            >
              <span>Pre-lock</span>
              <span className="font-mono tabular-nums">{prelockLoopCount}/10</span>
            </div>
          )}

          {phase === "tutoring" && (
            <>
              <div
                className={`text-sm flex items-baseline justify-between gap-2 ${counterColor(turnCount, maxTurns)}`}
                title={`Tutoring sessions are capped at ${maxTurns} turns. Currently at this many turns.`}
              >
                <span>Turn</span>
                <span className="font-mono tabular-nums">{turnCount}/{maxTurns}</span>
              </div>
              <div
                className={`text-sm flex items-baseline justify-between gap-2 ${hintColor(hintLevel, maxHints)}`}
                title="Hints get more direct as the level rises. Cap is 3; level 4 forces session close."
              >
                <span>Hint</span>
                <span className="font-mono tabular-nums">
                  {hintsExhausted ? "exhausted" : `${displayHint}/${maxHints}`}
                </span>
              </div>
            </>
          )}

          {phase === "clinical" && (
            <>
              <div
                className={`text-sm flex items-baseline justify-between gap-2 ${counterColor(clinicalTurnCount, clinicalMaxTurns)}`}
                title={`Clinical phase is capped at ${clinicalMaxTurns} turns. Counter ticks then closes naturally.`}
              >
                <span>Clinical turn</span>
                <span className="font-mono tabular-nums">{clinicalTurnCount}/{clinicalMaxTurns}</span>
              </div>
              <div
                className="text-[10px] text-muted/70 italic"
                title="Tutoring complete; clinical scenario is the bonus phase."
              >
                Tutoring closed at turn {turnCount}
              </div>
            </>
          )}

          {phase === "wrap" && (
            <div className="text-xs text-muted/70 italic" title="Session wrapping up — saving memory + scoring mastery.">
              Wrapping up — saving session…
            </div>
          )}

          {topicConfirmed && (subsectionName || topicSelection) && phase !== "wrap" && (
            <details className="group pt-2 mt-1 border-t border-border/60" open>
              <summary className="cursor-pointer list-none flex items-center gap-1.5 text-xs font-medium text-fg/90 hover:text-fg transition">
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                  className="w-3 h-3 shrink-0 text-muted group-open:rotate-90 transition"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                </svg>
                <span className="text-[10px] uppercase tracking-wider text-muted/80 font-semibold">
                  Current topic
                </span>
              </summary>
              <div className="pt-2 space-y-2">
                {/* Curriculum hierarchy as a definition list — three rows
                    with small-caps labels and indented values so it scans
                    as structured data, not a paragraph. */}
                <dl className="space-y-1.5 text-xs">
                  {chapterName && (
                    <div className="flex flex-col">
                      <dt className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                        Chapter
                      </dt>
                      <dd className="text-fg/85 leading-snug">{chapterName}</dd>
                    </div>
                  )}
                  {sectionName && (
                    <div className="flex flex-col">
                      <dt className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                        Section
                      </dt>
                      <dd className="text-fg/85 leading-snug">{sectionName}</dd>
                    </div>
                  )}
                  {(subsectionName || topicSelection) && (
                    <div className="flex flex-col">
                      <dt className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                        Subsection
                      </dt>
                      <dd className="text-fg font-medium leading-snug">
                        {subsectionName || topicSelection}
                      </dd>
                    </div>
                  )}
                </dl>
                {/* Locked question rendered as a quote-style block so it
                    visually separates from the curriculum hierarchy
                    above. The accent-colored left border anchors it as
                    "this is what the student is working on right now". */}
                {lockedQuestionText && (
                  <div className="pt-2 mt-2 border-t border-border/60">
                    <div className="text-[9px] uppercase tracking-wider text-muted/60 font-medium mb-1">
                      Question
                    </div>
                    <blockquote className="border-l-2 border-accent/50 pl-2.5 text-xs italic text-fg/85 leading-snug">
                      {lockedQuestionText}
                    </blockquote>
                  </div>
                )}
              </div>
            </details>
          )}

          {/* Conversation-health pills — N2: always visible during tutoring/clinical
              so the student can see counters tick before they hit thresholds.
              in clinical phase, show
              the clinical_* mirror counters instead of tutoring's. */}
          {showStrikePills && phase !== "clinical" && (
            <div className="pt-2 mt-2 border-t border-border/60 space-y-1.5">
              <div className="text-[10px] uppercase tracking-wider text-muted/70 font-semibold">
                Conversation health
              </div>
              <div
                className={`text-xs flex items-baseline justify-between gap-2 ${counterColor(consecutiveLowEffort, lowEffortThreshold)}`}
                title={`Consecutive passive 'i don't know' / 'idk' turns. At ${lowEffortThreshold}, the dean advances the hint level. Counter resets on any genuine attempt.`}
              >
                <span>Low-effort</span>
                <span className="font-mono tabular-nums">{consecutiveLowEffort}/{lowEffortThreshold}</span>
              </div>
              <div
                className={`text-xs flex items-baseline justify-between gap-2 ${counterColor(helpAbuseCount, helpAbuseThreshold)}`}
                title={`Active 'just tell me' / 'skip' demands. At ${helpAbuseThreshold}, the dean force-advances the hint level. Counter resets on any genuine attempt.`}
              >
                <span>Help-abuse</span>
                <span className="font-mono tabular-nums">{helpAbuseCount}/{helpAbuseThreshold}</span>
              </div>
              <div
                className={`text-xs flex items-baseline justify-between gap-2 ${counterColor(offTopicCount, offTopicThreshold)}`}
                title={`Consecutive off-DOMAIN turns (in-domain tangents don't count). At ${offTopicThreshold}, the session ends gracefully. Counter resets on any engaged turn.`}
              >
                <span>Off-topic</span>
                <span className="font-mono tabular-nums">{offTopicCount}/{offTopicThreshold}</span>
              </div>
              {(totalLowEffort > 0 || totalOffTopic > 0 || totalHelpAbuse > 0) && (
                <div
                  className="text-[10px] text-muted/60 pt-1 italic"
                  title="Session-wide telemetry. Mastery scorer reads these to penalize patterns even when no consecutive chain hit threshold."
                >
                  Session totals: {totalLowEffort} low · {totalOffTopic} off · {totalHelpAbuse} demand
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
            <div className="pt-2 mt-2 border-t border-border/60 space-y-1.5">
              <div className="text-[10px] uppercase tracking-wider text-muted/70 font-semibold">
                Clinical health
              </div>
              <div
                className={`text-xs flex items-baseline justify-between gap-2 ${counterColor(clinicalLowEffort, clinicalStrikeThreshold)}`}
                title="Clinical-phase consecutive low-effort turns (telemetry only — clinical never terminates on strike, just on the natural turn cap)."
              >
                <span>Low-effort</span>
                <span className="font-mono tabular-nums">{clinicalLowEffort}/{clinicalStrikeThreshold}</span>
              </div>
              <div
                className={`text-xs flex items-baseline justify-between gap-2 ${counterColor(clinicalHelpAbuse, clinicalStrikeThreshold)}`}
                title="Clinical-phase consecutive help-abuse turns (telemetry only)."
              >
                <span>Help-abuse</span>
                <span className="font-mono tabular-nums">{clinicalHelpAbuse}/{clinicalStrikeThreshold}</span>
              </div>
              <div
                className={`text-xs flex items-baseline justify-between gap-2 ${counterColor(clinicalOffTopic, clinicalStrikeThreshold)}`}
                title="Clinical-phase consecutive off-domain turns (telemetry only)."
              >
                <span>Off-topic</span>
                <span className="font-mono tabular-nums">{clinicalOffTopic}/{clinicalStrikeThreshold}</span>
              </div>
              {(totalClinicalLowEffort > 0 || totalClinicalOffTopic > 0 || totalClinicalHelpAbuse > 0) && (
                <div
                  className="text-[10px] text-muted/60 pt-1 italic"
                  title="Session-wide clinical telemetry."
                >
                  Session totals: {totalClinicalLowEffort} low · {totalClinicalOffTopic} off · {totalClinicalHelpAbuse} demand
                </div>
              )}
            </div>
          )}

          {/* Debug section — visible ONLY when debugMode is on. Renders
              all grader-only fields inline in the sidebar panel (no
              chat-window debug card). Sections:
                Locked target — locked_answer + aliases + full_answer
                Pacing       — turn budget %, urgency tier, exploration budget
                Last verdict — last preflight category for the most recent turn
                Counters     — engaged_wrong / tangents / hint advances (audit)
              These do NOT drive control logic; they're for grader
              visibility into what the gate is comparing against and
              what tier the dean's pacing is in. */}
          {debugMode && (
            <div className="text-xs pt-2 mt-2 border-t border-border space-y-3">
              <div className="text-[10px] uppercase tracking-wider text-accent font-semibold">
                Debug
              </div>

              {/* Locked target — what the reach gate compares against */}
              {(str(debug, "locked_answer") || str(debug, "full_answer")) && (
                <div className="space-y-1.5">
                  <div className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                    Locked target
                  </div>
                  {str(debug, "locked_answer") && (
                    <div className="text-xs">
                      <span className="text-muted/70">answer: </span>
                      <span className="font-medium text-fg">
                        {str(debug, "locked_answer")}
                      </span>
                    </div>
                  )}
                  {Array.isArray((debug as Record<string, unknown>)?.locked_answer_aliases) &&
                    ((debug as Record<string, unknown>).locked_answer_aliases as string[]).length > 0 && (
                      <div className="text-xs leading-snug">
                        <span className="text-muted/70">aliases: </span>
                        <span className="text-fg/80">
                          {((debug as Record<string, unknown>).locked_answer_aliases as string[]).join(", ")}
                        </span>
                      </div>
                    )}
                  {str(debug, "full_answer")
                    && str(debug, "full_answer") !== str(debug, "locked_answer") && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-muted/70 hover:text-muted">
                          full_answer (textbook)
                        </summary>
                        <div className="pt-1 pl-2 text-fg/80 leading-snug">
                          {str(debug, "full_answer")}
                        </div>
                      </details>
                    )}
                </div>
              )}

              {/* Pacing — turn budget + urgency tier + exploration budget */}
              <div className="space-y-1.5">
                <div className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                  Pacing
                </div>
                {(() => {
                  const turn = num(debug, "turn_count", 0);
                  const max = num(debug, "max_turns", 25);
                  const pct = max > 0 ? Math.round((turn / max) * 100) : 0;
                  const tier = str(debug, "urgency_tier") || "early";
                  const tierColor = {
                    early: "text-muted",
                    mid: "text-muted/90",
                    late: "text-amber-600 dark:text-amber-400",
                    final_stretch: "text-red-600 dark:text-red-400",
                  }[tier] ?? "text-muted";
                  return (
                    <>
                      <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                        title={`Session turn budget. ${turn} of ${max} turns used (${pct}%).`}
                      >
                        <span>Session</span>
                        <span className="font-mono tabular-nums">{turn}/{max} ({pct}%)</span>
                      </div>
                      <div
                        className={`text-xs flex items-baseline justify-between gap-2 ${tierColor}`}
                        title="Urgency tier scales pacing pressure. early→expansive, mid→orient, late→direct hints, final_stretch→push for commitment."
                      >
                        <span>Urgency</span>
                        <span className="font-mono">{tier}</span>
                      </div>
                      <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                        title={`Exploration retrievals used this session. Cap is runaway protection only — ${num(debug, "exploration_max", 10)}. Pacing pressure comes from urgency_tier, not from this counter.`}
                      >
                        <span>Exploration</span>
                        <span className="font-mono tabular-nums">
                          {num(debug, "exploration_used", 0)}/{num(debug, "exploration_max", 10)}
                        </span>
                      </div>
                    </>
                  );
                })()}
              </div>

              {/* Last preflight verdict — what the unified intent
                  classifier said about the most recent student turn */}
              {str(debug, "last_preflight_category") && (
                <div className="space-y-1.5">
                  <div className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                    Last preflight
                  </div>
                  <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                    title="Verdict from the unified intent classifier on the most recent student turn (on_topic_engaged | exploration | low_effort | help_abuse | off_domain | deflection | opt_in_*)."
                  >
                    <span>Verdict</span>
                    <span className="font-mono">{str(debug, "last_preflight_category")}</span>
                  </div>
                </div>
              )}

              {/* Engagement audit — diagnostic counters, NOT control */}
              {showStrikePills && (
                <div className="space-y-1.5">
                  <div className="text-[9px] uppercase tracking-wider text-muted/60 font-medium">
                    Engagement audit
                  </div>
                  <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                    title="Turns where the student engaged on-topic but did not reach the locked answer."
                  >
                    <span>Engaged-wrong</span>
                    <span className="font-mono tabular-nums">{num(debug, "engaged_wrong_count", 0)}</span>
                  </div>
                  <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                    title="Recent-tangent frequency (decays on engaged turns, increments on tangents)."
                  >
                    <span>Recent tangents</span>
                    <span className="font-mono tabular-nums">{num(debug, "exploration_count", 0)}</span>
                  </div>
                  <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                    title="Hint advances fired by Dean's TurnPlan signal (substantive-but-wrong attempts)."
                  >
                    <span>Dean hint-advance</span>
                    <span className="font-mono tabular-nums">{num(debug, "dean_hint_override_count", 0)}</span>
                  </div>
                  <div className="text-xs flex items-baseline justify-between gap-2 text-muted/80"
                    title="Hint advances forced by rule-based thresholds (help_abuse strike-4 / consecutive_low_effort streak-4)."
                  >
                    <span>Rule hint-advance</span>
                    <span className="font-mono tabular-nums">{num(debug, "rule_hint_advance_count", 0)}</span>
                  </div>
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
