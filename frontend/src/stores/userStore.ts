import { create } from "zustand";

interface UserState {
  studentId: string | null;
  debugMode: boolean;
  // Cross-session memory toggle. When false, the next session_start
  // request sends memory_enabled=false and the tutor opens with a
  // fresh greeting regardless of any prior history. Persisted to
  // localStorage so the choice survives reloads. Defaults to true.
  memoryEnabled: boolean;
  // L79 — read tutor messages aloud via Web Speech API SpeechSynthesis.
  // Default false (opt-in). Persisted to localStorage so the choice
  // survives reloads. Browser support: Chrome / Edge / Safari full,
  // Firefox partial; consumer hides the toggle when API unavailable.
  ttsEnabled: boolean;
  setStudentId: (id: string | null) => void;
  setDebugMode: (enabled: boolean) => void;
  setMemoryEnabled: (enabled: boolean) => void;
  setTtsEnabled: (enabled: boolean) => void;
}

const DEBUG_KEY = "sokratic_debug";
const STUDENT_KEY = "sokratic_student_id";
const MEMORY_KEY = "sokratic_memory_enabled";
const TTS_KEY = "sokratic_tts_enabled";

function readMemoryEnabled(): boolean {
  if (typeof window === "undefined") return true;
  // Default true if the key was never written.
  const v = localStorage.getItem(MEMORY_KEY);
  return v === null ? true : v === "true";
}

function readTtsEnabled(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem(TTS_KEY) === "true";
}

export const useUserStore = create<UserState>((set) => ({
  studentId: typeof window !== "undefined" ? localStorage.getItem(STUDENT_KEY) : null,
  debugMode: typeof window !== "undefined" ? localStorage.getItem(DEBUG_KEY) === "true" : false,
  memoryEnabled: readMemoryEnabled(),
  ttsEnabled: readTtsEnabled(),
  setStudentId: (id) => {
    if (typeof window !== "undefined") {
      if (id) localStorage.setItem(STUDENT_KEY, id);
      else localStorage.removeItem(STUDENT_KEY);
    }
    set({ studentId: id });
  },
  setDebugMode: (enabled) => {
    if (typeof window !== "undefined") {
      localStorage.setItem(DEBUG_KEY, String(enabled));
    }
    set({ debugMode: enabled });
  },
  setMemoryEnabled: (enabled) => {
    if (typeof window !== "undefined") {
      localStorage.setItem(MEMORY_KEY, String(enabled));
    }
    set({ memoryEnabled: enabled });
  },
  setTtsEnabled: (enabled) => {
    if (typeof window !== "undefined") {
      localStorage.setItem(TTS_KEY, String(enabled));
      // When the user disables TTS, immediately stop any in-flight speech
      // so they're not stuck listening to a long monologue.
      if (!enabled && typeof window.speechSynthesis !== "undefined") {
        window.speechSynthesis.cancel();
      }
    }
    set({ ttsEnabled: enabled });
  },
}));
