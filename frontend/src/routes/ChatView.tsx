import { Navigate } from "react-router-dom";
import { ChatSurface } from "../components/chat/ChatView";
import { AppShell } from "../components/layout/AppShell";
import { useUserStore } from "../stores/userStore";

// Note: the old fixed-position chat-window DebugPanel was removed.
// Debug-mode info now renders inline in the sidebar's Debug section
// (see Sidebar.tsx) — locked answer + aliases + full_answer, pacing
// urgency tier + exploration budget, last preflight verdict, and the
// engagement audit counters. Keeps the chat surface clean and puts
// grader-only context next to the existing topic + counter panels.

export function ChatView() {
  const studentId = useUserStore((s) => s.studentId);
  const authToken = useUserStore((s) => s.authToken);

  if (!studentId || !authToken) return <Navigate to="/" replace />;

  return (
    <AppShell>
      <ChatSurface />
    </AppShell>
  );
}
