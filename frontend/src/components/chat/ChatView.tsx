import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import { OptInCard } from "../cards/OptInCard";
import { TopicCard } from "../cards/TopicCard";
import { useSession } from "../../hooks/useSession";
import { useSessionStore } from "../../stores/sessionStore";

export function ChatSurface() {
  const { submitMessage, restartSession } = useSession();
  const pendingChoice = useSessionStore((s) => s.pendingChoice);
  const sessionPhase = useSessionStore((s) => s.sessionPhase);
  const setPendingChoice = useSessionStore((s) => s.setPendingChoice);
  const isTerminal = sessionPhase === "memory_update";

  return (
    <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
      <MessageList />
      {!isTerminal && pendingChoice?.kind === "opt_in" && (
        <OptInCard options={pendingChoice.options} onSelect={submitMessage} />
      )}
      {!isTerminal && pendingChoice?.kind === "topic" && (
        <TopicCard
          options={pendingChoice.options}
          onSelect={submitMessage}
          onSomethingElse={() => setPendingChoice(null)}
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
