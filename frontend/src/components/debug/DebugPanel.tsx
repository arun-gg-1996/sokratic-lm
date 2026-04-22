import { useUserStore } from "../../stores/userStore";
import { useSessionStore } from "../../stores/sessionStore";

function renderJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function DebugPanel() {
  const debugMode = useUserStore((s) => s.debugMode);
  const debug = useSessionStore((s) => s.debug);
  const messages = useSessionStore((s) => s.messages);
  const selectedDebugMessageId = useSessionStore((s) => s.selectedDebugMessageId);
  const setSelectedDebugMessageId = useSessionStore((s) => s.setSelectedDebugMessageId);

  if (!debugMode) return null;

  const fullTrace = Array.isArray(debug?.all_turn_traces)
    ? (debug?.all_turn_traces as Array<Record<string, unknown>>)
    : [];
  const liveTrace = Array.isArray(debug?.turn_trace) ? (debug?.turn_trace as Array<Record<string, unknown>>) : [];
  const tutorMessages = messages.filter((m) => m.role === "tutor" && (m.debugTrace?.length ?? 0) > 0);
  const selectedMessage = tutorMessages.find((m) => m.id === selectedDebugMessageId) ?? null;
  const selectedTrace = selectedMessage?.debugTrace ?? liveTrace;
  const trace = selectedTrace;
  const summaryRows: Array<[string, unknown]> = [
    ["phase", debug?.phase],
    ["current_node", debug?.current_node],
    ["last_routing", debug?.last_routing],
    ["topic_confirmed", debug?.topic_confirmed],
    ["topic_selection", debug?.topic_selection],
    ["locked_question", debug?.locked_question],
    ["answer_locked", debug?.answer_locked],
    ["student_state", debug?.student_state],
    ["hint_level", debug?.hint_level],
    ["locked_answer", debug?.locked_answer],
    ["domain", debug?.domain],
    ["assessment_turn", debug?.assessment_turn],
    ["turn_count", debug?.turn_count],
    ["api_calls", debug?.api_calls],
    ["input_tokens", debug?.input_tokens],
    ["output_tokens", debug?.output_tokens],
    ["cost_usd", debug?.cost_usd],
    ["retrieval_calls", debug?.retrieval_calls],
    ["all_turn_traces", fullTrace.length],
  ];

  return (
    <div className="fixed bottom-0 left-[260px] right-0 bg-panel border-t border-border max-h-[34vh] overflow-y-auto z-20">
      <div className="max-w-lane mx-auto px-6 py-3 text-xs font-mono text-muted space-y-3">
        <div className="text-text font-semibold">Debug trace</div>
        {tutorMessages.length > 0 && (
          <div className="rounded-lg border border-border p-2 space-y-2">
            <div className="text-text">Message trace selector</div>
            <div className="flex flex-wrap gap-2">
              {tutorMessages.map((m, idx) => (
                <button
                  key={m.id}
                  className={[
                    "rounded border px-2 py-1 text-[11px]",
                    selectedDebugMessageId === m.id ? "border-accent text-text" : "border-border",
                  ].join(" ")}
                  onClick={() => setSelectedDebugMessageId(selectedDebugMessageId === m.id ? null : m.id)}
                  title={m.content}
                >
                  T{idx + 1}{typeof m.debugTurn === "number" ? ` · turn ${m.debugTurn}` : ""}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="rounded-lg border border-border p-2 grid grid-cols-2 gap-x-4 gap-y-1">
          {summaryRows.map(([k, v]) => (
            <div key={k} className="truncate">
              <span className="text-text">{k}</span>: {String(v ?? "-")}
            </div>
          ))}
        </div>
        <details className="rounded-lg border border-border p-2">
          <summary className="cursor-pointer text-text">Hint plan</summary>
          <pre className="mt-2 whitespace-pre-wrap break-words text-[11px]">
            {renderJson(debug?.hint_plan ?? [])}
          </pre>
        </details>
        <details className="rounded-lg border border-border p-2">
          <summary className="cursor-pointer text-text">Hint progression</summary>
          <pre className="mt-2 whitespace-pre-wrap break-words text-[11px]">
            {renderJson(debug?.hint_progress ?? [])}
          </pre>
        </details>
        {trace.length === 0 && <div>No turn trace available.</div>}
        {trace.map((entry, idx) => (
          <div key={idx} className="rounded-lg border border-border p-2 space-y-2">
            <div className="text-text">
              {idx + 1}. {String(entry.wrapper ?? "-")}
            </div>
            <div className="grid grid-cols-3 gap-x-3 gap-y-1">
              <div>elapsed_s: {String(entry.elapsed_s ?? "-")}</div>
              <div>in_tok: {String(entry.in_tok ?? "-")}</div>
              <div>out_tok: {String(entry.out_tok ?? "-")}</div>
              <div>cache_read: {String(entry.cache_read ?? "-")}</div>
              <div>cache_write: {String(entry.cache_write ?? "-")}</div>
              <div>cost_usd: {String(entry.cost_usd ?? "-")}</div>
              <div className="col-span-3">input_hash: {String(entry.input_hash ?? "-")}</div>
              <div className="col-span-3">decision_effect: {String(entry.decision_effect ?? "-")}</div>
            </div>

            {"result" in entry && <div>result: {String(entry.result ?? "-")}</div>}

            <details className="rounded border border-border p-2">
              <summary className="cursor-pointer text-text">System</summary>
              <pre className="mt-2 whitespace-pre-wrap break-words text-[11px]">
                {String(entry.system_prompt ?? "(none)")}
              </pre>
            </details>

            <details className="rounded border border-border p-2">
              <summary className="cursor-pointer text-text">Input</summary>
              <pre className="mt-2 whitespace-pre-wrap break-words text-[11px]">
                {renderJson(entry.messages_sent ?? [])}
              </pre>
            </details>

            <details className="rounded border border-border p-2">
              <summary className="cursor-pointer text-text">Tools</summary>
              <pre className="mt-2 whitespace-pre-wrap break-words text-[11px]">
                {renderJson(entry.tool_calls_made ?? [])}
              </pre>
            </details>

            <details className="rounded border border-border p-2">
              <summary className="cursor-pointer text-text">Output</summary>
              <pre className="mt-2 whitespace-pre-wrap break-words text-[11px]">
                {String(entry.response_text ?? "(none)")}
              </pre>
            </details>
          </div>
        ))}
      </div>
    </div>
  );
}
