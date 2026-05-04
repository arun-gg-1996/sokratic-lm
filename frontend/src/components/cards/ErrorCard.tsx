/**
 * ErrorCard — distinct chat-bubble that surfaces an LLM-call failure.
 *
 * M-FB principle: never render fake tutor text on LLM failure. Instead,
 * render this card showing the failing component, error class, message
 * preview, and a [Retry] button.
 *
 * Backend signals an error card via a system message with metadata of
 * shape:
 *   {
 *     kind: "error_card",
 *     component:    "Teacher.draft[close]",
 *     error_class:  "TimeoutError",
 *     message:      "<exception preview>",
 *     retry_handler: "<id>"
 *   }
 *
 * Retry mechanism (M-FB D2): clicking Retry re-POSTs the user's last
 * message via onRetry — backend re-runs the whole turn from scratch.
 * No per-handler granular retry plumbing.
 */
interface ErrorCardProps {
  component: string;
  errorClass: string;
  message: string;
  onRetry?: () => void;
}

export function ErrorCard({ component, errorClass, message, onRetry }: ErrorCardProps) {
  return (
    <div className="my-2 mx-auto max-w-2xl rounded-lg border border-red-500/50 bg-red-500/10 px-4 py-3 text-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-red-600 dark:text-red-400 flex items-center gap-2">
            <span aria-hidden>⚠</span>
            <span>Couldn't generate the response</span>
          </div>
          <div className="mt-1 text-xs text-muted font-mono break-words">
            {component} · {errorClass}
          </div>
          {message && (
            <div className="mt-1 text-xs text-muted break-words">
              {message.length > 220 ? message.slice(0, 220) + "..." : message}
            </div>
          )}
        </div>
        {onRetry && (
          <button
            onClick={onRetry}
            className="shrink-0 rounded-md border border-red-500/50 px-3 py-1 text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-500/10 transition"
          >
            Retry
          </button>
        )}
      </div>
    </div>
  );
}
