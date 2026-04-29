import type {
  MasteryDashboardResponse,
  MemoryDeleteResponse,
  MemoryListResponse,
  SessionStartResponse,
  StudentOverviewResponse,
  User,
} from "../types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export { API_BASE };

export async function listUsers(): Promise<User[]> {
  const res = await fetch(`${API_BASE}/api/users`);
  if (!res.ok) throw new Error("Failed to fetch users");
  return res.json();
}

export async function startSession(
  studentId: string,
  memoryEnabled: boolean = true
): Promise<SessionStartResponse> {
  // D.6b-5: send the user's local hour so rapport's "Good morning/
  // afternoon/evening" comes from their clock, not the server's tz.
  // Cheap to compute, harmless if backend ignores it (older deploys).
  const clientHour = new Date().getHours();
  const res = await fetch(`${API_BASE}/api/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      student_id: studentId,
      memory_enabled: memoryEnabled,
      client_hour: clientHour,
    }),
  });
  if (!res.ok) throw new Error("Failed to start session");
  return res.json();
}

export async function getMemory(studentId: string): Promise<MemoryListResponse> {
  const res = await fetch(
    `${API_BASE}/api/memory/${encodeURIComponent(studentId)}`
  );
  if (!res.ok) throw new Error("Failed to fetch memory");
  return res.json();
}

export async function forgetMemory(studentId: string): Promise<MemoryDeleteResponse> {
  const res = await fetch(
    `${API_BASE}/api/memory/${encodeURIComponent(studentId)}`,
    { method: "DELETE" }
  );
  if (!res.ok) throw new Error("Failed to delete memory");
  return res.json();
}

export async function getMastery(
  studentId: string
): Promise<MasteryDashboardResponse> {
  const res = await fetch(
    `${API_BASE}/api/mastery/${encodeURIComponent(studentId)}`
  );
  if (!res.ok) throw new Error("Failed to fetch mastery");
  return res.json();
}

export async function exportSession(threadId: string): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_BASE}/api/session/${threadId}/export`);
  if (!res.ok) throw new Error("Failed to export session");
  return res.json();
}

export async function getStudentOverview(studentId: string): Promise<StudentOverviewResponse> {
  const res = await fetch(`${API_BASE}/api/students/${studentId}/overview`);
  if (!res.ok) throw new Error("Failed to fetch overview");
  return res.json();
}
