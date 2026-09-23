/** The server resolves canonical source bindings; the UI never invents a URL. */
export function taskTarget(task: { source_url?: string | null }): string | null {
  const value = task.source_url?.trim();
  if (!value || /[\x00-\x1f\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    return value;
  } catch { return null; }
}

export function embeddedControl(target: EventTarget | null): boolean {
  return target instanceof Element && !!target.closest('a,button,input,label,select,textarea,summary,details,[role="button"],[role="slider"],[contenteditable="true"],[data-task-control]');
}

export function openTask(task: { source_url?: string | null }): void {
  const target = taskTarget(task);
  if (target) window.open(target, '_blank', 'noopener,noreferrer');
}
