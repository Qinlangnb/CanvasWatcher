import { apiFetch } from "./api";

export async function downloadAssignmentFile(
  file: { file_id: number; filename: string },
  signal?: AbortSignal,
) {
  const response = await apiFetch(`/api/files/${file.file_id}/content`, { signal });
  if (!response.ok) {
    throw new Error(response.status === 404
      ? "Assignment file is no longer available. Sync and try again."
      : "Assignment download failed. Please try again.");
  }
  const blob = await response.blob();
  if (signal?.aborted) return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  document.body.appendChild(anchor);
  try { anchor.click(); }
  finally {
    anchor.remove();
    // Keep the object URL alive long enough for the browser to begin saving it.
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}
