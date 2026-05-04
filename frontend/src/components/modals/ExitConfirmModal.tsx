/**
 * ExitConfirmModal — M1 explicit-exit confirmation.
 *
 * Triggered when:
 *   1. preflight detects deflection (state.exit_intent_pending=true), OR
 *   2. user clicks the [End session] button in the chat header.
 *
 * Modal shows 2 buttons: [Cancel] / [End session]. End-session always
 * means "no save" — confirmed UX decision (per M1 spec). The static
 * copy here makes that explicit.
 *
 * Per M-FB: this is interface chrome (button labels, modal copy), not
 * templated tutor text — fine to be deterministic strings.
 */
interface ExitConfirmModalProps {
  open: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ExitConfirmModal({ open, onCancel, onConfirm }: ExitConfirmModalProps) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="exit-modal-title"
      onClick={onCancel}
    >
      <div
        className="w-[min(400px,90vw)] rounded-xl bg-panel border border-border shadow-2xl p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="exit-modal-title" className="text-lg font-semibold mb-3">
          End this session?
        </h2>
        <p className="text-sm text-muted mb-5">
          Your conversation won't be saved. You can start a new one anytime
          from My Mastery.
        </p>
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-md border border-border bg-panel text-sm font-medium hover:bg-bg transition"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 rounded-md bg-red-600 text-white text-sm font-medium hover:bg-red-700 transition"
          >
            End session
          </button>
        </div>
      </div>
    </div>
  );
}
