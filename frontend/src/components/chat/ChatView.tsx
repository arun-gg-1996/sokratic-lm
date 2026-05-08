import { Composer } from "./Composer";
import { ConnectionBanner } from "./ConnectionBanner";
import { MessageList } from "./MessageList";
import { OptInCard } from "../cards/OptInCard";
import { TopicCard } from "../cards/TopicCard";
import { AnchorPickCard } from "../cards/AnchorPickCard";
import { ExitConfirmModal } from "../modals/ExitConfirmModal";
import { useSession } from "../../hooks/useSession";
import { useSessionStore } from "../../stores/sessionStore";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// QA-mode student profiles surfaced in the suggest-replies dropdown.
// Order matches the simulated-conversation eval bank used by graders.
const STUDENT_PROFILES: Array<{ value: string; label: string; tone: string }> = [
  { value: "S1", label: "Strong",        tone: "fast, thorough, confident" },
  { value: "S2", label: "Moderate",      tone: "average pace, mostly correct" },
  { value: "S3", label: "Weak",          tone: "slow, frequent confusions" },
  { value: "S4", label: "Overconfident", tone: "asserts wrong answers boldly" },
  { value: "S5", label: "Disengaged",    tone: "minimal effort, drifts off" },
  { value: "S6", label: "Anxious",       tone: "hedges, second-guesses self" },
];

/**
 * Custom student-profile dropdown.
 *
 * Native <select> opens an OS-rendered menu that ignores our CSS — the
 * font, hover color, and selected-row treatment all get the system look.
 * To keep the panel consistent with the rest of the app's typography
 * (sans-serif + accent-tinted hover) we render our own popover below
 * the trigger button. Keyboard navigation (Enter/Escape/Arrow) and
 * focus management are wired up explicitly so we don't lose the a11y
 * affordances the native element gave us for free.
 */
function ProfileDropdown({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState<number>(() =>
    Math.max(0, STUDENT_PROFILES.findIndex((p) => p.value === value)),
  );
  // Measured anchor position for the portal-rendered panel. We
  // re-measure on open + on resize so the menu stays glued to the
  // trigger even if the layout shifts.
  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLUListElement | null>(null);

  const current = STUDENT_PROFILES.find((p) => p.value === value) ?? STUDENT_PROFILES[1];

  // Recompute the trigger's screen rect whenever the menu opens, the
  // window resizes, or the chat surface scrolls. We render the panel
  // via createPortal at document.body so a parent's overflow-hidden
  // can't clip it (and stacking-context siblings can't paint over it),
  // which is what was burying the menu behind the message bubbles.
  useLayoutEffect(() => {
    if (!open) return;
    const measure = () => {
      const el = buttonRef.current;
      if (el) setAnchorRect(el.getBoundingClientRect());
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true); // capture inner scrolls
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open]);

  // Close on outside click. The panel lives in a portal, so the check
  // has to allow clicks inside EITHER the trigger root or the portaled
  // panel — neither is a DOM descendant of the other.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (rootRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  // Keyboard navigation.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open && (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) {
      e.preventDefault();
      setOpen(true);
      return;
    }
    if (!open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      buttonRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((i) => (i + 1) % STUDENT_PROFILES.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((i) => (i - 1 + STUDENT_PROFILES.length) % STUDENT_PROFILES.length);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const picked = STUDENT_PROFILES[highlight];
      if (picked) {
        onChange(picked.value);
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
  };

  return (
    <div
      ref={rootRef}
      className="relative inline-flex items-center font-sans"
      onKeyDown={onKeyDown}
    >
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex items-center gap-2 bg-bg border border-border rounded-full pl-1.5 pr-2.5 py-1 text-xs font-medium text-fg/90 hover:border-accent focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 cursor-pointer transition"
        title={current.tone}
      >
        <span
          className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-accent/15 text-accent text-[10px] font-semibold tabular-nums"
          aria-hidden="true"
        >
          {current.value}
        </span>
        <span className="font-sans">{current.label}</span>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          className={`w-3 h-3 text-muted transition-transform ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.06l3.71-3.83a.75.75 0 1 1 1.08 1.04l-4.25 4.39a.75.75 0 0 1-1.08 0L5.21 8.27a.75.75 0 0 1 .02-1.06Z"
            clipRule="evenodd"
          />
        </svg>
      </button>

      {open && anchorRect && createPortal(
        <ul
          ref={panelRef}
          role="listbox"
          aria-label="Student profile"
          // Render at document.body via portal with position:fixed so
          // ancestor overflow / stacking contexts can't bury the menu.
          // z-50 sits above modal backdrops too.
          style={{
            position: "fixed",
            top: anchorRect.bottom + 6,
            left: anchorRect.left,
            width: 288,
          }}
          className="z-50 max-w-[calc(100vw-2rem)] rounded-card border border-border bg-panel shadow-lg py-1 font-sans overflow-hidden"
        >
          {STUDENT_PROFILES.map((p, idx) => {
            const isSelected = p.value === value;
            const isHighlighted = idx === highlight;
            return (
              <li
                key={p.value}
                role="option"
                aria-selected={isSelected}
                onMouseEnter={() => setHighlight(idx)}
                onClick={() => {
                  onChange(p.value);
                  setOpen(false);
                  buttonRef.current?.focus();
                }}
                className={`mx-1 my-0.5 px-2.5 py-2 rounded-lg flex items-start gap-2.5 cursor-pointer transition ${
                  isHighlighted
                    ? "bg-accent/10"
                    : "hover:bg-bg"
                }`}
              >
                <span
                  className={`shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full text-[10px] font-semibold tabular-nums ${
                    isSelected
                      ? "bg-accent text-white"
                      : "bg-accent/15 text-accent"
                  }`}
                  aria-hidden="true"
                >
                  {p.value}
                </span>
                <span className="flex-1 min-w-0">
                  <span className="block text-xs font-medium text-fg leading-tight">
                    {p.label}
                  </span>
                  <span className="block text-[11px] text-muted leading-snug mt-0.5">
                    {p.tone}
                  </span>
                </span>
                {isSelected && (
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    className="shrink-0 w-3.5 h-3.5 text-accent mt-0.5"
                    aria-hidden="true"
                  >
                    <path
                      fillRule="evenodd"
                      d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
                      clipRule="evenodd"
                    />
                  </svg>
                )}
              </li>
            );
          })}
        </ul>,
        document.body,
      )}
    </div>
  );
}

/**
 * Header above the chat viewport. Houses the "Suggest answers" QA toggle
 * (with student-profile selector when enabled) and the End-session
 * affordance. Designed as a subtly-tinted strip — not a hard divider —
 * so the chat scroll feels continuous beneath it.
 */
function ChatHeader({ onEndSession }: { onEndSession: () => void }) {
  const suggestEnabled = useSessionStore((s) => s.suggestEnabled);
  const suggestProfile = useSessionStore((s) => s.suggestProfile);
  const setSuggestEnabled = useSessionStore((s) => s.setSuggestEnabled);
  const setSuggestProfile = useSessionStore((s) => s.setSuggestProfile);

  return (
    <div className="shrink-0 px-4 py-2.5 flex items-center justify-between gap-3 bg-panel/30 border-b border-border/40 backdrop-blur-sm">
      <div className="flex items-center gap-2">
        {/* Toggle pill — replaces the bare checkbox. The whole pill is
            the click target; the inner switch dot animates left/right
            so the toggle state is unambiguous at a glance. */}
        <button
          type="button"
          role="switch"
          aria-checked={suggestEnabled}
          onClick={() => setSuggestEnabled(!suggestEnabled)}
          className={`group inline-flex items-center gap-2 rounded-full pl-1 pr-3 py-1 text-xs font-medium transition border ${
            suggestEnabled
              ? "bg-accent/10 border-accent/40 text-accent hover:bg-accent/15"
              : "bg-bg border-border text-muted hover:border-muted/60 hover:text-fg"
          }`}
          title="Show suggested student replies (QA / demo helper)"
        >
          <span
            className={`relative inline-flex w-7 h-4 rounded-full transition-colors ${
              suggestEnabled ? "bg-accent" : "bg-muted/30"
            }`}
            aria-hidden="true"
          >
            <span
              className={`absolute top-0.5 w-3 h-3 rounded-full bg-bg shadow-sm transition-all ${
                suggestEnabled ? "left-3.5" : "left-0.5"
              }`}
            />
          </span>
          <span>Suggest answers</span>
        </button>

        {/* Profile selector — only when suggest is on. Custom dropdown
            (not native <select>) so the open menu uses our typography +
            hover treatment instead of the OS-rendered system look. */}
        {suggestEnabled && (
          <ProfileDropdown
            value={suggestProfile}
            onChange={setSuggestProfile}
          />
        )}
      </div>

      <button
        onClick={onEndSession}
        className="inline-flex items-center gap-1.5 text-xs font-medium text-red-600 dark:text-red-400 px-3 py-1.5 rounded-lg border border-red-500/30 hover:border-red-500 hover:bg-red-500/10 transition"
        aria-label="End this session"
        title="End the current session and save progress"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          className="w-3.5 h-3.5"
          aria-hidden="true"
        >
          <path fillRule="evenodd" d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM8.28 7.22a.75.75 0 0 0-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 1 0 1.06 1.06L10 11.06l1.72 1.72a.75.75 0 1 0 1.06-1.06L11.06 10l1.72-1.72a.75.75 0 0 0-1.06-1.06L10 8.94 8.28 7.22Z" clipRule="evenodd" />
        </svg>
        <span>End session</span>
      </button>
    </div>
  );
}

export function ChatSurface() {
  const { submitMessage, restartSession, requestExitSession, cancelExitIntent } = useSession();
  const pendingChoice = useSessionStore((s) => s.pendingChoice);
  const sessionPhase = useSessionStore((s) => s.sessionPhase);
  const sessionEnded = useSessionStore((s) => s.sessionEnded);
  const exitIntentPending = useSessionStore((s) => s.exitIntentPending);
  const setPendingChoice = useSessionStore((s) => s.setPendingChoice);
  const [exitModalOpen, setExitModalOpen] = useState(false);
  // M1 — modal opens either via header [End session] button OR when
  // backend signals exit_intent_pending=true (preflight detected deflection).
  const showExitModal = exitModalOpen || exitIntentPending;
  const isTerminal = sessionPhase === "memory_update" || sessionEnded;

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      {/* L80.f — WS lifecycle banner; hidden during healthy connection. */}
      <ConnectionBanner />
      {/* M1 — chat header with persistent [End session] button. Hidden once
          session is terminal (banner takes over from Composer).
          added "Suggest answers"
          toggle + profile dropdown for QA / demo testing. */}
      {!isTerminal && (
        <ChatHeader onEndSession={() => setExitModalOpen(true)} />
      )}
      <ExitConfirmModal
        open={showExitModal}
        onCancel={() => {
          setExitModalOpen(false);
          if (exitIntentPending) cancelExitIntent();
        }}
        onConfirm={() => {
          setExitModalOpen(false);
          requestExitSession();
        }}
      />
      <MessageList />
      {!isTerminal && pendingChoice?.kind === "opt_in" && (
        <OptInCard options={pendingChoice.options} onSelect={submitMessage} />
      )}
      {!isTerminal && pendingChoice?.kind === "confirm_topic" && (
        <OptInCard
          options={pendingChoice.options}
          onSelect={submitMessage}
          label="Confirm topic:"
        />
      )}
      {!isTerminal && pendingChoice?.kind === "topic" && (
        <TopicCard
          options={pendingChoice.options}
          onSelect={submitMessage}
          allowCustom={pendingChoice.allow_custom !== false}
          endSessionLabel={pendingChoice.end_session_label}
          endSessionValue={pendingChoice.end_session_value}
          onSomethingElse={() => setPendingChoice(null)}
        />
      )}
      {/* M4 (B6) — anchor question picker for prelocked sessions. */}
      {!isTerminal && pendingChoice?.kind === "anchor_pick" && (
        <AnchorPickCard
          options={pendingChoice.options}
          subsection={pendingChoice.subsection}
          onSelect={submitMessage}
        />
      )}
      {!pendingChoice && !isTerminal && <Composer onSubmit={submitMessage} />}
      {isTerminal && (
        <div className="shrink-0 border-t border-border bg-bg">
          <div className="max-w-lane mx-auto px-6 py-4 text-center text-muted">
            <p>Session complete. Your progress has been saved.</p>
            <button
              onClick={restartSession}
              className="mt-2 text-accent underline"
            >
              Start a new chat
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
