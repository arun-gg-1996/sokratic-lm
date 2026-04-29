import { create } from "zustand";
import type { ChatMessage, PendingChoice } from "../types";

function messageId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

interface SessionState {
  threadId: string | null;
  sessionPhase: string;
  messages: ChatMessage[];
  pendingChoice: PendingChoice | null;
  isWaitingForTutor: boolean;
  selectedDebugMessageId: string | null;
  debug: Record<string, unknown> | null;
  // D.6a: while the backend streams the teacher's draft, partial
  // tokens accumulate here. ChatView renders this as a live tutor
  // bubble that grows token-by-token. On message_complete the
  // backend sends the final aggregated text — we replace this
  // buffer with a permanent tutor message and clear the buffer.
  streamingTutorContent: string;
  // Per-turn activity log. Each backend stage (retrieval, classifier,
  // draft, QC, etc.) appends a short label here. ChatView renders
  // these as a live status feed below the user's last message —
  // similar to Claude Code's tool-call display. Cleared at the start
  // of each new student turn so labels from a prior turn don't
  // linger.
  activityLog: string[];
  setThreadId: (id: string) => void;
  setSessionPhase: (phase: string) => void;
  addStudentMessage: (content: string) => void;
  addTutorMessage: (
    content: string,
    phase?: string,
    debugTrace?: Array<Record<string, unknown>>,
    debugTurn?: number,
    pendingChoiceAfterStream?: PendingChoice | null
  ) => void;
  addSystemMessage: (content: string) => void;
  markTutorMessageStreamed: (id: string) => void;
  setPendingChoice: (c: PendingChoice | null) => void;
  setWaiting: (w: boolean) => void;
  setSelectedDebugMessageId: (id: string | null) => void;
  setDebug: (d: Record<string, unknown> | null) => void;
  appendStreamingToken: (delta: string) => void;
  clearStreamingBuffer: () => void;
  appendActivity: (label: string) => void;
  clearActivityLog: () => void;
  reset: () => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  threadId: null,
  sessionPhase: "rapport",
  messages: [],
  pendingChoice: null,
  isWaitingForTutor: false,
  selectedDebugMessageId: null,
  debug: null,
  streamingTutorContent: "",
  activityLog: [],
  setThreadId: (id) => set({ threadId: id }),
  setSessionPhase: (phase) => set({ sessionPhase: phase || "tutoring" }),
  addStudentMessage: (content) =>
    set((s) => ({
      messages: [...s.messages, { id: messageId("student"), role: "student", content }],
    })),
  addTutorMessage: (content, phase, debugTrace, debugTurn, pendingChoiceAfterStream) =>
    set((s) => {
      const last = s.messages[s.messages.length - 1];
      if (last && last.role === "tutor" && last.content.trim() === content.trim()) {
        return {};
      }
      return {
        messages: [
          ...s.messages,
          {
            id: messageId("tutor"),
            role: "tutor",
            content,
            phase,
            shouldStream: true,
            pendingChoiceAfterStream: pendingChoiceAfterStream ?? null,
            debugTrace: debugTrace ?? [],
            debugTurn,
          },
        ],
      };
    }),
  addSystemMessage: (content) =>
    set((s) => ({
      messages: [...s.messages, { id: messageId("system"), role: "system", content }],
    })),
  markTutorMessageStreamed: (id) =>
    set((s) => {
      let queuedChoice: PendingChoice | null | undefined;
      const nextMessages = s.messages.map((m) => {
        if (m.id !== id) return m;
        queuedChoice = m.pendingChoiceAfterStream ?? null;
        return { ...m, shouldStream: false, pendingChoiceAfterStream: undefined };
      });
      return {
        messages: nextMessages,
        pendingChoice: queuedChoice !== undefined ? queuedChoice : s.pendingChoice,
      };
    }),
  setPendingChoice: (c) => set({ pendingChoice: c }),
  setWaiting: (w) => set({ isWaitingForTutor: w }),
  setSelectedDebugMessageId: (id) => set({ selectedDebugMessageId: id }),
  setDebug: (d) => set({ debug: d }),
  appendStreamingToken: (delta) =>
    set((s) => ({ streamingTutorContent: s.streamingTutorContent + delta })),
  clearStreamingBuffer: () => set({ streamingTutorContent: "" }),
  appendActivity: (label) =>
    set((s) => ({ activityLog: [...s.activityLog, label] })),
  clearActivityLog: () => set({ activityLog: [] }),
  reset: () =>
    set({
      threadId: null,
      sessionPhase: "rapport",
      messages: [],
      pendingChoice: null,
      isWaitingForTutor: false,
      selectedDebugMessageId: null,
      debug: null,
      streamingTutorContent: "",
      activityLog: [],
    }),
}));
