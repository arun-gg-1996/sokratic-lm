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
  prelockedTopic: string | null = null,
  imageContext: Record<string, unknown> | null = null,
): Promise<SessionStartResponse> {
  // D.6b-5: send the user's local hour so rapport's "Good morning/
  // afternoon/evening" comes from their clock, not the server's tz.
  const clientHour = new Date().getHours();
  // Revisit pre-lock: when set, the backend skips topic resolution
  // and pre-fills locked_topic + anchor question from the path. Sent
  // by the /mastery page's Revisit buttons (where we already know
  // exactly which subsection the user wants). Falls back to normal
  // free-text flow when null.
  // L77 imageContext: when set, the backend stashes it on
  // state.image_context AND seeds best_topic_guess/description as the
  // first student message so the v2 topic mapper resolves to the
  // right TOC node automatically.
  const body: Record<string, unknown> = {
    student_id: studentId,
    memory_enabled: memoryEnabled,
    client_hour: clientHour,
  };
  if (prelockedTopic) body.prelocked_topic = prelockedTopic;
  if (imageContext) body.image_context = imageContext;
  const res = await fetch(`${API_BASE}/api/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("Failed to start session");
  return res.json();
}

export interface VlmUploadResponse {
  thread_id: string;
  image_id: string;
  image_path: string;
  identified_structures: Array<{
    name: string;
    location: string;
    confidence: number;
  }>;
  image_type: string;
  description: string;
  best_topic_guess: string;
  confidence: number;
  route_decision: "lock_immediately" | "show_top_matches" | "refuse";
  elapsed_ms: number;
  error: string;
}

export async function uploadVlmImage(
  threadId: string,
  file: File,
): Promise<VlmUploadResponse> {
  const form = new FormData();
  form.append("thread_id", threadId);
  form.append("file", file);
  const res = await fetch(`${API_BASE}/api/vlm/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`VLM upload failed (${res.status}): ${detail.slice(0, 160)}`);
  }
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
  opts: { limit?: number; completedOnly?: boolean; subsectionPath?: string } = {}
): Promise<MasterySessionsResponse> {
  const params = new URLSearchParams();
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.completedOnly) params.set("completed_only", "true");
  if (opts.subsectionPath) params.set("subsection_path", opts.subsectionPath);
  const qs = params.toString() ? `?${params.toString()}` : "";
  const res = await fetch(
    `${API_BASE}/api/mastery/v2/${encodeURIComponent(studentId)}/sessions${qs}`
  );
  if (!res.ok) throw new Error("Failed to fetch mastery sessions");
  return res.json();
}

// M5 — analysis view endpoints

export interface TranscriptMessage {
  role: string;
  content: string;
  phase?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface TranscriptResponse {
  thread_id: string;
  student_id?: string | null;
  messages: TranscriptMessage[];
}

export async function getSessionTranscript(threadId: string): Promise<TranscriptResponse> {
  const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(threadId)}/transcript`);
  if (!res.ok) throw new Error("Failed to fetch transcript");
  return res.json();
}

export interface AnalysisChatResponse {
  thread_id: string;
  reply: string;
  in_scope: boolean;
  cost_estimate_usd: number;
}

export async function postAnalysisChat(
  threadId: string,
  message: string,
  history: { role: string; content: string }[] = []
): Promise<AnalysisChatResponse> {
  const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(threadId)}/analysis_chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, history }),
  });
  if (!res.ok) throw new Error("Failed to post analysis chat");
  return res.json();
}

export interface RegenerateResponse {
  thread_id: string;
  success: boolean;
  key_takeaways?: { demonstrated?: string; needs_work?: string; close_reason?: string } | null;
  error?: string | null;
}

export async function regenerateTakeaways(threadId: string): Promise<RegenerateResponse> {
  const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(threadId)}/regenerate_takeaways`, {
    method: "POST",
  });
  if (!res.ok) throw new Error("Failed to regenerate takeaways");
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
