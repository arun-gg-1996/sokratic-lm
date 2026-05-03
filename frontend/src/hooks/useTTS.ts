/**
 * useTTS — L79 accessibility bonus (browser-native TTS).
 *
 * Watches the latest tutor message and speaks it aloud via
 * window.speechSynthesis when the user has enabled the feature
 * (userStore.ttsEnabled). Streaming-text aware — speaks the FINAL
 * text once streaming completes (per-message), not chunk-by-chunk
 * (the chunk approach makes Web Speech API stutter badly when chunk
 * boundaries don't fall on sentence boundaries).
 *
 * Per L79 — zero backend changes, zero new dependencies. Gracefully
 * no-ops on browsers without speechSynthesis (Firefox partial).
 *
 * Stop-on-disable handled in userStore.setTtsEnabled (calls cancel()
 * immediately so the user isn't stuck listening through a long
 * monologue after toggling off).
 */
import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";
import { useSessionStore } from "../stores/sessionStore";
import { useUserStore } from "../stores/userStore";

export function useTTS(): void {
  const ttsEnabled = useUserStore((s) => s.ttsEnabled);
  const messages = useSessionStore((s) => s.messages);
  // Track which message ids we've already spoken so a re-render
  // doesn't replay prior messages.
  const spokenIds = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!ttsEnabled) return;
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;

    const tutorMessages = messages.filter(
      (m: ChatMessage) => m.role === "tutor" && m.content && m.content.trim(),
    );
    if (tutorMessages.length === 0) return;

    const latest = tutorMessages[tutorMessages.length - 1];
    if (spokenIds.current.has(latest.id)) return;
    // Don't speak while the message is still streaming — wait for the
    // final text. shouldStream toggles false once StreamingText calls
    // markTutorMessageStreamed.
    if (latest.shouldStream) return;

    spokenIds.current.add(latest.id);
    const utter = new SpeechSynthesisUtterance(latest.content);
    utter.lang = "en-US";
    utter.rate = 1.0;
    utter.pitch = 1.0;
    // Cancel any in-flight speech before queuing the new one — the
    // student never wants overlapping voices when a follow-up arrives
    // before the previous one finishes.
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utter);
  }, [ttsEnabled, messages]);
}

/** Feature-detect — used by the settings UI to hide the toggle when
 * the API isn't available (graceful degradation per L79). */
export function isTTSAvailable(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}
