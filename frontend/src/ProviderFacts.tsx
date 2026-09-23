import type { TodayWork } from "./types";
import { formatUiucDateTime } from "./timezone";
import { taskTarget } from "./taskNavigation";
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
      {taskTarget({source_url: row.direct_assignment_url}) && <a href={taskTarget({source_url: row.direct_assignment_url})!} target="_blank" rel="noopener noreferrer">Open provider assignment</a>}
    </div>)}
  </details>;
}
