/**
 * MasteryView — `/mastery` route
 * ===============================
 * Three-section dashboard backed by GET /api/mastery/{student_id}:
 *
 *   1. Header  — 3 stat cards (touched / mastered / avg mastery)
 *   2. Sessions list — chronological log of past sessions, each with
 *                      a Revisit button when mastery < 0.5
 *   3. Chapter tree — collapsible, all concepts grouped by chapter
 *
 * The "Revisit" button on a session card navigates to /chat with the
 * subsection name in localStorage so ChatView's session-bootstrap can
 * pick it up and auto-send it as the first student message after
 * rapport. We use localStorage rather than URL params or location
 * state so the value survives the React reset that happens when
 * studentId changes (the bootstrap useEffect re-fires).
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AppShell } from "../components/layout/AppShell";
import { getMastery } from "../api/client";
import { useUserStore } from "../stores/userStore";
import { useSessionStore } from "../stores/sessionStore";
import type {
  MasteryChapterRow,
  MasteryConcept,
  MasteryDashboardResponse,
  MasteryHeader,
  MasterySessionEntry,
} from "../types";

const REVISIT_KEY = "sokratic_revisit_topic";

// "Mastered" requires BOTH a high mastery score AND enough evidence
// (confidence). See memory/mastery_store.py docstring for the rationale
// — extends classical BKT with a coverage signal so a 1-session
// perfect answer doesn't prematurely badge a subsection as mastered.
const MASTERED_THRESHOLD = 0.80;
const CONFIDENCE_THRESHOLD = 0.60;
const WEAK_THRESHOLD = 0.50;

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

function confidenceLabel(c: number | undefined): string {
  const v = c ?? 0;
  if (v >= 0.6) return "high confidence";
  if (v >= 0.3) return "medium confidence";
  return "low confidence";
}

function isMastered(c: { mastery: number; confidence?: number }): boolean {
  return (
    c.mastery >= MASTERED_THRESHOLD &&
    (c.confidence ?? 0) >= CONFIDENCE_THRESHOLD
  );
}

function isWeak(c: { mastery: number }): boolean {
  return c.mastery < WEAK_THRESHOLD;
}

function MasteryBar({
  value,
  className = "",
}: {
  value: number;
  className?: string;
}) {
  const w = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div
      className={`h-1 w-full rounded-full bg-border overflow-hidden ${className}`}
      role="progressbar"
      aria-valuenow={Math.round(w)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full bg-accent transition-[width] duration-300"
        style={{ width: `${w}%` }}
      />
    </div>
  );
}

function HeaderStats({ header }: { header: MasteryHeader }) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <div className="rounded-card border border-border bg-panel px-4 py-3">
        <div className="text-xs text-muted uppercase tracking-wide">Touched</div>
        <div className="mt-1 text-3xl font-semibold">{header.touched}</div>
        <div className="text-xs text-muted">subsections</div>
      </div>
      <div className="rounded-card border border-border bg-panel px-4 py-3">
        <div className="text-xs text-muted uppercase tracking-wide">Mastered</div>
        <div className="mt-1 text-3xl font-semibold">{header.mastered}</div>
        <div className="text-xs text-muted">at 80%+</div>
      </div>
      <div className="rounded-card border border-border bg-panel px-4 py-3">
        <div className="text-xs text-muted uppercase tracking-wide">Avg mastery</div>
        <div className="mt-1 text-3xl font-semibold">{pct(header.avg_mastery)}</div>
        <div className="text-xs text-muted">across touched</div>
      </div>
    </div>
  );
}

function SessionCard({
  session,
  onRevisit,
}: {
  session: MasterySessionEntry;
  onRevisit: (subsectionTitle: string) => void;
}) {
  const reached = session.outcome === "reached";
  const hasMastery = typeof session.mastery === "number";
  const showRevisit = hasMastery && (session.mastery as number) < WEAK_THRESHOLD;
  const masteryLabel = hasMastery ? pct(session.mastery as number) : "—";

  return (
    <div className="rounded-card border border-border bg-panel px-4 py-3 space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-xs text-muted">{session.session_date}</div>
        <div className="text-xs text-muted">
          Ch{session.chapter_num} · {reached ? "Reached" : "Not reached"}
        </div>
      </div>
      <div className="font-medium">
        {session.subsection_title || session.section_title || "Unknown topic"}
      </div>
      {session.summary_text && (
        <div className="text-sm text-muted line-clamp-2">{session.summary_text}</div>
      )}
      {hasMastery && (
        <div className="flex items-center gap-3">
          <MasteryBar value={session.mastery as number} className="flex-1" />
          <div className="text-xs text-muted shrink-0">{masteryLabel}</div>
        </div>
      )}
      {showRevisit && (
        <div className="pt-1">
          <button
            onClick={() =>
              onRevisit(session.subsection_title || session.section_title)
            }
            className="rounded-lg border border-border px-3 py-1.5 text-sm hover:border-accent transition"
          >
            Revisit →
          </button>
        </div>
      )}
    </div>
  );
}

function ChapterRow({
  chapter,
  expanded,
  onToggle,
  onRevisit,
}: {
  chapter: MasteryChapterRow;
  expanded: boolean;
  onToggle: () => void;
  onRevisit: (subsectionTitle: string) => void;
}) {
  return (
    <div className="rounded-card border border-border bg-panel">
      <button
        onClick={onToggle}
        className="w-full px-4 py-3 flex items-center gap-3 hover:bg-bg transition text-left"
        aria-expanded={expanded}
      >
        <span className="text-muted text-sm w-4">{expanded ? "▾" : "▸"}</span>
        <span className="font-medium">
          Ch{chapter.chapter_num} {chapter.chapter_title || ""}
        </span>
        <span className="flex-1" />
        <span className="text-xs text-muted shrink-0">
          {chapter.n_subsections_touched} touched
        </span>
        <div className="w-24 shrink-0">
          <MasteryBar value={chapter.avg_mastery} />
        </div>
        <span className="text-xs text-muted shrink-0 w-10 text-right">
          {pct(chapter.avg_mastery)}
        </span>
      </button>
      {expanded && (
        <div className="border-t border-border divide-y divide-border">
          {chapter.concepts.map((c) => (
            <ConceptRow key={c.path} concept={c} onRevisit={onRevisit} />
          ))}
        </div>
      )}
    </div>
  );
}

function ConceptRow({
  concept,
  onRevisit,
}: {
  concept: MasteryConcept;
  onRevisit: (subsectionTitle: string) => void;
}) {
  const mastered = isMastered(concept);
  const weak = isWeak(concept);
  const dotClass = mastered
    ? "text-accent"
    : weak
      ? "text-red-500"
      : "text-muted";
  return (
    <div className="px-4 py-2 flex items-center gap-3">
      <span className={`shrink-0 ${dotClass}`} aria-hidden>
        ●
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-sm truncate">{concept.subsection_title}</div>
        <div className="text-xs text-muted">
          {concept.sessions} session{concept.sessions === 1 ? "" : "s"} ·
          {" "}{confidenceLabel(concept.confidence)} ·
          last seen {concept.last_seen || "—"}
        </div>
      </div>
      <div className="w-24 shrink-0">
        <MasteryBar value={concept.mastery} />
      </div>
      <div className="text-xs text-muted w-10 text-right shrink-0">
        {pct(concept.mastery)}
      </div>
      {weak && (
        <button
          onClick={() => onRevisit(concept.subsection_title)}
          className="rounded-lg border border-border px-2 py-1 text-xs hover:border-accent transition shrink-0"
          title="Start a session on this topic"
        >
          Revisit
        </button>
      )}
    </div>
  );
}

export function MasteryView() {
  const studentId = useUserStore((s) => s.studentId);
  const resetSession = useSessionStore((s) => s.reset);
  const navigate = useNavigate();
  const [data, setData] = useState<MasteryDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedChapters, setExpandedChapters] = useState<Set<number>>(
    new Set()
  );

  const load = useCallback(async () => {
    if (!studentId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getMastery(studentId);
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [studentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleChapter = (n: number) => {
    setExpandedChapters((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });
  };

  const handleRevisit = (subsectionTitle: string) => {
    if (!subsectionTitle) return;
    // Store the topic; ChatView reads + clears this on bootstrap and
    // dispatches it as the first student message after the tutor's
    // rapport. Using localStorage rather than URL params so the value
    // survives any session reset triggered by studentId change.
    try {
      localStorage.setItem(REVISIT_KEY, subsectionTitle);
    } catch {
      // ignore — user without localStorage just won't get the auto-send
    }
    resetSession();
    navigate("/chat");
  };

  if (!studentId) {
    return (
      <AppShell>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-muted">Pick a user to view mastery.</div>
        </div>
      </AppShell>
    );
  }

  if (loading && !data) {
    return (
      <AppShell>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-muted">Loading mastery…</div>
        </div>
      </AppShell>
    );
  }

  if (error) {
    return (
      <AppShell>
        <div className="flex-1 flex items-center justify-center">
          <div className="rounded-card border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm">
            Error: {error}
          </div>
        </div>
      </AppShell>
    );
  }

  const empty =
    !data ||
    (data.header.touched === 0 &&
      data.chapters.length === 0 &&
      data.sessions.length === 0);

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-lane mx-auto px-6 py-8 space-y-8">
          <div className="flex items-baseline justify-between">
            <h1 className="text-2xl font-semibold">My mastery</h1>
            <button
              onClick={() => void load()}
              className="rounded-lg border border-border px-3 py-1.5 text-sm hover:border-accent transition"
            >
              Refresh
            </button>
          </div>

          {empty ? (
            <div className="rounded-card border border-border bg-panel px-4 py-8 text-center">
              <div className="font-medium">No mastery data yet</div>
              <div className="mt-1 text-sm text-muted">
                Start a chat. After your first session, your topic mastery will
                appear here.
              </div>
            </div>
          ) : (
            <>
              {/* Section 1: Header */}
              <HeaderStats header={data!.header} />

              {/* Section 2: Sessions list */}
              <section className="space-y-3">
                <h2 className="text-base font-semibold">Sessions</h2>
                {data!.sessions.length === 0 ? (
                  <div className="rounded-card border border-border bg-panel px-4 py-3 text-sm text-muted">
                    No session history available.
                  </div>
                ) : (
                  <div className="space-y-3">
                    {data!.sessions.map((s, idx) => (
                      <SessionCard
                        key={`${s.session_date}-${s.subsection_path}-${idx}`}
                        session={s}
                        onRevisit={handleRevisit}
                      />
                    ))}
                  </div>
                )}
              </section>

              {/* Section 3: Chapter tree */}
              <section className="space-y-3">
                <h2 className="text-base font-semibold">All concepts</h2>
                {data!.chapters.length === 0 ? (
                  <div className="rounded-card border border-border bg-panel px-4 py-3 text-sm text-muted">
                    No concepts touched yet.
                  </div>
                ) : (
                  <div className="space-y-2">
                    {data!.chapters.map((ch) => (
                      <ChapterRow
                        key={ch.chapter_num}
                        chapter={ch}
                        expanded={expandedChapters.has(ch.chapter_num)}
                        onToggle={() => toggleChapter(ch.chapter_num)}
                        onRevisit={handleRevisit}
                      />
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </AppShell>
  );
}
