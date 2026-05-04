/**
 * SessionAnalysis — M5 analysis view.
 *
 * 3 panels:
 *   TRANSCRIPT  — read-only message log
 *   SUMMARY     — locked Q/A + mastery + key_takeaways (with [Regenerate])
 *   ANALYSIS CHAT — scoped Sonnet conversation about THIS session
 *
 * Per M5 design (locked decisions):
 *   - D1 keep Haiku scope check before Sonnet
 *   - D2 ephemeral analysis chat (no DB writes, history lost on navigate)
 *   - D3 regenerate button rebuilds inputs lazily at click time
 *   - D4 sessions row only created at memory_update_node (no in_progress here)
 *   - D5 transcript glob by thread_suffix
 */
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AppShell } from "../components/layout/AppShell";
import {
  getMasterySession,
  getSessionTranscript,
  postAnalysisChat,
  regenerateTakeaways,
  type AnalysisChatResponse,
  type TranscriptMessage,
} from "../api/client";
import type { MasterySessionRow } from "../types";

interface AnalysisChatTurn {
  role: "user" | "tutor" | "system";
  content: string;
}

export function SessionAnalysis() {
  const { threadId } = useParams<{ threadId: string }>();
  const [session, setSession] = useState<MasterySessionRow | null>(null);
  const [transcript, setTranscript] = useState<TranscriptMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Analysis chat (ephemeral)
  const [analysisHistory, setAnalysisHistory] = useState<AnalysisChatTurn[]>([]);
  const [analysisInput, setAnalysisInput] = useState("");
  const [analysisLoading, setAnalysisLoading] = useState(false);

  // Regenerate
  const [regenerating, setRegenerating] = useState(false);

  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    setLoading(true);
    Promise.all([
      getMasterySession(threadId).catch((e) => { throw new Error(`session: ${e.message}`); }),
      getSessionTranscript(threadId).catch((e) => { throw new Error(`transcript: ${e.message}`); }),
    ])
      .then(([s, t]) => {
        if (cancelled) return;
        setSession(s);
        setTranscript(t.messages);
        setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e.message || "Failed to load session");
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [threadId]);

  const handleAnalysisSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const msg = analysisInput.trim();
    if (!msg || !threadId || analysisLoading) return;
    setAnalysisInput("");
    const userTurn: AnalysisChatTurn = { role: "user", content: msg };
    setAnalysisHistory((prev) => [...prev, userTurn]);
    setAnalysisLoading(true);
    try {
      const resp: AnalysisChatResponse = await postAnalysisChat(
        threadId, msg,
        analysisHistory.map((t) => ({ role: t.role, content: t.content }))
      );
      const tutorTurn: AnalysisChatTurn = {
        role: resp.in_scope ? "tutor" : "system",
        content: resp.reply,
      };
      setAnalysisHistory((prev) => [...prev, tutorTurn]);
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : "analysis chat failed";
      setAnalysisHistory((prev) => [
        ...prev,
        { role: "system", content: `Error: ${errMsg}` },
      ]);
    } finally {
      setAnalysisLoading(false);
    }
  };

  const handleRegenerate = async () => {
    if (!threadId || regenerating) return;
    setRegenerating(true);
    try {
      const resp = await regenerateTakeaways(threadId);
      if (resp.success) {
        const updated = await getMasterySession(threadId);
        setSession(updated);
      } else {
        setError(`Regenerate failed: ${resp.error || "unknown"}`);
      }
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : "regenerate failed";
      setError(errMsg);
    } finally {
      setRegenerating(false);
    }
  };

  if (loading) {
    return (
      <AppShell>
        <div className="flex-1 flex items-center justify-center text-muted">Loading session…</div>
      </AppShell>
    );
  }
  if (error) {
    return (
      <AppShell>
        <div className="flex-1 flex flex-col items-center justify-center gap-3">
          <div className="text-red-600 dark:text-red-400">{error}</div>
          <Link to="/mastery" className="text-accent underline">← My Mastery</Link>
        </div>
      </AppShell>
    );
  }
  if (!session) {
    return (
      <AppShell>
        <div className="flex-1 flex items-center justify-center text-muted">Session not found.</div>
      </AppShell>
    );
  }

  const subsectionLabel = session.locked_subsection_path
    ? session.locked_subsection_path.split("|").slice(-1)[0]
    : "Session";
  const takeaways = session.key_takeaways || null;
  const score = session.core_score != null ? `${Math.round(session.core_score * 100)}%` : "—";
  const tier = session.mastery_tier || "—";

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6 space-y-5">
          <div className="text-sm text-muted">
            <Link to="/mastery" className="hover:text-accent">← My Mastery</Link>
            <span className="mx-2">/</span>
            <span>{subsectionLabel}</span>
          </div>

          {/* Transcript panel */}
          <section className="border border-border rounded-lg p-4 bg-panel">
            <h2 className="text-sm font-semibold uppercase text-muted tracking-wide mb-3">
              Transcript
            </h2>
            {transcript.length === 0 ? (
              <div className="text-sm text-muted italic">
                No transcript available for this session.
              </div>
            ) : (
              <div className="space-y-2 max-h-80 overflow-y-auto pr-2">
                {transcript.map((m, i) => (
                  <div
                    key={i}
                    className={
                      m.role === "tutor"
                        ? "text-sm bg-muted/30 rounded px-3 py-2"
                        : "text-sm bg-accent/10 rounded px-3 py-2 ml-8"
                    }
                  >
                    <div className="text-xs text-muted mb-1">
                      {m.role === "tutor" ? "Tutor" : "You"}
                    </div>
                    <div className="whitespace-pre-wrap">{m.content}</div>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Summary panel */}
          <section className="border border-border rounded-lg p-4 bg-panel">
            <div className="flex items-start justify-between mb-3">
              <h2 className="text-sm font-semibold uppercase text-muted tracking-wide">
                Summary
              </h2>
              {!takeaways && (
                <button
                  onClick={handleRegenerate}
                  disabled={regenerating}
                  className="text-xs text-accent hover:underline disabled:text-muted"
                >
                  {regenerating ? "Regenerating..." : "Regenerate"}
                </button>
              )}
            </div>
            <div className="text-sm space-y-2">
              {session.locked_question && (
                <div>
                  <span className="text-muted">Locked Q: </span>
                  <span>{session.locked_question}</span>
                </div>
              )}
              {session.locked_answer && (
                <div>
                  <span className="text-muted">Answer: </span>
                  <span>{session.locked_answer}</span>
                </div>
              )}
              <div className="flex gap-4 pt-1 text-xs">
                <span>Status: <strong>{session.status}</strong></span>
                <span>Score: <strong>{score}</strong></span>
                <span>Tier: <strong>{tier}</strong></span>
              </div>
              {takeaways && (
                <div className="pt-2 mt-2 border-t border-border space-y-1">
                  {takeaways.demonstrated && (
                    <div>
                      <span className="text-emerald-600 dark:text-emerald-400">✓ Demonstrated: </span>
                      <span>{takeaways.demonstrated}</span>
                    </div>
                  )}
                  {takeaways.needs_work && (
                    <div>
                      <span className="text-amber-600 dark:text-amber-400">⚠ Needs work: </span>
                      <span>{takeaways.needs_work}</span>
                    </div>
                  )}
                </div>
              )}
            </div>
          </section>

          {/* Analysis chat panel */}
          <section className="border border-border rounded-lg p-4 bg-panel">
            <h2 className="text-sm font-semibold uppercase text-muted tracking-wide mb-3">
              📖 Analysis chat <span className="text-xs font-normal normal-case text-muted">— scoped to this session</span>
            </h2>
            <div className="space-y-2 mb-3 max-h-60 overflow-y-auto pr-2">
              {analysisHistory.length === 0 && !analysisLoading && (
                <div className="text-sm text-muted italic">
                  Ask a question about this session — what you got stuck on, why a hint
                  didn't help, etc.
                </div>
              )}
              {analysisHistory.map((t, i) => {
                const isSystem = t.role === "system";
                return (
                  <div
                    key={i}
                    className={
                      isSystem
                        ? "text-sm rounded px-3 py-2 bg-amber-500/10 border border-amber-500/30"
                        : t.role === "user"
                          ? "text-sm bg-accent/10 rounded px-3 py-2 ml-8"
                          : "text-sm bg-muted/30 rounded px-3 py-2"
                    }
                  >
                    <div className="text-xs text-muted mb-1">
                      {t.role === "user" ? "You" : (isSystem ? "Note" : "Tutor")}
                    </div>
                    <div className="whitespace-pre-wrap">{t.content}</div>
                  </div>
                );
              })}
              {analysisLoading && (
                <div className="text-sm text-muted italic">Thinking…</div>
              )}
            </div>
            <form onSubmit={handleAnalysisSubmit} className="flex gap-2">
              <input
                type="text"
                value={analysisInput}
                onChange={(e) => setAnalysisInput(e.target.value)}
                placeholder="Ask about this session..."
                disabled={analysisLoading}
                className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm focus:outline-none focus:border-accent"
              />
              <button
                type="submit"
                disabled={analysisLoading || !analysisInput.trim()}
                className="rounded-md bg-accent text-accent-foreground px-4 py-2 text-sm font-medium hover:bg-accent/90 disabled:opacity-50"
              >
                Send
              </button>
            </form>
          </section>
        </div>
      </div>
    </AppShell>
  );
}
