interface TopicCardProps {
  options: string[];
  onSelect: (value: string) => void;
  onSomethingElse?: () => void;
  allowCustom?: boolean;
  endSessionLabel?: string;
  endSessionValue?: string;
}

export function TopicCard({
  options,
  onSelect,
  onSomethingElse,
  allowCustom = true,
  endSessionLabel,
  endSessionValue,
}: TopicCardProps) {
  return (
    <div className="shrink-0 border-t border-border bg-bg">
      <div className="max-w-lane mx-auto px-6 py-4 space-y-2">
        <div className="text-sm text-muted">Choose one focus area to continue:</div>
        <div className="space-y-2">
          {options.map((opt, idx) => (
            <button
              key={`${opt}_${idx}`}
              onClick={() => onSelect(opt)}
              className="w-full rounded-card border border-border bg-panel px-4 py-3 text-left hover:border-accent transition"
            >
              {opt}
            </button>
          ))}
          {allowCustom && onSomethingElse && (
            <button
              onClick={onSomethingElse}
              className="w-full rounded-card border border-dashed border-border bg-panel px-4 py-3 text-left text-muted hover:border-accent transition"
            >
              Something else (type your own topic)
            </button>
          )}
          {endSessionLabel && (
            <button
              onClick={() => onSelect(endSessionValue || endSessionLabel)}
              className="w-full rounded-card border border-border bg-bg px-4 py-3 text-left text-muted hover:border-accent transition"
            >
              {endSessionLabel}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
