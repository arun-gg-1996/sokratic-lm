import { useEffect, useRef } from "react";
import { startSession } from "../api/client";
import { useSessionStore } from "../stores/sessionStore";
import { useUserStore } from "../stores/userStore";
import { useWebSocket } from "./useWebSocket";

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
      } catch {
        if (bootstrapSeqRef.current !== seq) return;
        addTutorMessage("Unable to start session. Please retry.", "system");
      } finally {
        if (bootstrapSeqRef.current === seq) bootstrapRef.current = null;
      }
    })();
    bootstrapRef.current = bootstrap;
  }, [addTutorMessage, memoryEnabled, setDebug, setSessionPhase, setThreadId, studentId, threadId]);

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
