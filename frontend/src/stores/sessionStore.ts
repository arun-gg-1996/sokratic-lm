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
  reset: () =>
    set({
      threadId: null,
      sessionPhase: "rapport",
      messages: [],
      pendingChoice: null,
      isWaitingForTutor: false,
      selectedDebugMessageId: null,
      debug: null,
    }),
}));
