import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { exportSession } from "../../api/client";
import { downloadJson } from "../../utils/export";
import { useTheme } from "../../hooks/useTheme";
import { isTTSAvailable } from "../../hooks/useTTS";
import { useSessionStore } from "../../stores/sessionStore";
import { useUserStore } from "../../stores/userStore";
import { MemoryDrawer } from "./MemoryDrawer";

export function AccountPopover({ studentId }: { studentId: string | null }) {
  const [open, setOpen] = useState(false);
  const [memoryDrawerOpen, setMemoryDrawerOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const navigate = useNavigate();

  const { theme, toggleTheme } = useTheme();
  const debugMode = useUserStore((s) => s.debugMode);
  const setDebugMode = useUserStore((s) => s.setDebugMode);
  const setStudentId = useUserStore((s) => s.setStudentId);
  const memoryEnabled = useUserStore((s) => s.memoryEnabled);
  const setMemoryEnabled = useUserStore((s) => s.setMemoryEnabled);
  const ttsEnabled = useUserStore((s) => s.ttsEnabled);
  const setTtsEnabled = useUserStore((s) => s.setTtsEnabled);
  const threadId = useSessionStore((s) => s.threadId);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("click", onClick);
    return () => window.removeEventListener("click", onClick);
  }, []);

  const exportNow = async () => {
    if (!threadId || !studentId) return;
    const payload = await exportSession(threadId);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadJson(payload, `sokratic_${studentId}_${stamp}.json`);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full rounded-card border border-border bg-bg px-3 py-2 text-left hover:border-accent transition"
      >
        {studentId ?? "Select user"}
      </button>

      {open && (
        <div className="absolute bottom-12 left-0 right-0 rounded-card border border-border bg-panel shadow-lg p-3 space-y-2 z-30">
          <div className="text-sm font-semibold">{studentId ?? "No user"}</div>
          <button
            className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent"
            onClick={() => {
              setStudentId(null);
              setOpen(false);
              navigate("/");
            }}
          >
            Switch user
          </button>
          <button
            className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent"
            onClick={toggleTheme}
          >
            Theme: {theme}
          </button>
          <button
            className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent"
            onClick={() => setDebugMode(!debugMode)}
          >
            Debug mode: {debugMode ? "on" : "off"}
          </button>
          <button
            className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent"
            onClick={() => setMemoryEnabled(!memoryEnabled)}
            title="When off, the tutor opens each session as if you were a new student. Takes effect on next session."
          >
            Cross-session memory: {memoryEnabled ? "on" : "off"}
          </button>
          {/* L79 — TTS toggle. Hidden when SpeechSynthesis isn't
              available (e.g. some Firefox builds). */}
          {isTTSAvailable() && (
            <button
              className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent"
              onClick={() => setTtsEnabled(!ttsEnabled)}
              title="When on, tutor messages are read aloud via your browser's text-to-speech. Toggle off to silence in-flight speech."
            >
              Read aloud (TTS): {ttsEnabled ? "on" : "off"}
            </button>
          )}
          <button
            disabled={!studentId}
            className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent disabled:opacity-50"
            onClick={() => {
              setOpen(false);
              setMemoryDrawerOpen(true);
            }}
          >
            Manage my memory →
          </button>
          <button
            disabled={!threadId}
            className="w-full rounded-lg border border-border px-3 py-2 text-left hover:border-accent disabled:opacity-50"
            onClick={() => {
              void exportNow();
            }}
          >
            Export current session
          </button>
        </div>
      )}

      {studentId && (
        <MemoryDrawer
          studentId={studentId}
          open={memoryDrawerOpen}
          onClose={() => setMemoryDrawerOpen(false)}
        />
      )}
    </div>
  );
}
