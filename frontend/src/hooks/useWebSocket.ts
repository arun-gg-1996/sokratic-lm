import { useCallback, useEffect, useRef } from "react";
import { wsUrl } from "../api/websocket";
import { useSessionStore } from "../stores/sessionStore";
import type { ClientMessage, ServerMessage } from "../types";

export function useWebSocket(threadId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<number | null>(null);
  // Guards the auto-reconnect on ws.onclose. When the threadId changes
  // (user clicked "+ New chat"), the React cleanup fires before
  // ws.onclose — so onclose's captured threadId is stale, and its
  // setTimeout(() => connect(), 1200) reconnects to the dead old
  // thread. The disposed flag tells onclose "this hook is gone, do
  // not reconnect." Without it the backend ends up with two active
  // WebSockets per session and student messages route to the old
  // thread that's already in phase=memory_update — observed as
  // "Session complete" appearing immediately on every new chat.
  const disposedRef = useRef<boolean>(false);

  const setPendingChoice = useSessionStore((s) => s.setPendingChoice);
  const setDebug = useSessionStore((s) => s.setDebug);
  const setSessionPhase = useSessionStore((s) => s.setSessionPhase);
  const addTutorMessage = useSessionStore((s) => s.addTutorMessage);
  const setWaiting = useSessionStore((s) => s.setWaiting);
  // D.6a streaming: append per-token deltas + clear when the final
  // message_complete arrives.
  const appendStreamingToken = useSessionStore((s) => s.appendStreamingToken);
  const clearStreamingBuffer = useSessionStore((s) => s.clearStreamingBuffer);
  const appendActivity = useSessionStore((s) => s.appendActivity);
  const clearActivityLog = useSessionStore((s) => s.clearActivityLog);

  const connect = useCallback(() => {
    if (!threadId) return;
    if (disposedRef.current) return;  // hook unmounted / threadId changed; don't reconnect
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(wsUrl(threadId));
    wsRef.current = ws;

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as ServerMessage;
        if (payload.type === "token") {
          // Streaming partial — append to the live buffer. The
          // ChatView renders this buffer as a "live" tutor bubble
          // that grows in real time. Final aggregated content
          // arrives in the subsequent message_complete event.
          if (payload.content) appendStreamingToken(payload.content);
          return;
        }
        if (payload.type === "stream_reset") {
          // Dean's quality check rejected the streamed draft and
          // substituted a revised one. Clear the now-stale buffer so
          // the user sees a clean "thinking..." pause rather than
          // an abrupt content swap when message_complete arrives.
          clearStreamingBuffer();
          return;
        }
        if (payload.type === "activity") {
          // Backend stage label — append to the per-turn activity log.
          // Cleared by the student-submit path (see useSession), so the
          // log only shows what's happening for THIS turn.
          if (payload.content) appendActivity(payload.content);
          return;
        }
        if (payload.type === "message_complete") {
          // Order matters: ADD the permanent tutor message FIRST,
          // then clear the streaming buffer. The streaming bubble
          // and the permanent bubble briefly co-exist for one
          // render frame — visually identical content, so no
          // user-visible duplicate — but neither is ever absent,
          // which kills the "message disappears then reappears"
          // flicker reported on first run.
          //
          // message_complete is authoritative — it carries the
          // canonical final text (which may differ from the
          // streamed partials if the dean's quality check rewrote
          // the draft).
          const content = (payload.content ?? "").trim();
          const debugObj = (payload.debug ?? null) as Record<string, unknown> | null;
          const trace = Array.isArray(debugObj?.turn_trace)
            ? (debugObj?.turn_trace as Array<Record<string, unknown>>)
            : [];
          const turn = typeof debugObj?.turn_count === "number" ? (debugObj.turn_count as number) : undefined;
          const phase = (payload.phase ?? debugObj?.phase ?? "") as string;
          if (content) {
            // Snapshot the activity log onto the new tutor message so
            // it can be re-inspected later via the collapsed
            // "Activity log (N steps)" button on the bubble. Read
            // synchronously so a near-simultaneous next-turn submit
            // doesn't blank our snapshot before it's captured.
            const liveLog = useSessionStore.getState().activityLog;
            addTutorMessage(
              content,
              phase,
              trace,
              turn,
              payload.pending_choice ?? null,
              liveLog,
            );
          } else {
            setPendingChoice(payload.pending_choice ?? null);
          }
          if (phase) setSessionPhase(phase);
          setDebug(debugObj);
          setWaiting(false);
          // Clear AFTER the permanent message is staged.
          clearStreamingBuffer();
          return;
        }
        if (payload.type === "error") {
          clearStreamingBuffer();
          const content = payload.content || "Socket error.";
          addTutorMessage(content, "system");
          setWaiting(false);
        }
      } catch {
        clearStreamingBuffer();
        addTutorMessage("Could not parse server response.", "system");
        setWaiting(false);
      }
    };

    ws.onclose = () => {
      // Identity check: only reconnect if THIS ws is still the active
      // socket. If the threadId changed (user clicked +New Chat), the
      // cleanup function will have replaced wsRef.current with null and
      // a new ws is being opened. We must NOT auto-reconnect because:
      //   - the captured threadId in this closure is stale
      //   - reconnecting creates a zombie ws to the dead old thread
      //   - student messages then route to a session already in
      //     phase=memory_update, producing immediate "Session complete"
      //
      // Using ws-instance identity (not a boolean flag) avoids the
      // race where useEffect re-runs and resets the flag BEFORE the
      // old ws's async onclose has fired.
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      if (disposedRef.current) return;
      if (!threadId) return;
      reconnectRef.current = window.setTimeout(() => connect(), 1200);
    };
  }, [
    addTutorMessage,
    appendActivity,
    appendStreamingToken,
    clearStreamingBuffer,
    setDebug,
    setPendingChoice,
    setSessionPhase,
    setWaiting,
    threadId,
  ]);

  useEffect(() => {
    disposedRef.current = false;  // fresh effect run = active again
    connect();
    return () => {
      disposedRef.current = true;
      if (reconnectRef.current) {
        window.clearTimeout(reconnectRef.current);
        reconnectRef.current = null;
      }
      const old = wsRef.current;
      wsRef.current = null;  // detach BEFORE closing — onclose's identity
                             // check will then bail (wsRef.current !== old)
      if (old) {
        // Belt-and-suspenders: also null out the handlers so the old ws
        // cannot fire ANY React state updates after cleanup. Without
        // this, an onclose firing after a fresh effect run might re-set
        // wsRef.current = null and trigger spurious reconnects.
        old.onclose = null;
        old.onmessage = null;
        old.onerror = null;
        old.close();
      }
    };
  }, [connect]);

  const sendStudentMessage = useCallback(
    (content: string) => {
      const socket = wsRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) return false;
      const payload: ClientMessage = { type: "student_message", content };
      socket.send(JSON.stringify(payload));
      return true;
    },
    [],
  );

  return { sendStudentMessage };
}
