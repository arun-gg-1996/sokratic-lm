import { useEffect, useRef } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { useTTS } from "../../hooks/useTTS";
import { useSession } from "../../hooks/useSession";
import { ActivityFeed } from "./ActivityFeed";
import { MessageBubble } from "./MessageBubble";
import { SuggestionBubbles } from "./SuggestionBubbles";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { ErrorCard } from "../cards/ErrorCard";

export function MessageList() {
  const messages = useSessionStore((s) => s.messages);
  const isWaiting = useSessionStore((s) => s.isWaitingForTutor);
  // N8 — suggest-answers toggle state + submitMessage so suggestion
  // clicks dispatch as student messages through the same WebSocket
  // path as the composer.
  const suggestEnabled = useSessionStore((s) => s.suggestEnabled);
  const suggestProfile = useSessionStore((s) => s.suggestProfile);
  const threadId = useSessionStore((s) => s.threadId);
  const sessionEnded = useSessionStore((s) => s.sessionEnded);
  const pendingChoice = useSessionStore((s) => s.pendingChoice);
  const { submitMessage } = useSession();
  // L79 — read tutor messages aloud when ttsEnabled. Hook is a no-op
  // when the user has the toggle off or when speechSynthesis is missing
  // (Firefox partial support).
  useTTS();
  // D.6a: live streaming buffer. While the backend streams the
  // teacher's draft, this string grows token-by-token. Render it as a
  // tutor-styled bubble so the user sees the response forming in
  // real time. On message_complete the buffer is cleared and a
  // permanent tutor message is added — keeps the rendered output
  // identical end-state to the non-streaming path.
  const streaming = useSessionStore((s) => s.streamingTutorContent);
  // Per-turn backend activity log (D.6 UX). Each backend stage emits
  // a short label; we render them as a small status feed so the user
  // sees what's happening (Searching textbook → Drafting → Reviewing).
  const activityLog = useSessionStore((s) => s.activityLog);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, isWaiting, streaming.length, activityLog.length]);

  // Thinking indicator: only while waiting AND no other live signal
  // (no streaming tokens, no activity labels yet). Once the activity
  // feed starts emitting backend-stage labels it takes over — this is
  // the desired behavior (the activity feed is more informative).
  const showThinking =
    isWaiting && streaming.length === 0 && activityLog.length === 0;
  // Activity feed: render whenever we have stage labels and aren't
  // already streaming the tutor's bubble. This is the "live" mode of
  // the thinking indicator — shows each backend stage as it fires.
  const showActivity =
    isWaiting && activityLog.length > 0 && streaming.length === 0;
  // L77 — image upload moved to a `+` button on the Composer toolbar
  // (per UX feedback: the big upload card cluttered the rapport view
  // and lingered in the wrong contexts). The Composer button gates on
  // the same conditions: no student turns yet AND no topic locked.

  // N8 — id of the most recent tutor message (used to anchor the
  // SuggestionBubbles fetch + caching). Skip if streaming/waiting,
  // pending choice cards already cover the input, or session ended.
  const lastTutorMessage = [...messages].reverse().find((m) => m.role === "tutor");
  const lastTutorId = lastTutorMessage?.id || "";
  const showSuggestions =
    suggestEnabled
    && !!lastTutorId
    && !isWaiting
    && !pendingChoice
    && !sessionEnded
    && !lastTutorMessage?.shouldStream;  // wait for streaming to finish

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-lane mx-auto px-6 py-8 space-y-4">
        {messages.map((m, idx) => {
          // M-FB — system messages with error_card metadata render as the
          // dedicated ErrorCard component instead of a plain bubble. The
          // backend emits these in lieu of templated tutor fallbacks.
          if (m.role === "system" && m.metadata?.kind === "error_card") {
            return (
              <ErrorCard
                key={m.id}
                component={m.metadata.component || "unknown"}
                errorClass={m.metadata.error_class || "Error"}
                message={m.metadata.message || ""}
              />
            );
          }
          // N8 — render SuggestionBubbles right after the last tutor
          // message (only that one — keeps the LLM-call count to 1
          // per turn).
          const renderSuggestionsAfter =
            showSuggestions && m.id === lastTutorId && m.role === "tutor"
            && idx === messages.length - 1;  // truly the last message overall
          return (
            <div key={m.id}>
              <MessageBubble message={m} />
              {renderSuggestionsAfter && (
                <SuggestionBubbles
                  threadId={threadId}
                  profile={suggestProfile}
                  enabled={true}
                  tutorMessageId={m.id}
                  onPick={(text) => submitMessage(text)}
                />
              )}
            </div>
          );
        })}
        {showActivity && <ActivityFeed labels={activityLog} mode="live" />}
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
