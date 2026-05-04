/**
 * MasteryView — `/mastery` route (Track 5 — L29-L34)
 * ===================================================
 * SQLite-backed accordion tree per the v2 mastery design.
 *
 * Uses GET /api/mastery/v2/{student_id}/tree which returns the FULL
 * TOC tree with mastery overlay (touched + untouched). Untouched
 * nodes report score=null, color="grey" so the tree renders as a
 * visual heat map of student progress over the corpus.
 *
 * Layout per L29:
 *   - In-place accordion at chapter / section / subsection level
 *   - Multiple chapters can be open at once
 *   - Color rendering via node.color (green/yellow/red/grey dot)
 *
 * Subsection row per L30:
 *   - Color dot
 *   - display_label (LLM-rewritten friendly name from L19)
 *   - Numeric EWMA score
 *   - Last session date
 *   - Action button: "Start" (attempt_count == 0) or "Revisit"
 *     (attempt_count > 0). "Revisit" wires through localStorage to
 *     ChatView's session bootstrap so /api/session/start sends the
 *     locked subsection path as `prelocked_topic` (skipping the
 *     dean's free-text resolution).
 *
 * UI per L34:
 *   - Single "Sort by mastery" toggle (lowest-mastery-first when on)
 *   - No search, no filter chips, no breadcrumbs — tree IS the nav
 *   - Empty state = full tree, all greys (informative + inviting)
 *
 * Replaces the legacy view that consumed /api/mastery/{student_id}.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AppShell } from "../components/layout/AppShell";
import { getMasterySessions, getMasteryTree } from "../api/client";
import { useUserStore } from "../stores/userStore";
import { useSessionStore } from "../stores/sessionStore";
import type {
  MasteryChapterNode,
  MasteryColor,
  MasterySectionNode,
  MasterySessionRow,
  MasterySubsectionNode,
  MasteryTreeResponse,
} from "../types";

// localStorage keys consumed by ChatView's session-bootstrap. The path
// version is preferred — when present, /api/session/start receives
// `prelocked_topic` and the dean skips topic resolution entirely.
const REVISIT_TOPIC_PATH = "sokratic_revisit_topic_path";
const REVISIT_KEY = "sokratic_revisit_topic";

// ─────────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ─────────────────────────────────────────────────────────────────────────────

function pct(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "—";
  return `${Math.round(score * 100)}%`;
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  // Trim to YYYY-MM-DD if we got a full ISO timestamp.
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(iso);
  return m ? m[1] : iso;
}

const DOT_CLASS: Record<MasteryColor, string> = {
  green: "text-emerald-500",
  yellow: "text-amber-500",
  red: "text-red-500",
  grey: "text-muted",
};

// L80.g — color threshold tooltip on hover; same text everywhere so
// students can learn the legend just by hovering one row.
const COLOR_LEGEND =
  "Green ≥ 75% mastery · Yellow 50–75% · Red < 50% · Grey untouched";

const BORDER_CLASS: Record<MasteryColor, string> = {
  green: "border-l-2 border-l-emerald-500",
  yellow: "border-l-2 border-l-amber-500",
  red: "border-l-2 border-l-red-500",
  grey: "border-l-2 border-l-border",
};

// Treat anything past the score threshold as effectively "for sort".
// Untouched (null) sorts to the bottom when sort-by-mastery is on,
// so weak (red) nodes float to the top — matches L34's intent.
function sortKey(score: number | null | undefined): number {
  if (score == null) return 1.5; // past 1.0 → after green when ascending
  return score;
}

// ─────────────────────────────────────────────────────────────────────────────
// Atomic UI bits
// ─────────────────────────────────────────────────────────────────────────────

function Dot({ color }: { color: MasteryColor }) {
  return (
    <span
      className={`shrink-0 ${DOT_CLASS[color]}`}
      aria-hidden
      title={COLOR_LEGEND}
    >
      ●
    </span>
  );
}

// M5 — bar fill matches the tier color (was monochrome bg-accent).
const BAR_FILL_CLASS: Record<MasteryColor, string> = {
  green: "bg-emerald-500",
  yellow: "bg-amber-500",
  red: "bg-red-500",
  grey: "bg-border",
};

function MasteryBar({
  value,
  color,
}: {
  value: number | null;
  color?: MasteryColor;
}) {
  const w = value == null ? 0 : Math.max(0, Math.min(1, value)) * 100;
  const fill = color ? BAR_FILL_CLASS[color] : "bg-accent";
  return (
    <div
      className="h-1 w-full rounded-full bg-border overflow-hidden"
      role="progressbar"
      aria-valuenow={Math.round(w)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="mastery score"
    >
      {value != null && (
        <div
          className={`h-full ${fill} transition-[width] duration-300`}
          style={{ width: `${w}%` }}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Subsection row (leaf — has the action button per L30)
// ─────────────────────────────────────────────────────────────────────────────

function SubsectionRow({
  sub,
  studentId,
  onAction,
}: {
  sub: MasterySubsectionNode;
  studentId: string;
  onAction: (sub: MasterySubsectionNode) => void;
}) {
  const isStart = sub.attempt_count === 0;
  // M5 — button copy: untouched → [Start], touched → [+ New session].
  const buttonLabel = isStart ? "Start" : "+ New session";
  const dateText = sub.last_session_at
    ? `${sub.attempt_count} session${sub.attempt_count === 1 ? "" : "s"} · last ${fmtDate(sub.last_session_at)}`
    : "untouched";

  const [expanded, setExpanded] = useState(false);
  const [sessions, setSessions] = useState<MasterySessionRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // M5 — only touched subsections expand (no sessions to show otherwise).
  const expandable = sub.attempt_count > 0;

  const handleToggle = useCallback(async () => {
    if (!expandable) return;
    const next = !expanded;
    setExpanded(next);
    if (next && sessions === null && !loading && studentId && sub.path) {
      setLoading(true);
      setLoadError(null);
      try {
        const resp = await getMasterySessions(studentId, {
          subsectionPath: sub.path,
          completedOnly: true,
          limit: 20,
        });
        setSessions(resp.sessions);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "load error";
        setLoadError(msg);
      } finally {
        setLoading(false);
      }
    }
  }, [expandable, expanded, sessions, loading, studentId, sub.path]);

  return (
    <div className={BORDER_CLASS[sub.color]}>
      <div className="pl-12 pr-4 py-2 flex items-center gap-3">
        <button
          onClick={handleToggle}
          disabled={!expandable}
          className={`shrink-0 w-4 text-muted-foreground ${expandable ? "cursor-pointer hover:text-foreground" : "opacity-30 cursor-default"}`}
          aria-label={expanded ? "Collapse sessions" : "Expand sessions"}
          aria-expanded={expanded}
        >
          {expandable ? (expanded ? "▾" : "▸") : ""}
        </button>
        <Dot color={sub.color} />
        <div className="flex-1 min-w-0">
          <div className="text-sm truncate">
            {sub.display_label || sub.subsection || "Unknown"}
          </div>
          <div className="text-xs text-muted">{dateText}</div>
        </div>
        <div className="w-24 shrink-0">
          <MasteryBar value={sub.score} color={sub.color} />
        </div>
        <div className="text-xs text-muted w-10 text-right shrink-0">
          {pct(sub.score)}
        </div>
        <button
          onClick={() => onAction(sub)}
          className="rounded-lg border border-border px-2 py-1 text-xs hover:border-accent transition shrink-0"
          title={`${buttonLabel} on ${sub.display_label || sub.subsection || "this topic"}`}
        >
          {buttonLabel}
        </button>
      </div>
      {expanded && expandable && (
        <div className="pl-20 pr-4 pb-3 space-y-1">
          {loading && (
            <div className="text-xs text-muted-foreground italic">
              Loading sessions…
            </div>
          )}
          {loadError && (
            <div className="text-xs text-destructive">
              Failed to load: {loadError}
            </div>
          )}
          {sessions !== null && sessions.length === 0 && (
            <div className="text-xs text-muted-foreground italic">
              No completed sessions yet.
            </div>
          )}
          {sessions?.map((s) => {
            const dateStr = s.ended_at ? fmtDate(s.ended_at) : "";
            const reach = s.reach_status === true ? "reached"
              : s.reach_status === false ? "not reached"
              : (s.status || "");
            const score = s.core_score != null
              ? `.${String(Math.round(s.core_score * 100)).padStart(2, "0")}`
              : "—";
            return (
              <div
                key={s.thread_id}
                className="flex items-center gap-3 text-xs py-1 border-t border-border/50"
              >
                <span className="text-muted-foreground w-24">{dateStr}</span>
                <span className="text-muted-foreground flex-1 truncate">
                  {reach}
                </span>
                <span className="font-mono text-muted-foreground w-10 text-right">
                  {score}
                </span>
                <Link
                  to={`/sessions/${encodeURIComponent(s.thread_id)}`}
                  className="text-accent hover:underline px-2 py-0.5 rounded border border-border hover:border-accent"
                >
                  Open
                </Link>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Section row (mid-level accordion)
// ─────────────────────────────────────────────────────────────────────────────

function SectionRow({
  section,
  expanded,
  onToggle,
  sortByMastery,
  onAction,
  studentId,
}: {
  section: MasterySectionNode;
  expanded: boolean;
  onToggle: () => void;
  sortByMastery: boolean;
  onAction: (sub: MasterySubsectionNode) => void;
  studentId: string;
}) {
  const subs = useMemo(() => {
    if (!sortByMastery) return section.subsections;
    return [...section.subsections].sort(
      (a, b) => sortKey(a.score) - sortKey(b.score),
    );
  }, [section.subsections, sortByMastery]);

  return (
    <div className={`${BORDER_CLASS[section.color]}`}>
      <button
        onClick={onToggle}
        className="w-full pl-8 pr-4 py-2 flex items-center gap-3 hover:bg-bg transition text-left"
        aria-expanded={expanded}
      >
        <span className="text-muted text-sm w-4">{expanded ? "▾" : "▸"}</span>
        <Dot color={section.color} />
        <span className="font-medium text-sm">
          {section.section || "Unknown section"}
        </span>
        <span className="flex-1" />
        <span className="text-xs text-muted shrink-0">
          {section.touched}/{section.total}
        </span>
        <div className="w-24 shrink-0">
          <MasteryBar value={section.score} color={section.color} />
        </div>
        <span className="text-xs text-muted shrink-0 w-10 text-right">
          {pct(section.score)}
        </span>
      </button>
      {expanded && (
        <div className="border-t border-border/50 divide-y divide-border/50">
          {subs.map((sub) => (
            <SubsectionRow
              key={sub.path}
              sub={sub}
              studentId={studentId}
              onAction={onAction}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Chapter row (top-level accordion)
// ─────────────────────────────────────────────────────────────────────────────

function ChapterRow({
  chapter,
  expanded,
  onToggle,
  expandedSections,
  onToggleSection,
  sortByMastery,
  onAction,
  studentId,
}: {
  chapter: MasteryChapterNode;
  expanded: boolean;
  onToggle: () => void;
  expandedSections: Set<string>;
  onToggleSection: (key: string) => void;
  sortByMastery: boolean;
  onAction: (sub: MasterySubsectionNode) => void;
  studentId: string;
}) {
  const sections = useMemo(() => {
    if (!sortByMastery) return chapter.sections;
    return [...chapter.sections].sort(
      (a, b) => sortKey(a.score) - sortKey(b.score),
    );
  }, [chapter.sections, sortByMastery]);

  return (
    <div className={`rounded-card border border-border bg-panel ${BORDER_CLASS[chapter.color]}`}>
      <button
        onClick={onToggle}
        className="w-full px-4 py-3 flex items-center gap-3 hover:bg-bg transition text-left"
        aria-expanded={expanded}
      >
        <span className="text-muted text-sm w-4">{expanded ? "▾" : "▸"}</span>
        <Dot color={chapter.color} />
        <span className="font-medium">
          {chapter.chapter_num != null ? `Ch${chapter.chapter_num} ` : ""}
          {chapter.chapter || ""}
        </span>
        <span className="flex-1" />
        <span className="text-xs text-muted shrink-0">
          {chapter.touched}/{chapter.total} subsections
        </span>
        <div className="w-24 shrink-0">
          <MasteryBar value={chapter.score} color={chapter.color} />
        </div>
        <span className="text-xs text-muted shrink-0 w-10 text-right">
          {pct(chapter.score)}
        </span>
      </button>
      {expanded && (
        <div className="border-t border-border divide-y divide-border">
          {sections.map((section) => {
            const key = `${chapter.chapter}::${section.section}`;
            return (
              <SectionRow
                key={key}
                section={section}
                expanded={expandedSections.has(key)}
                onToggle={() => onToggleSection(key)}
                sortByMastery={sortByMastery}
                onAction={onAction}
                studentId={studentId}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────────

export function MasteryView() {
  const studentId = useUserStore((s) => s.studentId);
  const resetSession = useSessionStore((s) => s.reset);
  const navigate = useNavigate();
  const [data, setData] = useState<MasteryTreeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedChapters, setExpandedChapters] = useState<Set<string>>(
    new Set(),
  );
  const [expandedSections, setExpandedSections] = useState<Set<string>>(
    new Set(),
  );
  const [sortByMastery, setSortByMastery] = useState(false);

  const load = useCallback(async () => {
    if (!studentId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getMasteryTree(studentId);
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

  const toggleChapter = (key: string) => {
    setExpandedChapters((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleSection = (key: string) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleAction = (sub: MasterySubsectionNode) => {
    // M4 (B6) — pass the canonical subsection PATH to the backend via
    // REVISIT_TOPIC_PATH (consumed by useSession bootstrap and forwarded
    // as `prelocked_topic` to startSession). Backend generates 3 anchor
    // question variations and ships them as initial_pending_choice
    // (kind="anchor_pick"). The student picks WHICH anchor to work on.
    try {
      const path = (sub.path || "").trim();
      if (path) {
        localStorage.setItem(REVISIT_TOPIC_PATH, path);
      }
      // Drop the legacy subsection-name auto-inject hack — superseded.
      localStorage.removeItem(REVISIT_KEY);
    } catch {
      // localStorage unavailable — user just won't get the prelock
    }
    resetSession();
    navigate("/chat");
  };

  const chapters = useMemo(() => {
    if (!data) return [];
    if (!sortByMastery) return data.chapters;
    return [...data.chapters].sort((a, b) => sortKey(a.score) - sortKey(b.score));
  }, [data, sortByMastery]);

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

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-lane mx-auto px-6 py-8 space-y-6">
          <div className="flex items-baseline justify-between gap-4">
            <h1 className="text-2xl font-semibold">My mastery</h1>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-2 text-xs text-muted cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={sortByMastery}
                  onChange={(e) => setSortByMastery(e.target.checked)}
                  className="accent-accent"
                />
                Sort by mastery
              </label>
              <button
                onClick={() => void load()}
                className="rounded-lg border border-border px-3 py-1.5 text-sm hover:border-accent transition"
              >
                Refresh
              </button>
            </div>
          </div>

          {chapters.length === 0 ? (
            <div className="rounded-card border border-border bg-panel px-4 py-8 text-center">
              <div className="font-medium">No corpus loaded</div>
              <div className="mt-1 text-sm text-muted">
                The mastery tree is empty. Check the topic index for the
                active domain.
              </div>
            </div>
          ) : (
            <section className="space-y-2">
              {chapters.map((chapter) => {
                const key = `${chapter.chapter_num ?? ""}::${chapter.chapter}`;
                return (
                  <ChapterRow
                    key={key}
                    chapter={chapter}
                    expanded={expandedChapters.has(key)}
                    onToggle={() => toggleChapter(key)}
                    expandedSections={expandedSections}
                    onToggleSection={toggleSection}
                    sortByMastery={sortByMastery}
                    onAction={handleAction}
                    studentId={studentId}
                  />
                );
              })}
            </section>
          )}
        </div>
      </div>
    </AppShell>
  );
}
