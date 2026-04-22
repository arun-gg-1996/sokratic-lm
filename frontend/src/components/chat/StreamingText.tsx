import { useEffect, useState } from "react";

interface StreamingTextProps {
  text: string;
  speedMs?: number;
  enabled?: boolean;
  onComplete?: () => void;
}

export function StreamingText({
  text,
  speedMs = 18,
  enabled = true,
  onComplete,
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

  return (
    <span>
      {shown}
      {isStreaming && <span className="animate-pulse">▌</span>}
    </span>
  );
}
