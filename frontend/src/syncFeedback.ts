const messages: Record<string, string> = {
  AUTH_REQUIRED: "Sign-in required. Open Manage and reconnect this source.",
  PROVIDER_COURSE_MAPPING_REQUIRED: "Open Manage and map at least one course before syncing.",
  PROVIDER_TASK_MAPPING_REQUIRED: "Open Manage and confirm the matching assignments before syncing.",
  PRAIRIELEARN_PARSER_NOT_VERIFIED: "PrairieLearn course syncing is not available yet; browser sign-in can be tested separately.",
  PERMISSION_DENIED: "This account does not have permission to read the requested course.",
};

export function syncFailureMessage(error: unknown): string {
  return messages[error instanceof Error ? error.message : ""]
    ?? "Sync could not finish. Open Manage to check source status, sign-in and course mapping.";
}

export function syncResultFeedback(result: unknown): { failed: boolean; message: string } {
  const value = result && typeof result === "object" ? result as { errors?: unknown; state?: unknown } : {};
  const errors = value.errors;
  const hasErrors = Array.isArray(errors) ? errors.length > 0
    : errors && typeof errors === "object" ? Object.keys(errors).length > 0 : Boolean(errors);
  if (hasErrors || ["FAILED", "DEGRADED", "PARTIAL", "PARTIAL_SUCCESS", "AUTH_REQUIRED"].includes(String(value.state).toUpperCase())) {
    return { failed: true, message: "Sync finished with errors. Some items could not be verified; open Manage to review source status." };
  }
  return { failed: false, message: "Sync completed successfully." };
}
