import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { exportSession } from "../../api/client";
import { downloadJson } from "../../utils/export";
import { useTheme } from "../../hooks/useTheme";
import { useSessionStore } from "../../stores/sessionStore";
import { useUserStore } from "../../stores/userStore";

export function AccountPopover({ studentId }: { studentId: string | null }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const navigate = useNavigate();

  const { theme, toggleTheme } = useTheme();
  const debugMode = useUserStore((s) => s.debugMode);
  const setDebugMode = useUserStore((s) => s.setDebugMode);
  const setStudentId = useUserStore((s) => s.setStudentId);
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
    </div>
  );
}
