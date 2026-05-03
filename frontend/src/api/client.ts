import type {
  MasteryDashboardResponse,
  MasterySessionRow,
  MasterySessionsResponse,
  MasteryTreeResponse,
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
  memoryEnabled: boolean = true,
  prelockedTopic: string | null = null
): Promise<SessionStartResponse> {
  // D.6b-5: send the user's local hour so rapport's "Good morning/
  // afternoon/evening" comes from their clock, not the server's tz.
  const clientHour = new Date().getHours();
  // Revisit pre-lock: when set, the backend skips topic resolution
  // and pre-fills locked_topic + anchor question from the path. Sent
  // by the /mastery page's Revisit buttons (where we already know
  // exactly which subsection the user wants). Falls back to normal
  // free-text flow when null.
  const body: Record<string, unknown> = {
    student_id: studentId,
    memory_enabled: memoryEnabled,
    client_hour: clientHour,
  };
  if (prelockedTopic) body.prelocked_topic = prelockedTopic;
  const res = await fetch(`${API_BASE}/api/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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

// L29-L34 v2 endpoints (SQLite-backed, accordion tree). Track 5.

export async function getMasteryTree(
  studentId: string
): Promise<MasteryTreeResponse> {
  const res = await fetch(
    `${API_BASE}/api/mastery/v2/${encodeURIComponent(studentId)}/tree`
  );
  if (!res.ok) throw new Error("Failed to fetch mastery tree");
  return res.json();
}

export async function getMasterySessions(
  studentId: string,
  opts: { limit?: number; completedOnly?: boolean } = {}
): Promise<MasterySessionsResponse> {
  const params = new URLSearchParams();
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.completedOnly) params.set("completed_only", "true");
  const qs = params.toString() ? `?${params.toString()}` : "";
  const res = await fetch(
    `${API_BASE}/api/mastery/v2/${encodeURIComponent(studentId)}/sessions${qs}`
  );
  if (!res.ok) throw new Error("Failed to fetch mastery sessions");
  return res.json();
}

export async function getMasterySession(
  threadId: string
): Promise<MasterySessionRow> {
  const res = await fetch(
    `${API_BASE}/api/mastery/v2/session/${encodeURIComponent(threadId)}`
  );
  if (!res.ok) throw new Error("Failed to fetch mastery session");
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
