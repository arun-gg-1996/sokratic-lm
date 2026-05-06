import { Composer } from "./Composer";
import { ConnectionBanner } from "./ConnectionBanner";
import { MessageList } from "./MessageList";
import { OptInCard } from "../cards/OptInCard";
import { TopicCard } from "../cards/TopicCard";
import { AnchorPickCard } from "../cards/AnchorPickCard";
import { ExitConfirmModal } from "../modals/ExitConfirmModal";
import { useSession } from "../../hooks/useSession";
import { useSessionStore } from "../../stores/sessionStore";
import { useState } from "react";

// N8 (POST_DEMO_FIXES.md, 2026-05-06): chat header with End-session +
// "Suggest answers" toggle + profile dropdown.
function ChatHeader({ onEndSession }: { onEndSession: () => void }) {
  const suggestEnabled = useSessionStore((s) => s.suggestEnabled);
  const suggestProfile = useSessionStore((s) => s.suggestProfile);
  const setSuggestEnabled = useSessionStore((s) => s.setSuggestEnabled);
  const setSuggestProfile = useSessionStore((s) => s.setSuggestProfile);
  return (
    <div className="shrink-0 border-b border-border px-4 py-2 flex items-center justify-between gap-2">
      <div className="flex items-center gap-3 text-xs text-muted">
        <label className="flex items-center gap-1.5 cursor-pointer hover:text-text transition">
          <input
            type="checkbox"
            checked={suggestEnabled}
            onChange={(e) => setSuggestEnabled(e.target.checked)}
            className="accent-accent"
          />
          <span>Suggest answers</span>
        </label>
        {suggestEnabled && (
          <select
            value={suggestProfile}
            onChange={(e) => setSuggestProfile(e.target.value)}
            className="bg-bg border border-border rounded px-2 py-0.5 text-xs hover:border-accent focus:outline-none focus:border-accent"
            title="Student profile for suggested replies"
          >
            <option value="S1">S1 — Strong</option>
            <option value="S2">S2 — Moderate</option>
            <option value="S3">S3 — Weak</option>
            <option value="S4">S4 — Overconfident</option>
            <option value="S5">S5 — Disengaged</option>
            <option value="S6">S6 — Anxious</option>
          </select>
        )}
      </div>
      <button
        onClick={onEndSession}
        className="text-xs text-muted hover:text-red-600 dark:text-red-400 transition px-2 py-1 rounded border border-border hover:border-red-500"
        aria-label="End this session"
      >
        End session
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
          N8 (POST_DEMO_FIXES.md, 2026-05-06): added "Suggest answers"
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
