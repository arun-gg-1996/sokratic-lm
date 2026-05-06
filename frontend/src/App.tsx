import { Navigate, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { useTheme } from "./hooks/useTheme";
import { ChatView } from "./routes/ChatView";
import { MasteryView } from "./routes/MasteryView";
import { SessionOverview } from "./routes/SessionOverview";
import { SessionAnalysis } from "./routes/SessionAnalysis";
import { UserPicker } from "./routes/UserPicker";
import { useUserStore } from "./stores/userStore";

function RequireAuth({ children }: { children: ReactNode }) {
  const studentId = useUserStore((s) => s.studentId);
  const authToken = useUserStore((s) => s.authToken);
  if (!studentId || !authToken) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  useTheme();

  return (
    <Routes>
      <Route path="/" element={<UserPicker />} />
      <Route path="/chat" element={<RequireAuth><ChatView /></RequireAuth>} />
      <Route path="/overview" element={<RequireAuth><SessionOverview /></RequireAuth>} />
      <Route path="/mastery" element={<RequireAuth><MasteryView /></RequireAuth>} />
      <Route path="/sessions/:threadId" element={<RequireAuth><SessionAnalysis /></RequireAuth>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
