import { useEffect, useState } from "react";
import type { ReactNode } from "react";

interface StreamingTextProps {
  text: string;
  speedMs?: number;
  enabled?: boolean;
  onComplete?: () => void;
  /**
   * N5 (POST_DEMO_FIXES.md, 2026-05-06): optional formatter for the
   * FINAL text. While streaming we render plain text + cursor (so a
   * half-typed `**bo` doesn't flash unformatted asterisks). On
   * completion (or when streaming is disabled), the renderer is
   * applied to the full string. Default: plain text.
   */
  renderer?: (text: string) => ReactNode;
}

export function StreamingText({
  text,
  speedMs = 18,
  enabled = true,
  onComplete,
  renderer,
}: StreamingTextProps) {
  const [shown, setShown] = useState(enabled ? "" : text);

  useEffect(() => {
    if (!enabled) {
      setShown(text);
      return;
    }

    setShown("");
    let i = 0;
    const timer = window.setInterval(() => {
      i += 1;
      setShown(text.slice(0, i));
      if (i >= text.length) {
        window.clearInterval(timer);
        onComplete?.();
      }
    }, speedMs);

    return () => window.clearInterval(timer);
  }, [enabled, onComplete, speedMs, text]);

  const isStreaming = enabled && shown.length < text.length;

  if (isStreaming) {
    return (
      <span>
        {shown}
        <span className="animate-pulse">▌</span>
      </span>
    );
  }

  // Streaming complete (or disabled): apply optional renderer for
  // markdown / formatting. Wrap in a span so the parent layout stays
  // consistent with the streaming-state span.
  if (renderer) {
    return <span>{renderer(text)}</span>;
  }
  return <span>{text}</span>;
}
