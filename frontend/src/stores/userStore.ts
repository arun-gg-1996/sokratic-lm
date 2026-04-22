import { create } from "zustand";

interface UserState {
  studentId: string | null;
  debugMode: boolean;
  setStudentId: (id: string | null) => void;
  setDebugMode: (enabled: boolean) => void;
}

const DEBUG_KEY = "sokratic_debug";
const STUDENT_KEY = "sokratic_student_id";

export const useUserStore = create<UserState>((set) => ({
  studentId: typeof window !== "undefined" ? localStorage.getItem(STUDENT_KEY) : null,
  debugMode: typeof window !== "undefined" ? localStorage.getItem(DEBUG_KEY) === "true" : false,
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
}));
