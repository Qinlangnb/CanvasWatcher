import { useEffect, useRef, useState } from "react";
import { downloadAssignmentFile } from "./assignmentDownload";
import type { TodayWork } from "./types";

export function AssignmentFiles({ files = [], notify }: {
  files: TodayWork["attachments"];
  notify: (message: string) => void;
}) {
  const [selected, setSelected] = useState(files[0]?.file_id ?? 0);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const chosen = files.find(file => file.file_id === selected) ?? files[0];
  useEffect(() => () => controller.current?.abort(), []);
  if (!chosen) return null;
  return <div className="assignment-files" data-task-control>
    {files.length > 1 && <select aria-label="Assignment file" value={chosen.file_id}
      disabled={busy} onChange={event => setSelected(Number(event.target.value))}>
      {files.map(file => <option key={file.file_id} value={file.file_id}>{file.filename}</option>)}
    </select>}
    <button className="secondary" disabled={busy} aria-busy={busy}
      title={chosen.filename} onClick={async event => {
        event.stopPropagation();
        if (controller.current) return;
        const request = new AbortController(); controller.current = request;
        setBusy(true);
        try {
          await downloadAssignmentFile(chosen, request.signal);
          if (!request.signal.aborted) notify(`Download started: ${chosen.filename}`);
        } catch (error) {
          if (!request.signal.aborted) notify(error instanceof Error ? error.message : "Assignment download failed.");
        } finally {
          controller.current = null;
          if (!request.signal.aborted) setBusy(false);
        }
      }}>{busy ? "Downloading…" : "Download assignment"}</button>
  </div>;
}
