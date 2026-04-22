import { FormEvent, KeyboardEvent, useState } from "react";
import { useSessionStore } from "../../stores/sessionStore";

interface ComposerProps {
  onSubmit: (text: string) => void;
  placeholder?: string;
}

export function Composer({ onSubmit, placeholder = "Reply..." }: ComposerProps) {
  const [text, setText] = useState("");
  const isWaiting = useSessionStore((s) => s.isWaitingForTutor);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || isWaiting) return;
    onSubmit(trimmed);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="shrink-0 border-t border-border bg-bg">
      <form onSubmit={submit} className="max-w-lane mx-auto px-6 py-4">
        <div className="rounded-composer border border-border bg-panel p-3 flex gap-3 items-end">
          <textarea
            className="flex-1 resize-none bg-transparent outline-none text-text placeholder:text-muted leading-relaxed min-h-[52px] max-h-[180px]"
            placeholder={placeholder}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            disabled={isWaiting}
          />
          <button
            type="submit"
            disabled={isWaiting || !text.trim()}
            className="h-10 w-10 rounded-full bg-text text-bg disabled:opacity-40 disabled:cursor-not-allowed"
            aria-label="Send"
          >
            ↑
          </button>
        </div>
      </form>
    </div>
  );
}
