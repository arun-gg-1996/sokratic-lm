import { useEffect, useRef } from "react";
import { startSession } from "../api/client";
import { useSessionStore } from "../stores/sessionStore";
import { useUserStore } from "../stores/userStore";
import { useWebSocket } from "./useWebSocket";

// localStorage key used by the /mastery page's "Revisit" buttons. When
// set, the next bootstrapped session auto-sends this value as the
// first student message after the tutor's rapport, then clears the key
// so subsequent sessions don't keep reusing it.
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
    const bootstrap = (async () => {
      try {
        const session = await startSession(studentId, memoryEnabled);
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
        // After the bootstrap places the rapport message, check for a
        // /mastery "Revisit" topic queued in localStorage. We just
        // STAGE it here — a separate effect polls until the websocket
        // is ready and then actually dispatches it via
        // sendStudentMessage. localStorage is cleared in both spots
        // so a retry doesn't double-send.
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

  // Auto-dispatch a queued "Revisit" topic once the websocket is ready.
  // Polls every 200ms up to 5s; gives up silently if the connection
  // never opens (the user can still type the topic themselves).
  useEffect(() => {
    if (!threadId || !pendingRevisitRef.current) return;
    let attempts = 0;
    const maxAttempts = 25; // 25 * 200ms = 5s
    const tick = () => {
      attempts += 1;
      const topic = pendingRevisitRef.current;
      if (!topic) return;
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
    const handle = window.setTimeout(tick, 200);
    return () => window.clearTimeout(handle);
  }, [
    threadId,
    sendStudentMessage,
    addStudentMessage,
    setPendingChoice,
    setWaiting,
  ]);

  const submitMessage = (content: string) => {
    const trimmed = content.trim();
    if (!trimmed) return;
    addStudentMessage(trimmed);
    setPendingChoice(null);
    setWaiting(true);
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

  return { threadId, studentId, submitMessage, restartSession };
}
