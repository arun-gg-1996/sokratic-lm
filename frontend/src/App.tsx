import { Navigate, Route, Routes } from "react-router-dom";
import { useTheme } from "./hooks/useTheme";
import { ChatView } from "./routes/ChatView";
import { MasteryView } from "./routes/MasteryView";
import { SessionOverview } from "./routes/SessionOverview";
import { SessionAnalysis } from "./routes/SessionAnalysis";
import { UserPicker } from "./routes/UserPicker";

export default function App() {
  useTheme();

  return (
    <Routes>
      <Route path="/" element={<UserPicker />} />
      <Route path="/chat" element={<ChatView />} />
      <Route path="/overview" element={<SessionOverview />} />
      <Route path="/mastery" element={<MasteryView />} />
      <Route path="/sessions/:threadId" element={<SessionAnalysis />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
