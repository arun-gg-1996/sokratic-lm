import { useEffect, useRef } from "react";
import { startSession } from "../api/client";
import { useSessionStore } from "../stores/sessionStore";
import { useUserStore } from "../stores/userStore";
import { useWebSocket } from "./useWebSocket";

// localStorage keys used by the /mastery page's "Revisit" buttons.
//
// REVISIT_TOPIC_PATH (preferred): the canonical "ChN|sec|sub" path.
//   When set, startSession sends it as `prelocked_topic` and the
//   backend pre-fills locked_topic + anchor question, skipping the
//   dean's free-text topic resolution. We also auto-send a brief
//   "Let's continue" message so the dean fires the first hint
//   without the user having to type anything.
//
// REVISIT_KEY (legacy / fallback): just the subsection title text.
//   Used when the click happens before path metadata is present
//   (e.g. older session cards). The bootstrap sends this as the
//   first student message and the dean resolves topic the usual
//   way — historically prone to mis-locking on short queries.
const REVISIT_TOPIC_PATH = "sokratic_revisit_topic_path";
// L77 — image-initiated session. /api/vlm/upload result stashed here
// before the bootstrap fires; useSession reads + clears it then passes
// the VLM JSON to startSession, which seeds state.image_context AND
// auto-routes the description through the v2 topic mapper for an
// image-driven first turn.
const IMAGE_CONTEXT_KEY = "sokratic_pending_image_context";
const REVISIT_KEY = "sokratic_revisit_topic";

export function useSession() {
  const studentId = useUserStore((s) => s.studentId);
  // Read once at session-start time. If the user toggles memory mid-session,
  // it takes effect on the NEXT session (a restart re-runs this effect).
  const memoryEnabled = useUserStore((s) => s.memoryEnabled);

  const threadId = useSessionStore((s) => s.threadId);
  const setThreadId = useSessionStore((s) => s.setThreadId);
  const addTutorMessage = useSessionStore((s) => s.addTutorMessage);
  const addStudentMessage = useSessionStore((s) => s.addStudentMessage);
  const setDebug = useSessionStore((s) => s.setDebug);
  const setSessionPhase = useSessionStore((s) => s.setSessionPhase);
  const setPendingChoice = useSessionStore((s) => s.setPendingChoice);
  const setWaiting = useSessionStore((s) => s.setWaiting);
  const reset = useSessionStore((s) => s.reset);

  const bootstrapRef = useRef<Promise<void> | null>(null);
  const bootstrapSeqRef = useRef(0);
  // Holds a pending /mastery-page "Revisit" topic. Set during bootstrap
  // when localStorage has REVISIT_KEY; consumed by a polling effect once
  // the websocket is ready.
  const pendingRevisitRef = useRef<string | null>(null);
  const { sendStudentMessage } = useWebSocket(threadId);

  useEffect(() => {
    bootstrapSeqRef.current += 1;
    bootstrapRef.current = null;
    useSessionStore.getState().reset();
  }, [studentId]);

  useEffect(() => {
    if (!studentId || threadId || bootstrapRef.current) return;

    const seq = bootstrapSeqRef.current;
    // Read the prelocked-topic path BEFORE startSession so we can pass
    // it through. Clear after read so a subsequent fresh-chat doesn't
    // inherit it. The lookup is wrapped in try/catch — localStorage
    // is unavailable in some private-browsing modes, fail-soft.
    let prelockedPath: string | null = null;
    try {
      prelockedPath = localStorage.getItem(REVISIT_TOPIC_PATH);
      if (prelockedPath) localStorage.removeItem(REVISIT_TOPIC_PATH);
    } catch {
      prelockedPath = null;
    }
    // L77 — pending image context from a prior /api/vlm/upload call.
    // Same pattern as the prelocked-topic path: read, clear, then pass
    // through. Cleared on read so a subsequent fresh-chat doesn't
    // inherit a stale upload.
    let imageContext: Record<string, unknown> | null = null;
    try {
      const raw = localStorage.getItem(IMAGE_CONTEXT_KEY);
      if (raw) {
        localStorage.removeItem(IMAGE_CONTEXT_KEY);
        try {
          imageContext = JSON.parse(raw);
        } catch {
          imageContext = null;
        }
      }
    } catch {
      imageContext = null;
    }
    const bootstrap = (async () => {
      try {
        const session = await startSession(
          studentId, memoryEnabled, prelockedPath, imageContext,
        );
        // Ignore stale bootstrap responses (prevents duplicate greetings/threads).
        if (bootstrapSeqRef.current !== seq) return;
        if (useSessionStore.getState().threadId) return;
        setThreadId(session.thread_id);
        const initialDebug = (session.initial_debug ?? null) as Record<string, unknown> | null;
        setDebug(initialDebug);
        const initialPhase = (initialDebug?.phase as string | undefined) ?? "tutoring";
        setSessionPhase(initialPhase);
        const initialTrace = Array.isArray(initialDebug?.turn_trace)
          ? (initialDebug?.turn_trace as Array<Record<string, unknown>>)
          : [];
        if (session.initial_message) addTutorMessage(session.initial_message, "rapport", initialTrace, 0);
        // My Mastery → Start flow (refactored 2026-05-03):
        // Don't pre-bake a topic_ack on the backend. Instead, queue
        // the subsection name as a student auto-send, dispatched
        // after rapport has rendered (the polling effect below uses
        // a 1500ms initial delay). Dean will then resolve the topic
        // via the normal lock flow.
        try {
          const revisit = localStorage.getItem(REVISIT_KEY);
          if (revisit && revisit.trim()) {
            pendingRevisitRef.current = revisit.trim();
            localStorage.removeItem(REVISIT_KEY);
          }
        } catch {
          // localStorage unavailable — ignore, user just won't get auto-send
        }
      } catch {
        if (bootstrapSeqRef.current !== seq) return;
        addTutorMessage("Unable to start session. Please retry.", "system");
      } finally {
        if (bootstrapSeqRef.current === seq) bootstrapRef.current = null;
      }
    })();
    bootstrapRef.current = bootstrap;
  }, [addTutorMessage, memoryEnabled, setDebug, setSessionPhase, setThreadId, studentId, threadId]);

  // Auto-dispatch a queued "Revisit" topic AFTER the rapport message
  // has finished streaming. We poll the messages array — the rapport
  // tutor message starts with shouldStream=true and flips to false
  // once the typewriter animation completes (markTutorMessageStreamed).
  // Only then do we fire the auto-injected student message.
  // This prevents the visual race where the student bubble appears
  // mid-stream while the rapport text is still typing.
  useEffect(() => {
    if (!threadId || !pendingRevisitRef.current) return;
    let attempts = 0;
    const maxAttempts = 50; // 50 * 200ms = 10s safety cap
    const tick = () => {
      attempts += 1;
      const topic = pendingRevisitRef.current;
      if (!topic) return;
      // Look at the latest tutor message — if it's still streaming,
      // wait for it to finish before injecting the student message.
      const msgs = useSessionStore.getState().messages;
      let latestTutor = null;
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === "tutor") { latestTutor = msgs[i]; break; }
      }
      const stillStreaming = Boolean(latestTutor?.shouldStream);
      if (stillStreaming) {
        if (attempts < maxAttempts) window.setTimeout(tick, 200);
        else pendingRevisitRef.current = null;
        return;
      }
      const sent = sendStudentMessage(topic);
      if (sent) {
        addStudentMessage(topic);
        setPendingChoice(null);
        setWaiting(true);
        pendingRevisitRef.current = null;
        return;
      }
      if (attempts < maxAttempts) {
        window.setTimeout(tick, 200);
      } else {
        // Gave up — clear so the next session doesn't inherit it.
        pendingRevisitRef.current = null;
      }
    };
    // Small initial delay for the rapport message to be added to the
    // store. After that, the streaming-completion check above gates
    // the actual dispatch.
    const handle = window.setTimeout(tick, 400);
    return () => window.clearTimeout(handle);
  }, [
    threadId,
    sendStudentMessage,
    addStudentMessage,
    setPendingChoice,
    setWaiting,
  ]);

  const submitMessage = (content: string, imageUrl?: string) => {
    const trimmed = content.trim();
    if (!trimmed) return;
    // imageUrl is set by the VLM upload path — the student bubble then
    // renders an image preview above the caption text instead of a
    // text-only bubble.
    addStudentMessage(trimmed, imageUrl);
    setPendingChoice(null);
    setWaiting(true);
    // Clear the previous turn's activity log so the new turn starts
    // with an empty status feed. Activity events from the new turn
    // will populate it as the backend progresses through stages.
    useSessionStore.getState().clearActivityLog();
    const sent = sendStudentMessage(trimmed);
    if (!sent) {
      addTutorMessage("Connection not ready. Please retry.", "system");
      setWaiting(false);
    }
  };

  const restartSession = () => {
    bootstrapSeqRef.current += 1;
    bootstrapRef.current = null;
    reset();
  };

  // M1 — explicit-exit (button click OR confirming the deflection modal).
  // Sends a special "__exit_session__" sentinel message; backend interprets
  // it via state and routes to memory_update with close_reason=exit_intent.
  // Per M1 spec: no save, just goodbye + END.
  const requestExitSession = () => {
    setWaiting(true);
    useSessionStore.getState().clearActivityLog();
    const sent = sendStudentMessage("__exit_session__");
    if (!sent) {
      addTutorMessage("Connection not ready. Please retry.", "system");
      setWaiting(false);
    }
  };

  // M1 — clears the exit_intent_pending flag without ending the session.
  // Frontend-only: doesn't talk to backend (backend will clear on next turn).
  const cancelExitIntent = () => {
    useSessionStore.getState().setExitIntentPending(false);
  };

  return { threadId, studentId, submitMessage, restartSession, requestExitSession, cancelExitIntent };
}
