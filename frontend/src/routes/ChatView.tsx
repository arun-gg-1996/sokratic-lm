import { Navigate } from "react-router-dom";
import { ChatSurface } from "../components/chat/ChatView";
import { DebugPanel } from "../components/debug/DebugPanel";
import { AppShell } from "../components/layout/AppShell";
import { useUserStore } from "../stores/userStore";

export function ChatView() {
  const studentId = useUserStore((s) => s.studentId);

  if (!studentId) return <Navigate to="/" replace />;

  return (
    <AppShell>
      <ChatSurface />
      <DebugPanel />
    </AppShell>
  );
}
