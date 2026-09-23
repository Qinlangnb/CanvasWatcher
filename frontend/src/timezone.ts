export const UIUC_TIME_ZONE = "America/Chicago";

function instant(value: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  return new Date(hasZone ? value : `${value}Z`);
}

export function formatUiucDateTime(value: string | null): string {
  if (!value) return "No deadline";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: UIUC_TIME_ZONE,
  }).format(instant(value));
}

export function formatUiucTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: UIUC_TIME_ZONE,
  }).format(instant(value));
}

export function formatLocalDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}

export function formatUiucHeaderDate(): string {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
    timeZone: UIUC_TIME_ZONE,
  }).format(new Date()).toUpperCase();
}

export function formatDeadline(task: {
  due_at: string | null;
  due_date_local: string | null;
  deadline_precision: string | null;
}): string {
  if (task.deadline_precision === "DATE_ONLY" && task.due_date_local) {
    return `Due ${formatLocalDate(task.due_date_local)} · Time not specified by source`;
  }
  return task.due_at ? `Due ${formatUiucDateTime(task.due_at)}` : "No deadline";
}

export function formatTodayDeadline(item: {
  deadline: string | null;
  deadline_precision: string | null;
  due_date_local?: string | null;
}): string {
  return formatDeadline({
    due_at: item.deadline_precision === "DATE_ONLY" ? null : item.deadline,
    due_date_local: item.due_date_local ?? null,
    deadline_precision: item.deadline_precision,
  });
}
