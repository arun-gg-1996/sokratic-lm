import type { SessionStartResponse, StudentOverviewResponse, User } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export { API_BASE };

export async function listUsers(): Promise<User[]> {
  const res = await fetch(`${API_BASE}/api/users`);
  if (!res.ok) throw new Error("Failed to fetch users");
  return res.json();
}

export async function startSession(studentId: string): Promise<SessionStartResponse> {
  const res = await fetch(`${API_BASE}/api/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ student_id: studentId }),
  });
  if (!res.ok) throw new Error("Failed to start session");
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
