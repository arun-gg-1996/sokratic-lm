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
  // Snapshot of the activity log captured when this tutor message
  // was finalized. Lets us show a collapsed "Activity log (N steps)"
  // affordance below the message for users who want to inspect what
  // the system did during that turn.
  activityLog?: string[];
}

export interface PendingChoice {
  kind: "opt_in" | "topic" | "confirm_topic";
  options: string[];
  allow_custom?: boolean;
  end_session_label?: string;
  end_session_value?: string;
}

export interface ServerMessage {
  // "token" is incremental streaming output (D.6a). Emitted multiple
  // times during a tutoring turn while the teacher draft streams; the
  // ChatView appends each delta to the currently-streaming tutor
  // message. Followed by a single "message_complete" with the full
  // aggregated content + final state — that event finalizes the
  // streaming buffer and refreshes pending_choice / debug.
  //
  // "stream_reset" is fired when the dean's quality check rejects the
  // streamed draft and substitutes a revised one. The frontend
  // clears the streaming buffer immediately so the user sees a clean
  // "thinking..." pause rather than content X being abruptly replaced
  // by content Y when message_complete arrives.
  // "activity" carries short user-facing labels for backend stages
  // (e.g. "Searching textbook", "Reviewing draft for accuracy"). The
  // frontend appends each to a per-turn activity log so the user sees
  // a Claude-Code-style live status feed instead of an opaque spinner.
  type: "token" | "stream_reset" | "activity" | "message_complete" | "error";
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
  // Set ONLY for pre-locked (revisit) sessions — the deterministic
  // "Got it — let's work on X. Question?" message the backend builds
  // inline. Frontend renders it as a second tutor turn right after
  // the rapport greeting. Null/undefined for free-text sessions
  // where the dean ack-emits during the normal turn loop instead.
  initial_topic_ack?: string | null;
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
  // Structured metadata mem0 stored in the Qdrant payload. The drawer
  // groups entries by metadata.session_date + metadata.topic_path.
  // Empty object for legacy pre-metadata entries.
  metadata?: Record<string, unknown>;
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

// --- Mastery dashboard (D.3) ---

export interface MasteryHeader {
  touched: number;
  mastered: number;
  avg_mastery: number;
  avg_confidence?: number;
}

export interface MasteryConcept {
  path: string;
  chapter_num: number;
  chapter_title: string;
  section_title: string;
  subsection_title: string;
  mastery: number;
  confidence?: number;
  sessions: number;
  last_seen: string;
  last_outcome: string;
  last_rationale?: string;
}

export interface MasteryChapterRow {
  chapter_num: number;
  chapter_title: string;
  avg_mastery: number;
  n_subsections_touched: number;
  concepts: MasteryConcept[];
}

export interface MasterySessionEntry {
  session_date: string;
  chapter_num: number;
  chapter_title: string;
  section_title: string;
  subsection_title: string;
  subsection_path: string;
  outcome: string;
  mastery: number | null;
  summary_text: string;
}

export interface MasteryDashboardResponse {
  student_id: string;
  available: boolean;
  header: MasteryHeader;
  chapters: MasteryChapterRow[];
  sessions: MasterySessionEntry[];
}
