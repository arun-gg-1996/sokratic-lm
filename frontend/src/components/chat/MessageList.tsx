import { useEffect, useRef } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { ThinkingIndicator } from "./ThinkingIndicator";

export function MessageList() {
  const messages = useSessionStore((s) => s.messages);
  const isWaiting = useSessionStore((s) => s.isWaitingForTutor);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, isWaiting]);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-lane mx-auto px-6 py-8 space-y-4">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {isWaiting && (
          <div className="rounded-card bg-panel border border-border px-4 py-3 inline-flex">
            <ThinkingIndicator />
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
