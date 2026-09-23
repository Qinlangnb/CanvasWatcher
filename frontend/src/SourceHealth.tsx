import type { CourseSource } from "./types";

export function SourceHealth({ source }: { source?: CourseSource }) {
  return <>
    {source?.health_summary && <p className="form-message">{source.health_summary}</p>}
    {!!source?.health_diagnostics?.length && <details className="source-diagnostics"><summary>Advanced resource diagnostics</summary><ul>
      {source.health_diagnostics.map((detail, index) => <li key={index}>{detail.resource}: {detail.code} · HTTP {detail.http_status ?? "unknown"}{detail.historical ? " · historical record" : ""}</li>)}
    </ul></details>}
  </>;
}
