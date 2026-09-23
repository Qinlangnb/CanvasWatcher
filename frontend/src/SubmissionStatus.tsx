export function submissionStatus(state?: string | null) {
  const normalized = state?.toLowerCase();
  if (normalized === "graded") return { tone: "positive", icon: "✓", label: "Graded" };
  if (normalized === "submitted") return { tone: "positive", icon: "✓", label: "Submission confirmed" };
  return { tone: "unconfirmed", icon: "○", label: "Submission not confirmed" };
}

export function SubmissionStatus({ state }: { state?: string | null }) {
  const status = submissionStatus(state);
  return <span className={`submission-status ${status.tone}`}><span aria-hidden="true">{status.icon}</span> {status.label}</span>;
}
