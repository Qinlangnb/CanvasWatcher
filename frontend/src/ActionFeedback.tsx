import { useRef, useState } from "react";
import { createActionRunner, type ActionState } from "./actionRunner";

export function useAsyncAction() {
  const [state, setState] = useState<ActionState>({ phase: "idle", message: "" });
  const runner = useRef<ReturnType<typeof createActionRunner>>();
  if (!runner.current) runner.current = createActionRunner(setState);
  return { ...state, busy: state.phase === "busy", run: runner.current };
}

export function ActionFeedback({ action }: { action: Pick<ActionState, "phase" | "message"> }) {
  if (!action.message) return null;
  return <span className={`action-feedback ${action.phase}`}
    role={action.phase === "error" ? "alert" : "status"} aria-atomic="true">
    {action.phase === "busy" ? <span className="button-spinner" aria-hidden="true" /> : null}
    {action.message}
  </span>;
}
