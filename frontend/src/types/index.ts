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
  // Optional image preview URL — set on student bubbles created from
  // a VLM upload so the picture renders inline above the caption.
  imageUrl?: string;
  // M-FB — error card metadata. When set on a system message, render
  // ErrorCard component instead of plain text. Backend emits this on
  // any LLM-call failure path in lieu of a templated tutor fallback.
  metadata?: {
    kind?: "error_card";
    component?: string;
    error_class?: string;
    message?: string;
    retry_handler?: string;
  };
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

// --- L29-L34 v2 mastery tree (SQLite-backed, accordion-rendered) ---

export type MasteryColor = "green" | "yellow" | "red" | "grey";

export interface MasterySubsectionNode {
  subsection: string;
  display_label: string;
  path: string;
  score: number | null;
  color: MasteryColor;
  tier: string;
  outcome: string | null;
  last_session_at: string | null;
  attempt_count: number;
}

export interface MasterySectionNode {
  section: string;
  score: number | null;
  color: MasteryColor;
  tier: string;
  touched: number;
  total: number;
  subsections: MasterySubsectionNode[];
}

export interface MasteryChapterNode {
  chapter: string;
  chapter_num: number | null;
  score: number | null;
  color: MasteryColor;
  tier: string;
  touched: number;
  total: number;
  sections: MasterySectionNode[];
}

export interface MasteryTreeResponse {
  student_id: string;
  chapters: MasteryChapterNode[];
}

export interface MasterySessionRow {
  thread_id: string;
  student_id: string;
  started_at: string | null;
  ended_at: string | null;
  locked_topic_path: string | null;
  locked_subsection_path: string | null;
  // M5 — locked Q/A surfaced at session-end so the analysis view
  // doesn't need a second fetch.
  locked_question?: string | null;
  locked_answer?: string | null;
  full_answer?: string | null;
  mastery_tier: string | null;
  core_mastery_tier: string | null;
  clinical_mastery_tier: string | null;
  core_score: number | null;
  clinical_score: number | null;
  status: string;
  turn_count: number | null;
  reach_status: boolean | null;
  // M1 — close-LLM JSON output. Backend uses {demonstrated, needs_work,
  // close_reason}; legacy "what_*" keys retained for existing rows.
  key_takeaways: {
    demonstrated?: string;
    needs_work?: string;
    close_reason?: string;
    regenerated?: boolean;
    what_demonstrated?: string;
    what_needs_work?: string;
  } | null;
}

export interface MasterySessionsResponse {
  student_id: string;
  sessions: MasterySessionRow[];
}
