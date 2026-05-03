interface OptInCardProps {
  options: string[];
  onSelect: (value: string) => void;
  label?: string;
}

export function OptInCard({ options, onSelect, label = "Apply this concept:" }: OptInCardProps) {
  return (
    <div className="shrink-0 border-t border-border bg-bg">
      <div className="max-w-lane mx-auto px-6 py-4 space-y-2">
        <div className="text-sm text-muted">{label}</div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {options.map((opt, idx) => (
            <button
              key={`${opt}_${idx}`}
              onClick={() => onSelect(opt)}
              className="rounded-card border border-border bg-panel px-4 py-3 text-left hover:border-accent transition"
            >
              {opt}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
