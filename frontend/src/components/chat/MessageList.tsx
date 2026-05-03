import { useEffect, useRef } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { useTTS } from "../../hooks/useTTS";
import { ActivityFeed } from "./ActivityFeed";
import { ImageUploadCard } from "./ImageUploadCard";
import { MessageBubble } from "./MessageBubble";
import { ThinkingIndicator } from "./ThinkingIndicator";

export function MessageList() {
  const messages = useSessionStore((s) => s.messages);
  const isWaiting = useSessionStore((s) => s.isWaitingForTutor);
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

  // The "thinking…" indicator only shows while we're waiting AND
  // there's no other live signal (no streaming tokens yet, no activity
  // labels yet). Once either lands, the spinner disappears so the user
  // isn't seeing two indicators at once.
  const showThinking =
    isWaiting && streaming.length === 0 && activityLog.length === 0;
  // The activity feed lingers while waiting; once the streaming
  // bubble appears or the final message lands, the activity feed
  // can fade out (we hide it once streaming has any content).
  const showActivity =
    isWaiting && activityLog.length > 0 && streaming.length === 0;
  // L77 — image upload affordance. Only shown on a fresh chat
  // (rapport phase: at most the rapport opener has been rendered, no
  // student messages yet). Hidden as soon as the student types or
  // picks a topic card.
  const showImageUpload = (
    !isWaiting
    && messages.filter((m) => m.role === "student").length === 0
  );

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-lane mx-auto px-6 py-8 space-y-4">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {showImageUpload && <ImageUploadCard />}
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
