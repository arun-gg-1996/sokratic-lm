import { useCallback, useEffect, useRef } from "react";
import { wsUrl } from "../api/websocket";
import { useSessionStore } from "../stores/sessionStore";
import type { ClientMessage, ServerMessage } from "../types";

export function useWebSocket(threadId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<number | null>(null);

  const setPendingChoice = useSessionStore((s) => s.setPendingChoice);
  const setDebug = useSessionStore((s) => s.setDebug);
  const setSessionPhase = useSessionStore((s) => s.setSessionPhase);
  const addTutorMessage = useSessionStore((s) => s.addTutorMessage);
  const setWaiting = useSessionStore((s) => s.setWaiting);
  // D.6a streaming: append per-token deltas + clear when the final
  // message_complete arrives.
  const appendStreamingToken = useSessionStore((s) => s.appendStreamingToken);
  const clearStreamingBuffer = useSessionStore((s) => s.clearStreamingBuffer);

  const connect = useCallback(() => {
    if (!threadId) return;
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
        if (payload.type === "message_complete") {
          // Always clear the streaming buffer first — message_complete
          // is authoritative; the buffered partials may have been from
          // a streaming draft that was later revised by the dean's
          // quality check (a rare but real path). Replace, don't merge.
          clearStreamingBuffer();
          const content = (payload.content ?? "").trim();
          const debugObj = (payload.debug ?? null) as Record<string, unknown> | null;
          const trace = Array.isArray(debugObj?.turn_trace)
            ? (debugObj?.turn_trace as Array<Record<string, unknown>>)
            : [];
          const turn = typeof debugObj?.turn_count === "number" ? (debugObj.turn_count as number) : undefined;
          const phase = (payload.phase ?? debugObj?.phase ?? "") as string;
          if (content) {
            addTutorMessage(content, phase, trace, turn, payload.pending_choice ?? null);
          } else {
            setPendingChoice(payload.pending_choice ?? null);
          }
          if (phase) setSessionPhase(phase);
          setDebug(debugObj);
          setWaiting(false);
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
      wsRef.current = null;
      if (!threadId) return;
      reconnectRef.current = window.setTimeout(() => connect(), 1200);
    };
  }, [
    addTutorMessage,
    appendStreamingToken,
    clearStreamingBuffer,
    setDebug,
    setPendingChoice,
    setSessionPhase,
    setWaiting,
    threadId,
  ]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectRef.current) window.clearTimeout(reconnectRef.current);
      wsRef.current?.close();
      wsRef.current = null;
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
