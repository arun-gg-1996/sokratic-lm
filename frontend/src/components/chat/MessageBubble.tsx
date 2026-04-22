import type { ChatMessage } from "../../types";
import { useSessionStore } from "../../stores/sessionStore";
import { useUserStore } from "../../stores/userStore";
import { StreamingText } from "./StreamingText";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const markStreamed = useSessionStore((s) => s.markTutorMessageStreamed);
  const setSelectedDebugMessageId = useSessionStore((s) => s.setSelectedDebugMessageId);
  const selectedDebugMessageId = useSessionStore((s) => s.selectedDebugMessageId);
  const debugMode = useUserStore((s) => s.debugMode);

  if (message.role === "student") {
    return (
      <div className="flex justify-end">
        <div className="bg-accent-soft text-text rounded-2xl px-4 py-2 max-w-[520px] leading-relaxed">
          {message.content}
        </div>
      </div>
    );
  }

  if (message.role === "system") {
    return (
      <div className="rounded-card border border-border bg-panel px-4 py-3 text-sm text-muted">
        {message.content}
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <img
        src="/sokratic_bot_icon.png"
        alt="Sokratic Tutor"
        className="h-8 w-8 rounded-md mt-1 shrink-0"
      />
      <div
        className={[
          "text-text leading-relaxed rounded-card bg-panel border border-border px-4 py-3 flex-1",
          debugMode && (message.debugTrace?.length ?? 0) > 0 ? "cursor-pointer hover:border-accent" : "",
          selectedDebugMessageId === message.id ? "border-accent" : "",
        ].join(" ")}
        onClick={() => {
          if (!debugMode || (message.debugTrace?.length ?? 0) === 0) return;
          setSelectedDebugMessageId(selectedDebugMessageId === message.id ? null : message.id);
        }}
        title={debugMode && (message.debugTrace?.length ?? 0) > 0 ? "Click to inspect turn trace" : undefined}
      >
        <StreamingText
          text={message.content}
          enabled={Boolean(message.shouldStream)}
          onComplete={() => {
            if (message.shouldStream) markStreamed(message.id);
          }}
        />
      </div>
    </div>
  );
}
