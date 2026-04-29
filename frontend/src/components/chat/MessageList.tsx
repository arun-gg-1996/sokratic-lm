import { useEffect, useRef } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { ThinkingIndicator } from "./ThinkingIndicator";

export function MessageList() {
  const messages = useSessionStore((s) => s.messages);
  const isWaiting = useSessionStore((s) => s.isWaitingForTutor);
  // D.6a: live streaming buffer. While the backend streams the
  // teacher's draft, this string grows token-by-token. Render it as a
  // tutor-styled bubble so the user sees the response forming in
  // real time. On message_complete the buffer is cleared and a
  // permanent tutor message is added — keeps the rendered output
  // identical end-state to the non-streaming path.
  const streaming = useSessionStore((s) => s.streamingTutorContent);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, isWaiting, streaming.length]);

  // The "thinking…" indicator should only show while we are waiting
  // AND no tokens have streamed yet. Once tokens start arriving, the
  // streaming bubble below replaces the indicator (avoids both
  // showing simultaneously).
  const showThinking = isWaiting && streaming.length === 0;

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-lane mx-auto px-6 py-8 space-y-4">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {streaming && (
          <div className="rounded-card bg-panel border border-border px-4 py-3">
            <div className="whitespace-pre-wrap">{streaming}</div>
          </div>
        )}
        {showThinking && (
          <div className="rounded-card bg-panel border border-border px-4 py-3 inline-flex">
            <ThinkingIndicator />
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
