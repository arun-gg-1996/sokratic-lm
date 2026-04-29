export type MessageRole = "student" | "tutor" | "system";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  phase?: string;
  shouldStream?: boolean;
  pendingChoiceAfterStream?: PendingChoice | null;
  debugTrace?: Array<Record<string, unknown>>;
  debugTurn?: number;
}

export interface PendingChoice {
  kind: "opt_in" | "topic";
  options: string[];
}

export interface ServerMessage {
  type: "message_complete" | "error";
  content?: string;
  pending_choice?: PendingChoice | null;
  topic_confirmed?: boolean;
  phase?: string;
  debug?: Record<string, unknown>;
}

export interface ClientMessage {
  type: "student_message";
  content: string;
}

export interface User {
  id: string;
  display_name: string;
}

export interface SessionStartResponse {
  thread_id: string;
  initial_message: string;
  initial_debug?: Record<string, unknown> | null;
}

export interface StudentOverviewResponse {
  student_id: string;
  weak_topics: Array<Record<string, unknown>>;
  strong_topics: Array<Record<string, unknown>>;
}

export interface MemoryEntry {
  id: string | null;
  text: string;
  created_at: string | null;
  score: number | null;
}

export interface MemoryListResponse {
  student_id: string;
  available: boolean;
  count: number;
  entries: MemoryEntry[];
}

export interface MemoryDeleteResponse {
  student_id: string;
  deleted: number;
  available: boolean;
}
