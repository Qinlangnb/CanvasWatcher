import type { TodayWork } from "./types";
import { formatUiucDateTime } from "./timezone";
import { SubmissionStatus } from "./SubmissionStatus";

export function ProviderFacts({ facts }: { facts: TodayWork["provider_facts"] }) {
  if (!facts?.length) return null;
  const dates = new Set(facts.filter(row => row.due_at).map(row => Date.parse(row.due_at!)));
  return <details className="provider-facts"><summary>Source facts{dates.size > 1 ? " · deadlines differ" : ""}</summary>
    {facts.map(row => <div key={row.source_item_id}>
      <strong>{row.provider === "gradescope" ? "Gradescope" : row.provider === "canvas" ? "Canvas" : "PrairieLearn"}</strong>
      <p><SubmissionStatus state={row.submission_state} /></p>
      <p>Due: {row.due_at ? formatUiucDateTime(row.due_at) : "unknown"}</p>
      {row.late_due_at && <p>Late cutoff: {formatUiucDateTime(row.late_due_at)}</p>}
      <p className="assignment-description-summary"><span>AI summary: </span>{row.description_summary || ({
        unavailable: "No assignment description is available.",
        needs_connection: "Test the selected AI connection in Settings to generate a summary.",
        failed: "AI summary unavailable. The background generation failed.",
        pending: "Generating assignment summary…",
        ready: "AI summary unavailable.",
      }[row.description_summary_status || "pending"])}</p>
    </div>)}
  </details>;
}
