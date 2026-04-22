import { useEffect, useState } from "react";
import { AppShell } from "../components/layout/AppShell";
import { getStudentOverview } from "../api/client";
import { useUserStore } from "../stores/userStore";

export function SessionOverview() {
  const studentId = useUserStore((s) => s.studentId);
  const [data, setData] = useState<{ weak_topics: Array<Record<string, unknown>>; strong_topics: Array<Record<string, unknown>> }>({
    weak_topics: [],
    strong_topics: [],
  });

  useEffect(() => {
    if (!studentId) return;
    getStudentOverview(studentId)
      .then((res) => setData({ weak_topics: res.weak_topics ?? [], strong_topics: res.strong_topics ?? [] }))
      .catch(() => setData({ weak_topics: [], strong_topics: [] }));
  }, [studentId]);

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-lane mx-auto px-6 py-8 space-y-8">
          <section>
            <h2 className="text-xl font-semibold mb-3">Weak topics</h2>
            <div className="space-y-2">
              {data.weak_topics.map((t, idx) => (
                <div key={idx} className="rounded-card border border-border bg-panel px-4 py-3">
                  <div className="font-medium">{String(t.topic ?? "Unknown topic")}</div>
                  <div className="text-sm text-muted">
                    failures: {String(t.failure_count ?? 0)} | difficulty: {String(t.difficulty ?? "-")}
                  </div>
                </div>
              ))}
              {data.weak_topics.length === 0 && <div className="text-muted">No weak topics yet.</div>}
            </div>
          </section>

          <section>
            <h2 className="text-xl font-semibold mb-3">Strong topics</h2>
            <div className="flex flex-wrap gap-2">
              {data.strong_topics.map((t, idx) => (
                <span key={idx} className="rounded-full bg-accent-soft px-3 py-1 text-sm">
                  {String((t as Record<string, unknown>).topic ?? "Topic")}
                </span>
              ))}
              {data.strong_topics.length === 0 && <div className="text-muted">No strong-topic tracking yet.</div>}
            </div>
          </section>

          {data.weak_topics.length === 0 && data.strong_topics.length === 0 && (
            <div className="text-muted">No session history yet. Start a chat to build your profile.</div>
          )}
        </div>
      </div>
    </AppShell>
  );
}
