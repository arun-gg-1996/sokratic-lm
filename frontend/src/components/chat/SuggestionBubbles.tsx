/**
 * SuggestionBubbles — N8 (POST_DEMO_FIXES.md, 2026-05-06)
 *
 * Renders 4 student-reply suggestion bubbles below a tutor message
 * when the "Suggest answers" toggle is on. Click → submits the bubble
 * text as the next student message. Type-your-own remains available
 * via the composer.
 *
 * Per N8 design:
 *   - Visible only when sessionStore.suggestEnabled is true
 *   - Each bubble shows kind label + color band + small rationale on hover
 *   - 4-bubble mix is intent-class diverse (per backend's prompt rules)
 *   - Profile chosen via the dropdown in the header
 */
import { useEffect, useState } from "react";
import { postSuggestReplies, type SuggestionItem } from "../../api/client";

interface Props {
  threadId: string | null;
  profile: string;
  enabled: boolean;
  /** ID of the tutor message these suggestions belong to.
   * Used to cache so toggling off and back on doesn't refetch. */
  tutorMessageId: string;
  /** Click handler — invoked with the bubble text when the user picks one. */
  onPick: (text: string) => void;
}

// Color token → tailwind classes. Mirrors backend _color_for_kind.
// 2026-05-06 demo-feedback: bubble TEXT colour was matching the
// background tint (e.g. emerald text on emerald-tinted bg), which made
// it nearly unreadable. Border keeps the kind cue; text now uses
// high-contrast neutral (`text-foreground`) so the suggestion is easy
// to read across all colour variants and both dark/light themes.
const COLOR_CLASSES: Record<string, string> = {
  green: "bg-emerald-500/15 border-emerald-500/50 text-foreground hover:bg-emerald-500/25",
  "yellow-green": "bg-lime-500/15 border-lime-500/50 text-foreground hover:bg-lime-500/25",
  yellow: "bg-yellow-500/15 border-yellow-500/50 text-foreground hover:bg-yellow-500/25",
  orange: "bg-orange-500/15 border-orange-500/50 text-foreground hover:bg-orange-500/25",
  "red-orange": "bg-rose-500/15 border-rose-500/50 text-foreground hover:bg-rose-500/25",
  red: "bg-red-500/15 border-red-500/50 text-foreground hover:bg-red-500/25",
  blue: "bg-blue-500/15 border-blue-500/50 text-foreground hover:bg-blue-500/25",
  purple: "bg-purple-500/15 border-purple-500/50 text-foreground hover:bg-purple-500/25",
  muted: "bg-muted/15 border-muted/50 text-foreground hover:bg-muted/25",
};

const KIND_LABEL: Record<string, string> = {
  correct: "✓ correct",
  partial: "~ partial",
  wrong_engaged: "◐ wrong-engaged",
  low_effort: "⚠ low-effort",
  help_abuse: "⊘ help-abuse",
  off_topic: "✗ off-topic",
  opt_in_yes: "Yes",
  opt_in_no: "No",
  exit_intent: "⊗ exit",
};

export function SuggestionBubbles({ threadId, profile, enabled, tutorMessageId, onPick }: Props) {
  const [suggestions, setSuggestions] = useState<SuggestionItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !threadId) {
      setSuggestions(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    postSuggestReplies(threadId, profile)
      .then((r) => {
        if (cancelled) return;
        if (r.error) {
          setError(r.error);
          setSuggestions([]);
        } else {
          setSuggestions(r.suggestions || []);
        }
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "suggest fetch failed");
        setSuggestions([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
    // tutorMessageId in deps so each new tutor reply triggers a fresh fetch.
  }, [enabled, threadId, profile, tutorMessageId]);

  if (!enabled) return null;
  if (loading) {
    return (
      <div className="mt-2 ml-10 text-xs text-muted italic animate-pulse">
        Generating {profile} suggestions…
      </div>
    );
  }
  if (error) {
    return (
      <div className="mt-2 ml-10 text-xs text-amber-400/70 italic">
        Suggestions unavailable: {error}
      </div>
    );
  }
  if (!suggestions || suggestions.length === 0) return null;

  return (
    <div className="mt-2 ml-10 space-y-1">
      <div className="text-xs text-muted">
        Suggested replies — <span className="font-medium">{profile}</span> profile
      </div>
      <div className="flex flex-wrap gap-2">
        {suggestions.map((s, i) => {
          const cls = COLOR_CLASSES[s.color] || COLOR_CLASSES.muted;
          const label = KIND_LABEL[s.kind] || s.kind;
          return (
            <button
              key={`${tutorMessageId}-${i}`}
              type="button"
              onClick={() => onPick(s.text)}
              title={s.rationale || s.kind}
              className={`text-left text-xs rounded-md border px-3 py-2 max-w-[260px] transition ${cls}`}
            >
              <div className="text-[10px] uppercase tracking-wide opacity-70 mb-1">
                {label}
              </div>
              <div className="text-sm whitespace-pre-wrap leading-snug">
                {s.text}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
