import { ActionFeedback, useAsyncAction } from "./ActionFeedback";

export function CommitButton({ dirty, onCommit, label = "Save", disabled = false }: {
  dirty: boolean; onCommit: () => Promise<void>; label?: string; disabled?: boolean;
}) {
  const action = useAsyncAction();
  const title = action.busy ? "Saving" : action.phase === "error" ? `Retry ${label}` : label;
  return <span className="commit-control">
    <button type="button" className={`primary commit-button ${action.busy ? "saving" : action.phase === "success" ? "saved" : action.phase}`}
      aria-label={title} title={title} aria-busy={action.busy}
      disabled={disabled || !dirty || action.busy}
      onClick={() => { if (!disabled && dirty) void action.run(label, onCommit); }}>
      {action.busy ? <span className="button-spinner" aria-hidden="true" /> : action.phase === "error" ? "!" : "✓"}
    </button>
    <ActionFeedback action={action} />
  </span>;
}
