/**
 * Composer — chat input box.
 *
 * L80.d (UX polish):
 *   - Visible disabled state (greyed bg, dashed border, not-allowed cursor)
 *     when isWaiting OR a pending_user_choice is open.
 *   - Context-aware helper text replaces the placeholder while disabled
 *     so the student knows WHY they can't type instead of staring at a
 *     dead box. Helper picked from the pending choice kind.
 *
 * L79 (accessibility):
 *   - Mic button next to send. Click → browser-native SpeechRecognition
 *     transcribes speech into the input. Click again or stop → keeps the
 *     transcript in the input box; the student edits + presses send.
 *     No auto-send. Hidden when SpeechRecognition isn't available.
 *
 * Send button shares the same disabled treatment so they read as one
 * unit. 100ms transition on background color per L80.e.
 */
import { FormEvent, KeyboardEvent, useState } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { useSTT } from "../../hooks/useSTT";

interface ComposerProps {
  onSubmit: (text: string) => void;
  placeholder?: string;
}

function helperFor(
  isWaiting: boolean,
  pendingKind: string | undefined,
): string | null {
  if (isWaiting) return "Tutor is responding…";
  switch (pendingKind) {
    case "opt_in":
      return "Click Yes or No above ↑";
    case "confirm_topic":
      return "Click Yes to lock or No to pick a different topic ↑";
    case "topic":
      return "Pick a topic above ↑";
    default:
      return null;
  }
}

export function Composer({ onSubmit, placeholder = "Reply..." }: ComposerProps) {
  const [text, setText] = useState("");
  const isWaiting = useSessionStore((s) => s.isWaitingForTutor);
  const pendingChoice = useSessionStore((s) => s.pendingChoice);
  const stt = useSTT();
  const helper = helperFor(isWaiting, pendingChoice?.kind);
  // ChatView already hides the Composer entirely when pendingChoice is
  // set, so the most common disabled path is "tutor is streaming".
  const disabled = isWaiting;
  const showHelper = disabled && helper;

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    // Stop any in-flight recognition so it doesn't overwrite the text
    // we're about to send.
    if (stt.listening) stt.stop();
    onSubmit(trimmed);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const toggleMic = () => {
    if (disabled) return;
    if (stt.listening) {
      stt.stop();
      return;
    }
    stt.start((transcript) => {
      // Replace the input text with the live transcript. The student
      // can edit before sending. Per L79: no auto-send.
      setText(transcript);
    });
  };

  return (
    <div className="shrink-0 border-t border-border bg-bg">
      <form onSubmit={submit} className="max-w-lane mx-auto px-6 py-4">
        <div
          className={`rounded-composer flex gap-3 items-end p-3 transition-colors duration-100 ${
            disabled
              ? "border border-dashed border-border/60 bg-panel/40 cursor-not-allowed"
              : "border border-border bg-panel"
          }`}
        >
          <textarea
            className={`flex-1 resize-none bg-transparent outline-none text-text leading-relaxed min-h-[52px] max-h-[180px] ${
              disabled
                ? "cursor-not-allowed placeholder:text-muted/60"
                : "placeholder:text-muted"
            }`}
            placeholder={showHelper ? (helper as string) : placeholder}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            disabled={disabled}
            aria-disabled={disabled}
            aria-label={showHelper ? (helper as string) : "Reply to tutor"}
          />
          {stt.supported && (
            <button
              type="button"
              onClick={toggleMic}
              disabled={disabled}
              className={`h-10 w-10 rounded-full transition-colors duration-100 ${
                disabled
                  ? "bg-muted/20 text-muted/40 cursor-not-allowed"
                  : stt.listening
                    ? "bg-red-500 text-bg animate-pulse"
                    : "bg-panel text-text border border-border hover:border-accent"
              }`}
              aria-label={stt.listening ? "Stop recording" : "Start voice input"}
              title={stt.listening ? "Stop recording" : "Speak your reply"}
            >
              {stt.listening ? "■" : "🎤"}
            </button>
          )}
          <button
            type="submit"
            disabled={disabled || !text.trim()}
            className={`h-10 w-10 rounded-full transition-colors duration-100 ${
              disabled
                ? "bg-muted/30 text-muted/50 cursor-not-allowed"
                : "bg-text text-bg disabled:opacity-40 disabled:cursor-not-allowed"
            }`}
            aria-label="Send"
          >
            ↑
          </button>
        </div>
      </form>
    </div>
  );
}
