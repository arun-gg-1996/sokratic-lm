import { useState } from "react";
import type { MouseEvent } from "react";
import type { ChatMessage } from "../../types";
import { useSessionStore } from "../../stores/sessionStore";
import { useUserStore } from "../../stores/userStore";
import { ActivityFeed } from "./ActivityFeed";
import { StreamingText } from "./StreamingText";

// Per-bubble click-to-speak. Uses the browser's Web Speech API
// (speechSynthesis) — no server roundtrip. Falls back gracefully if
// the platform doesn't support it.
function SpeakButton({ text }: { text: string }) {
  const [isSpeaking, setIsSpeaking] = useState(false);
  const supported =
    typeof window !== "undefined" && "speechSynthesis" in window;
  if (!supported || !text.trim()) return null;

  const onClick = (e: MouseEvent) => {
    e.stopPropagation();
    const synth = window.speechSynthesis;
    if (isSpeaking) {
      synth.cancel();
      setIsSpeaking(false);
      return;
    }
    synth.cancel(); // stop any other bubble currently speaking
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate = 1.0;
    utt.pitch = 1.0;
    utt.onend = () => setIsSpeaking(false);
    utt.onerror = () => setIsSpeaking(false);
    setIsSpeaking(true);
    synth.speak(utt);
  };

  return (
    <button
      type="button"
      onClick={onClick}
      className="mt-2 inline-flex items-center gap-1 text-xs text-muted hover:text-accent transition"
      title={isSpeaking ? "Stop speaking" : "Read aloud"}
      aria-label={isSpeaking ? "Stop speaking" : "Read aloud"}
    >
      {isSpeaking ? (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4">
          <rect x="6" y="6" width="12" height="12" rx="1.5" />
        </svg>
      ) : (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
          <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
          <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
        </svg>
      )}
      <span>{isSpeaking ? "Stop" : "Listen"}</span>
    </button>
  );
}

export function MessageBubble({ message }: { message: ChatMessage }) {
  const markStreamed = useSessionStore((s) => s.markTutorMessageStreamed);
  const setSelectedDebugMessageId = useSessionStore((s) => s.setSelectedDebugMessageId);
  const selectedDebugMessageId = useSessionStore((s) => s.selectedDebugMessageId);
  const debugMode = useUserStore((s) => s.debugMode);

  if (message.role === "student") {
    return (
      <div className="flex justify-end fade-in">
        <div className="bg-accent-soft text-text rounded-2xl px-4 py-2 max-w-[520px] leading-relaxed">
          {message.content}
        </div>
      </div>
    );
  }

  if (message.role === "system") {
    return (
      <div className="rounded-card border border-border bg-panel px-4 py-3 text-sm text-muted fade-in">
        {message.content}
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3 fade-in">
      <img
        src="/sokratic_bot_icon.png"
        alt="Sokratic Tutor"
        className="h-8 w-8 rounded-md mt-1 shrink-0"
      />
      <div className="flex-1">
        <div
          className={[
            "text-text leading-relaxed rounded-card bg-panel border border-border px-4 py-3",
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
          {message.activityLog && message.activityLog.length > 0 && (
            <ActivityFeed labels={message.activityLog} mode="collapsed" />
          )}
        </div>
        {/* Per-message text-to-speech: rendered as a discreet action
            row OUTSIDE the bubble (below it, left-aligned) so it never
            tangles with the message text. */}
        {!message.shouldStream && (
          <div className="mt-1 ml-1">
            <SpeakButton text={message.content} />
          </div>
        )}
      </div>
    </div>
  );
}
