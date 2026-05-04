/**
 * AnchorPickCard — M4 (B6) anchor question picker.
 *
 * Rendered when a session was prelocked from My Mastery and the backend
 * generated 3 anchor question variations. Student picks WHICH angle they
 * want to work on; that variation's question becomes the locked_question.
 *
 * Visual hierarchy per M4 spec:
 *   Subsection (header — same for all cards)
 *   ▸ Question text (one per card, italic-styled to read like a question)
 */
interface AnchorPickCardProps {
  options: string[];
  subsection?: string;
  onSelect: (value: string) => void;
}

export function AnchorPickCard({ options, subsection, onSelect }: AnchorPickCardProps) {
  return (
    <div className="shrink-0 border-t border-border bg-bg">
      <div className="max-w-lane mx-auto px-6 py-4 space-y-3">
        <div className="text-sm text-muted">
          {subsection ? (
            <>
              Pick how you'd like to start{" "}
              <span className="font-medium text-text">{subsection}</span>:
            </>
          ) : (
            "Pick a question to start with:"
          )}
        </div>
        <div className="space-y-2">
          {options.map((opt, idx) => (
            <button
              key={`${idx}_${opt.slice(0, 24)}`}
              onClick={() => onSelect(opt)}
              className="w-full rounded-card border border-border bg-panel px-4 py-3 text-left hover:border-accent transition"
            >
              <div className="text-xs text-muted mb-1">
                Option {idx + 1}
              </div>
              <div className="text-sm italic">{opt}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
