export type ActionState = { phase: "idle" | "busy" | "success" | "error"; message: string };

// The synchronous gate closes before React renders disabled buttons.
export function createActionRunner(report: (state: ActionState) => void) {
  let running = false;
  return async (label: string, action: () => Promise<unknown>): Promise<boolean> => {
    if (running) return false;
    running = true;
    report({ phase: "busy", message: `${label}…` });
    try {
      const result = await action();
      report(result === false ? { phase: "idle", message: "" } :
        { phase: "success", message: `${label}: completed.` });
      return result !== false;
    } catch (reason) {
      report({ phase: "error", message: `${label}: ${reason instanceof Error ? reason.message : "Unable to complete the action. Please retry."}` });
      return false;
    } finally {
      running = false;
    }
  };
}
