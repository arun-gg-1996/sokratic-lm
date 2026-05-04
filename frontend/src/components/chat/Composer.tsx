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
import { FormEvent, KeyboardEvent, useRef, useState } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import { useSTT } from "../../hooks/useSTT";
import { uploadVlmImage } from "../../api/client";

const IMAGE_CONTEXT_KEY = "sokratic_pending_image_context";
const IMAGE_ACCEPT = ".png,.jpg,.jpeg,.webp";

interface ComposerProps {
  // imageUrl: when provided, a single student bubble with image
  // preview + caption is rendered instead of a text-only bubble.
  // Used by the VLM upload flow.
  onSubmit: (text: string, imageUrl?: string) => void;
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
  const debug = useSessionStore((s) => s.debug) as Record<string, unknown> | null;
  const messages = useSessionStore((s) => s.messages);
  const sessionEnded = useSessionStore((s) => s.sessionEnded);
  const stt = useSTT();
  const helper = helperFor(isWaiting, pendingChoice?.kind);
  // ChatView already hides the Composer entirely when pendingChoice is
  // set, so the most common disabled path is "tutor is streaming".
  // M1 — once session is ended, hard-disable input. Banner above explains why.
  const disabled = isWaiting || sessionEnded;
  const showHelper = disabled && (helper || (sessionEnded ? "Session ended — visit My Mastery to review or start a new session." : null));

  // M1 — render a session-ended banner above the disabled input so the
  // student understands why typing is blocked.
  if (sessionEnded) {
    return (
      <div className="border-t border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground flex items-center justify-between gap-3">
        <span>Session ended. Visit My Mastery to review or start a new session.</span>
        <a
          href="/mastery"
          className="text-accent hover:underline whitespace-nowrap font-medium"
        >
          Open My Mastery →
        </a>
      </div>
    );
  }

  // Image upload affordance — available only before a topic is locked
  // and only on a fresh chat (no student messages yet). Once the
  // student has typed or a topic is confirmed, the + button hides.
  const topicConfirmed = Boolean(debug?.topic_confirmed);
  const noStudentTurnsYet = messages.filter((m) => m.role === "student").length === 0;
  const showImageButton = !topicConfirmed && noStudentTurnsYet;
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const onUploadClick = () => {
    if (disabled || uploading) return;
    fileRef.current?.click();
  };

  const onFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = ""; // allow re-picking the same file later
    setUploading(true);
    setUploadError(null);
    const previewUrl = URL.createObjectURL(file);
    try {
      const placeholder = `pending_${Math.random().toString(36).slice(2, 10)}`;
      const result = await uploadVlmImage(placeholder, file);
      if (result.route_decision === "refuse" || result.confidence < 0.3) {
        setUploadError(
          "Couldn't recognize that image — try a labeled diagram or skip and type the topic.",
        );
        setUploading(false);
        return;
      }
      const topicText = (result.best_topic_guess || result.description || "").trim();
      if (!topicText) {
        setUploadError("Image analyzed but no topic guess returned.");
        setUploading(false);
        return;
      }
      // Hand off to the standard submit path with imageUrl — useSession
      // renders one student bubble (image + caption) and dispatches the
      // topicText to Dean for normal topic resolution.
      onSubmit(topicText, previewUrl);
      setUploading(false);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
      setUploading(false);
    }
  };

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
        {uploadError && (
          <div className="mb-2 text-xs text-red-400" role="alert">{uploadError}</div>
        )}
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
          {showImageButton && (
            <>
              <input
                ref={fileRef}
                type="file"
                accept={IMAGE_ACCEPT}
                onChange={onFileChange}
                className="hidden"
              />
              <button
                type="button"
                onClick={onUploadClick}
                disabled={disabled || uploading}
                className={`h-10 w-10 rounded-full transition-colors duration-100 ${
                  disabled || uploading
                    ? "bg-muted/20 text-muted/40 cursor-not-allowed"
                    : "bg-panel text-text border border-border hover:border-accent"
                }`}
                aria-label="Upload an anatomical image"
                title={uploading ? "Analyzing image…" : "Upload an image (anatomical diagram or photo)"}
              >
                {uploading ? (
                  <span className="inline-block h-3 w-3 rounded-full border-2 border-muted border-t-accent animate-spin" />
                ) : (
                  "+"
                )}
              </button>
            </>
          )}
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
