/** In-memory sample data for the preview login. No request reaches the API. */
import type { Change, Course, FileRecord, Task, TodayData, TodayWork } from "./types";

const day = (offset: number) => new Date(Date.now() + offset * 86_400_000).toISOString();
const date = (offset: number) => day(offset).slice(0, 10);
const courses: Course[] = [
  { id: 101, course_code: "BIO 101", name: "Sample Biology", term: "Fall 2026", lifecycle_state: "ACTIVE", archived_at: null, term_id: "demo-fall", term_name: "Fall 2026", term_start_at: null, term_end_at: null, term_sort_key: "2026-fall", display_course_code: "BIO 101", display_name: "Sample Biology", section: null, commute_minutes: 0 },
  { id: 102, course_code: "HIST 204", name: "Sample World History", term: "Fall 2026", lifecycle_state: "ACTIVE", archived_at: null, term_id: "demo-fall", term_name: "Fall 2026", term_start_at: null, term_end_at: null, term_sort_key: "2026-fall", display_course_code: "HIST 204", display_name: "Sample World History", section: null, commute_minutes: 0 },
  { id: 103, course_code: "CS 150", name: "Sample Programming", term: "Fall 2026", lifecycle_state: "ACTIVE", archived_at: null, term_id: "demo-fall", term_name: "Fall 2026", term_start_at: null, term_end_at: null, term_sort_key: "2026-fall", display_course_code: "CS 150", display_name: "Sample Programming", section: null, commute_minutes: 0 },
];
const task = (id: number, course_id: number, title: string, task_type: string, days: number): Task => ({
  id, course_id, title, task_type, due_at: day(days), assigned_date_local: date(-2),
  due_date_local: date(days), deadline_precision: "datetime", deadline_timezone: "America/Chicago",
  source_deadline_text: null, points_possible: 100, status: "OPEN", ignored_at: null,
  local_completed: false, local_completed_at: null, completion_origin: null,
  state_revision: 1, submission_state: null, source_url: null, source_link_kind: "unavailable",
});
const tasks: Task[] = [
  task(201, 101, "Sample lab report", "assignment", 2),
  task(202, 102, "Sample reading reflection", "assignment", 4),
  task(203, 103, "Sample coding exercise", "assignment", 6),
  task(204, 101, "Sample quiz review", "quiz", 8),
];
const file = (id: number, course: Course, filename: string, type: string): FileRecord => ({
  id, course_id: course.id, course_code: course.course_code, course_name: course.name,
  source_url: "", original_filename: filename, local_path: "", category: type,
  source_detected_type: type, user_override_type: null, effective_type: type,
  size_bytes: id * 1024, mime_type: "application/pdf", version_number: 1,
  downloaded_at: day(-2), integrity_status: "ok", validation_error: null,
  export_name: null, export_path: null, export_status: "not_configured", export_error: null,
  source_deleted: false,
});
const files: FileRecord[] = [
  file(301, courses[0], "Sample_Lecture_01.pdf", "lecture"),
  file(302, courses[1], "Sample_Reading_Guide.pdf", "reading"),
  file(303, courses[2], "Sample_Homework_Instructions.pdf", "homework"),
];
const changes: Change[] = [
  { id: 401, source_item_id: 401, change_type: "new_announcement", detected_at: day(-1), importance: "medium", summary: "Sample announcement: office hours updated", ai_summary: null, read_at: null, category: "announcement", change_field: null, course_code: "BIO 101", title: "Sample office hours", primary_text: "Example announcement for the preview account.", event_label: "New announcement", has_meaningful_diff: false },
  { id: 402, source_item_id: 402, change_type: "new_file", detected_at: day(-2), importance: "low", summary: "Sample lecture file added", ai_summary: null, read_at: null, category: "file", change_field: null, course_code: "CS 150", title: "Sample_Lecture_01.pdf", primary_text: "Example file activity for the preview account.", event_label: "New file", has_meaningful_diff: false },
];
const work = (item: Task): TodayWork => ({
  task_id: item.id, course_id: item.course_id,
  course_code: courses.find((course) => course.id === item.course_id)!.course_code,
  title: item.title, deadline: item.due_at, deadline_precision: "datetime",
  due_date_local: item.due_date_local, progress: 0, effective_total_minutes: 90,
  remaining_minutes: 90, estimate_source: "fallback", estimate_confidence: 0,
  analysis_status: "READY", priority_score: 50, priority_bucket: "Medium",
  today_reason: "Sample task for interface preview", slack_minutes: null,
  slack_bucket: "ample", tracked_minutes: 0, manual_adjustment_minutes: 0,
  effective_used_minutes: 0, is_working: false, active_started_at: null,
  active_elapsed_seconds: 0, local_completed: false, submission_state: null,
  source_url: null, source_link_kind: "unavailable", state_revision: 1,
});
const today = (): TodayData => ({
  date: date(0), timezone: "America/Chicago", work: tasks.slice(0, 3).map(work),
  completed: [], attention: [], capacity_minutes: 180, capacity_source: "fallback",
});

function sample(path: string): unknown | undefined {
  const url = new URL(path, "https://preview.invalid");
  const route = url.pathname.replace(/^\/watcher/, "");
  const courseId = Number(url.searchParams.get("course_id"));
  if (route === "/api/courses/terms") return [{ term_id: "demo-fall", display_name: "Fall 2026", active_course_count: 3, archived_course_count: 0, is_default: true }];
  if (route === "/api/courses") return url.searchParams.get("archived") === "true" ? [] : courses;
  if (/^\/api\/courses\/\d+$/.test(route)) return courses.find((row) => row.id === Number(route.split("/").at(-1))) ?? null;
  if (route === "/api/tasks") return courseId ? tasks.filter((row) => row.course_id === courseId) : tasks;
  if (route === "/api/files") {
    const type = url.searchParams.get("type");
    return files.filter((row) => (!courseId || row.course_id === courseId) && (!type || type === "all" || row.effective_type === type));
  }
  if (route === "/api/changes/unread-count") return { count: changes.length };
  if (route === "/api/changes") return courseId ? changes.filter((row) => courses.find((course) => course.id === courseId)?.course_code === row.course_code) : changes;
  if (/^\/api\/changes\/\d+$/.test(route)) {
    const row = changes.find((item) => item.id === Number(route.split("/").at(-1)));
    return row ? { ...row, course: courses.find((course) => course.course_code === row.course_code) ?? null, source_type: "preview", source_url: null, requires_action: false, old: null, new: null, new_file: null, related_task: null, internal_metadata: {} } : null;
  }
  if (route === "/api/today") return today();
  if (route === "/api/notifications" || route === "/api/sources" || route === "/api/source-connections" || route === "/api/auth/profiles" || route === "/api/auth/pending" || route === "/api/calendar/events" || route === "/api/calendar/availability" || route === "/api/calendar/availability/overrides" || route === "/api/calendar/ics/imports" || route === "/api/provider-sources") return [];
  if (route === "/api/calendar/view") return { instances: [], layers: [], warnings: [] };
  if (route === "/api/settings/general") return { academic_timezone: "America/Chicago" };
  if (route === "/api/settings/change-filters") return { rules: [], recent_reviews: [] };
  if (route === "/api/settings/credential-storage") return { mode: "memory" };
  if (route === "/api/auth/canvas/status") return { configured: false, auth_mode: null, credential_state: "AUTH_REQUIRED", source_health: "NOT_CONFIGURED", verified: false, verified_at: null, account_id: null, account_display_name: null, expires_at: null, days_remaining: null, expiration_state: "unknown", expiration_source: null };
  if (route === "/api/auth/broker/configuration") return { available: false };
  if (route === "/api/settings/ai") return { provider: "disabled", providers: [], needs_provider_selection: true, base_url: "", model_id: "", credential_loaded: false, connected_to_live_ai: false, calendar_chat_ready: false, chat_ready: false };
  if (route === "/api/integrations/google-calendar/status") return { configured: false, state: "NOT_CONFIGURED", connected_account: null, scopes: [], last_successful_sync: null, last_error: null, calendars: [] };
  if (route === "/api/sync/status") return { running: false, last_success_at: null, last_error: null };
  return undefined;
}

export function previewResponse(path: string, method: string): Response {
  if (method !== "GET") return Response.json({ detail: "PREVIEW_READ_ONLY" }, { status: 403 });
  const data = sample(path);
  return data === undefined
    ? Response.json({ detail: "PREVIEW_UNAVAILABLE" }, { status: 404 })
    : Response.json(data, { headers: { "Cache-Control": "no-store" } });
}
