import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, json } from "./api";
import { ActionFeedback, useAsyncAction } from "./ActionFeedback";
import { setTaskIgnored } from "./taskActions";
import { AIChatDrawer } from "./AIChatDrawer";
import { FilesView, type FileType } from "./FilesView";
import { SettingsView } from "./SettingsView";
import { sourceStatusTone as statusTone } from "./SourceStatusBadge";
import { syncFailureMessage, syncResultFeedback } from "./syncFeedback";
import { TimelineView } from "./timeline";
import { TodayView } from "./TodayView";
import { SubmissionStatus } from "./SubmissionStatus";
import { SourceHealth } from "./SourceHealth";
import type {
  AuthProfile,
  AppNotification,
  CanvasStatus,
  Change,
  ChangeDetail,
  Course,
  CourseTerm,
  FileRecord,
  PendingResource,
  CourseSource,
  SourceConnection,
  Task,
  TodayData,
} from "./types";
import {
  formatDeadline,
  formatUiucDateTime,
  formatUiucHeaderDate,
} from "./timezone";

type Tab = "Today" | "Timeline" | "Courses" | "Changes" | "Files";
const TABS: Tab[] = ["Today", "Timeline", "Courses", "Changes", "Files"];
const tabFromLocation = (path: string, search: string): Tab => {
  if (path.startsWith("/courses")) return "Courses";
  const value = new URLSearchParams(search).get("tab");
  return TABS.includes(value as Tab) ? (value as Tab) : "Today";
};

export default function App() {
  const query = useQueryClient();
  const notificationAction = useAsyncAction();
  const [tab, setTab] = useState<Tab>(() =>
    tabFromLocation(location.pathname, location.search),
  );
  const [path, setPath] = useState(location.pathname);
  const [search, setSearch] = useState(location.search);
  const [selectedChange, setSelectedChange] = useState<number | null>(null);
  const [chat, setChat] = useState(false);
  const [sourcePanel, setSourcePanel] = useState<"canvas" | number | null>(
    null,
  );
  const [sourceAnchor, setSourceAnchor] = useState<HTMLButtonElement | null>(
    null,
  );
  const [toast, setToast] = useState("");
  useEffect(() => {
    const pop = () => {
      setPath(location.pathname);
      setSearch(location.search);
      setTab(tabFromLocation(location.pathname, location.search));
    };
    addEventListener("popstate", pop);
    return () => removeEventListener("popstate", pop);
  }, []);
  useEffect(() => {
    const commitOnEnter = (event: KeyboardEvent) => {
      if (
        event.key !== "Enter" ||
        event.defaultPrevented ||
        event.shiftKey ||
        event.isComposing ||
        event.keyCode === 229 ||
        !(event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement)
      ) return;
      const scope = event.target.closest("form,.settings-panel,.dialog,.used-time-editor,.commute-grid label");
      const button = scope?.querySelector<HTMLButtonElement>(".commit-button:not(:disabled)");
      if (button) { event.preventDefault(); button.click(); }
    };
    addEventListener("keydown", commitOnEnter);
    return () => removeEventListener("keydown", commitOnEnter);
  }, []);
  const navigate = (next: string, replace = false) => {
    if (replace) history.replaceState({}, "", next);
    else history.pushState({}, "", next);
    setPath(location.pathname);
    setSearch(location.search);
    setTab(tabFromLocation(location.pathname, location.search));
  };
  useEffect(() => {
    if (path === "/sources") navigate("/settings/sources", true);
  }, [path]);
  function chooseTab(next: Tab) {
    navigate(next === "Courses" ? "/courses" : `/?tab=${next}`);
  }
  const params = new URLSearchParams(search);
  const fileCourse = params.get("course_id")
    ? Number(params.get("course_id"))
    : null;
  const fileType = (params.get("type") ?? "all") as FileType;
  const courses = useQuery({
    queryKey: ["courses"],
    queryFn: () => api<Course[]>("/api/courses"),
  });
  const archived = useQuery({
    queryKey: ["courses", "archived"],
    queryFn: () => api<Course[]>("/api/courses?archived=true"),
  });
  const terms = useQuery({
    queryKey: ["terms"],
    queryFn: () => api<CourseTerm[]>("/api/courses/terms"),
  });
  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api<Task[]>("/api/tasks"),
  });
  const files = useQuery({
    queryKey: ["files", fileCourse, fileType],
    queryFn: () => {
      const q = new URLSearchParams();
      if (fileCourse) q.set("course_id", String(fileCourse));
      if (fileType !== "all") q.set("type", fileType);
      return api<FileRecord[]>(`/api/files?${q}`);
    },
  });
  const changes = useQuery({
    queryKey: ["changes"],
    queryFn: () => api<Change[]>("/api/changes?limit=100"),
    refetchInterval: 30000,
  });
  const unread = useQuery({
    queryKey: ["changes-unread"],
    queryFn: () => api<{ count: number }>("/api/changes/unread-count"),
    refetchInterval: 30000,
  });
  const today = useQuery({
    queryKey: ["today"],
    queryFn: () => api<TodayData>("/api/today"),
    refetchInterval: 60000,
  });
  const canvas = useQuery({
    queryKey: ["canvas-status"],
    queryFn: () => api<CanvasStatus>("/api/auth/canvas/status"),
  });
  const profiles = useQuery({
    queryKey: ["auth-profiles"],
    queryFn: () => api<AuthProfile[]>("/api/auth/profiles"),
  });
  const connections = useQuery({
    queryKey: ["source-connections"],
    queryFn: () => api<SourceConnection[]>("/api/source-connections"),
  });
  const courseSources = useQuery({
    queryKey: ["sources"],
    queryFn: () => api<CourseSource[]>("/api/sources"),
  });
  const pending = useQuery({
    queryKey: ["pending"],
    queryFn: () => api<PendingResource[]>("/api/auth/pending"),
  });
  const notifications = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api<AppNotification[]>("/api/notifications"),
  });
  const match = path.match(/^\/courses\/(\d+)$/);
  const courseId = match ? Number(match[1]) : null;
  const allCourses = [...(courses.data ?? []), ...(archived.data ?? [])];
  const workspace = courseId
    ? allCourses.find((course) => course.id === courseId)
    : undefined;
  const archivedPage = path === "/courses/archived";
  const managePage = path === "/courses/manage";
  const settingsPage = path.startsWith("/settings")
    ? ((path.split("/")[2] || "general") as
        | "general"
        | "sources"
        | "calendar"
        | "ai")
    : null;
  const courseTasks = useQuery({
    queryKey: ["tasks", "course", courseId],
    queryFn: () =>
      api<Task[]>(`/api/tasks?active_only=false&course_id=${courseId}`),
    enabled: courseId !== null,
  });
  const courseFiles = useQuery({
    queryKey: ["files", "course", courseId],
    queryFn: () => api<FileRecord[]>(`/api/files?course_id=${courseId}`),
    enabled: courseId !== null,
  });
  const courseChanges = useQuery({
    queryKey: ["changes", "course", courseId],
    queryFn: () =>
      api<Change[]>(`/api/changes?course_id=${courseId}&limit=500`),
    enabled: courseId !== null,
  });
  const detail = useQuery({
    queryKey: ["change-detail", selectedChange],
    queryFn: () => api<ChangeDetail>(`/api/changes/${selectedChange}`),
    enabled: selectedChange !== null,
  });
  const sync = useMutation({
    mutationFn: () => api("/api/sync/run", { method: "POST" }),
    onSuccess: () => query.invalidateQueries(),
  });
  const canvasState = canvas.data?.credential_state ?? "AUTH_REQUIRED";
  const otherConnections = (connections.data ?? []).filter(
    (row) => row.source_type !== "canvas",
  );
  const title = workspace
    ? (workspace.display_course_code ?? workspace.course_code)
    : archivedPage
      ? "Archived Courses"
      : managePage
        ? "Manage Courses"
        : settingsPage
          ? settingsPage === "ai"
            ? "AI API Settings"
            : `${settingsPage[0].toUpperCase() + settingsPage.slice(1)} Settings`
          : tab;
  async function openChange(id: number) {
    await api(`/api/changes/${id}/read`, { method: "POST" });
    setSelectedChange(id);
    query.invalidateQueries({ queryKey: ["changes"] });
    query.invalidateQueries({ queryKey: ["changes-unread"] });
    query.invalidateQueries({ queryKey: ["today"] });
  }
  async function restoreCourse(id: number) {
    setToast("");
    try {
      const restored = await api<Course>(`/api/courses/${id}/restore`, {
        method: "POST",
      });
      await query.invalidateQueries();
      setToast(
        `${restored.display_course_code ?? restored.course_code} restored. Reconciliation queued.`,
      );
    } catch (error) {
      setToast(
        error instanceof Error
          ? `Restore failed: ${error.message}`
          : "Restore failed. Course remains archived.",
      );
    }
  }
  async function archiveCourse(id: number) {
    if (!confirm("Archive this course?")) return;
    await api(`/api/courses/${id}/archive`, { method: "POST" });
    setToast("Course archived. Local history and files were preserved.");
    await query.invalidateQueries();
  }
  function toggleSourcePanel(
    next: "canvas" | number,
    anchor: HTMLButtonElement,
  ) {
    if (sourcePanel === next) {
      setSourcePanel(null);
      setSourceAnchor(null);
      return;
    }
    setSourceAnchor(anchor);
    setSourcePanel(next);
  }
  function closeSourcePanel() {
    setSourcePanel(null);
    setSourceAnchor(null);
  }
  function setFileFilters(nextCourse: number | null, nextType: FileType) {
    const q = new URLSearchParams({ tab: "Files" });
    if (nextCourse) q.set("course_id", String(nextCourse));
    if (nextType !== "all") q.set("type", nextType);
    navigate(`/?${q}`);
  }
  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="brand">
          <div className="mark">AW</div>
          <div>
            <strong>Academic Watcher</strong>
            <small>Local study intelligence</small>
          </div>
        </div>
        <nav className="main-nav">
          {TABS.map((item) => (
            <button
              key={item}
              className={
                !settingsPage &&
                !courseId &&
                !archivedPage &&
                !managePage &&
                tab === item
                  ? "active"
                  : ""
              }
              onClick={() => chooseTab(item)}
            >
              <span>
                {
                  (
                    {
                      Today: "◒",
                      Timeline: "↗",
                      Courses: "▤",
                      Changes: "△",
                      Files: "▱",
                    } as Record<Tab, string>
                  )[item]
                }
              </span>
              {item}
              {item === "Changes" && unread.data?.count ? (
                <i>{unread.data.count}</i>
              ) : null}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <small>SOURCES</small>
          <button
            className="sidebar-source"
            aria-expanded={sourcePanel === "canvas"}
            onClick={(event) =>
              toggleSourcePanel("canvas", event.currentTarget)
            }
          >
            <span className={`sidebar-status-dot ${statusTone(canvasState)}`} />
            <span>
              <strong>Canvas API</strong>
              <small>
                  {canvas.data?.verified
                    ? canvas.data.effective_method === "oauth" ? "OAuth"
                    : canvas.data.effective_method === "browser_session"
                      ? `Session${canvas.data.credentials?.pat.state === "EXPIRED" ? " · PAT expired" : ""}`
                      : canvas.data.days_remaining == null ? "PAT · expiry unknown" : `PAT · ${canvas.data.days_remaining} days left`
                  : "Authentication required"}
              </small>
            </span>
          </button>
          <div className="sidebar-source-list">
            {otherConnections.map((site) => (
              <button
                key={site.id}
                className="sidebar-source"
                title={site.name}
                aria-expanded={sourcePanel === site.id}
                onClick={(event) =>
                  toggleSourcePanel(site.id, event.currentTarget)
                }
              >
                <span
                  className={`sidebar-status-dot ${statusTone(site.state)}`}
                />
                <span>
                  <strong>{site.name}</strong>
                  <small>{site.state.replaceAll("_", " ")}</small>
                </span>
              </button>
            ))}
          </div>
          {sourcePanel !== null && sourceAnchor ? (
            <SourceQuickPanel
              canvasStatus={canvas.data}
              anchor={sourceAnchor}
              connection={
                sourcePanel === "canvas"
                  ? connections.data?.find(
                      (row) => row.source_type === "canvas",
                    )
                  : connections.data?.find((row) => row.id === sourcePanel)
              }
              source={
                sourcePanel === "canvas"
                  ? courseSources.data
                      ?.filter((row) => row.source_type === "canvas")
                      .sort((a, b) =>
                        String(b.last_success_at ?? "").localeCompare(
                          String(a.last_success_at ?? ""),
                        ),
                      )[0]
                  : courseSources.data?.find(
                      (row) =>
                        row.id ===
                        Number(
                          (
                            connections.data?.find(
                              (value) => value.id === sourcePanel,
                            )?.config_json ?? {}
                          ).course_source_id,
                        ),
                    )
              }
              status={
                sourcePanel === "canvas"
                  ? canvasState
                  : (connections.data?.find((row) => row.id === sourcePanel)
                      ?.state ?? "UNKNOWN")
              }
              close={closeSourcePanel}
              manage={() => {
                const id =
                  sourcePanel === "canvas"
                    ? connections.data?.find(
                        (row) => row.source_type === "canvas",
                      )?.id
                    : sourcePanel;
                closeSourcePanel();
                navigate(`/settings/sources${id ? `?edit=${id}` : ""}`);
              }}
              sync={async () => {
                const selected = connections.data?.find(row => row.id === sourcePanel);
                if (selected?.source_type === "gradescope" || selected?.source_type === "prairielearn") {
                  const result = await api(`/api/provider-sources/${selected.id}/sync`, { method: "POST", headers: { "X-AW-Broker": "1" } });
                  await query.invalidateQueries();
                  return result;
                } else { return await sync.mutateAsync(); }
              }}
            />
          ) : null}
          <button
            className={`settings-link ${settingsPage ? "active" : ""}`}
            onClick={() => navigate("/settings/general")}
          >
            <span>⚙</span>Settings
          </button>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p className="eyebrow">
              {formatUiucHeaderDate()} · ACADEMIC OVERVIEW
            </p>
            <h1>{title}</h1>
          </div>
          <div className="actions">
            <button className="primary" onClick={() => sync.mutate()}>
              {sync.isPending ? "Syncing…" : "Sync now"}
            </button>
          </div>
        </header>
        <ActionFeedback action={notificationAction} />
        {notifications.data?.map((item) => (
          <div
            className={`global-banner ${item.level}`}
            key={item.id}
            role={item.level === "critical" ? "alert" : "status"}
          >
            <div>
              <strong>{item.title}</strong>
              {item.body && <p>{item.body}</p>}
            </div>
            <button
              aria-label={`Dismiss ${item.title}`}
              disabled={notificationAction.busy}
              onClick={() => { void notificationAction.run("Dismiss notification", async () => {
                await api(`/api/notifications/${item.id}/dismiss`, {
                  method: "POST",
                });
                await query.invalidateQueries({ queryKey: ["notifications"] });
              }); }}
            >
              ×
            </button>
          </div>
        ))}
        {toast ? (
          <div
            className={
              toast.startsWith("Restore failed")
                ? "toast error dismissible"
                : "toast dismissible"
            }
            role="status"
          >
            {toast}
            <button aria-label="Dismiss message" onClick={() => setToast("")}>
              ×
            </button>
          </div>
        ) : null}
        {courseId ? (
          <CourseWorkspace
            course={workspace}
            tasks={courseTasks.data ?? []}
            files={courseFiles.data ?? []}
            changes={courseChanges.data ?? []}
            back={() => navigate("/courses")}
            openChange={openChange}
            refresh={() => query.invalidateQueries()}
          />
        ) : archivedPage ? (
          <ArchivedCourses
            data={archived.data ?? []}
            tasks={tasks.data ?? []}
            files={files.data ?? []}
            open={(id) => navigate(`/courses/${id}`)}
            restore={restoreCourse}
            back={() => navigate("/courses/manage")}
          />
        ) : managePage ? (
          <ManageCourses
            active={courses.data ?? []}
            archived={archived.data ?? []}
            tasks={tasks.data ?? []}
            files={files.data ?? []}
            open={(id) => navigate(`/courses/${id}`)}
            archive={archiveCourse}
            restore={restoreCourse}
          />
        ) : settingsPage ? (
          <SettingsView
            page={settingsPage}
            search={search}
            navigate={navigate}
            courses={courses.data ?? []}
            status={canvas.data}
            profiles={profiles.data ?? []}
            pending={pending.data ?? []}
          />
        ) : (
          <>
            {tab === "Today" && (
              <TodayView data={today.data} openChange={openChange} />
            )}{" "}
            {tab === "Timeline" && (
              <TimelineView
                tasks={tasks.data ?? []}
                courses={courses.data ?? []}
                onCourse={(id) => navigate(`/courses/${id}`)}
              />
            )}{" "}
            {tab === "Courses" && (
              <Courses
                data={courses.data ?? []}
                terms={terms.data ?? []}
                tasks={tasks.data ?? []}
                files={files.data ?? []}
                search={search}
                navigate={navigate}
                open={(id) => navigate(`/courses/${id}`)}
              />
            )}{" "}
            {tab === "Changes" && (
              <Changes
                data={changes.data ?? []}
                open={openChange}
                refresh={() => {
                  query.invalidateQueries({ queryKey: ["changes"] });
                  query.invalidateQueries({ queryKey: ["changes-unread"] });
                  query.invalidateQueries({ queryKey: ["today"] });
                }}
              />
            )}{" "}
            {tab === "Files" && (
              <FilesView
                files={files.data ?? []}
                courses={courses.data ?? []}
                courseId={fileCourse}
                type={fileType}
                onFiltersChange={setFileFilters}
                onChanged={() =>
                  query.invalidateQueries({ queryKey: ["files"] })
                }
              />
            )}
          </>
        )}
      </main>
      <button
        className="chat-launcher"
        aria-label="Open AI Chat"
        onClick={() => setChat(true)}
      >
        ✦
      </button>
      <AIChatDrawer
        open={chat}
        close={() => setChat(false)}
        changed={() => query.invalidateQueries()}
        route={`${path}${search}`}
        courseId={courseId}
      />
      {selectedChange !== null ? (
        <ChangeDialog
          detail={detail.data}
          close={() => setSelectedChange(null)}
        />
      ) : null}
    </div>
  );
}

function SourceQuickPanel({
  canvasStatus,
  anchor,
  connection,
  source,
  status,
  close,
  manage,
  sync,
}: {
  anchor: HTMLButtonElement;
  canvasStatus?: CanvasStatus;
  connection: SourceConnection | undefined;
  source: CourseSource | undefined;
  status: string;
  close: () => void;
  manage: () => void;
  sync: () => Promise<unknown>;
}) {
  const panel = useRef<HTMLElement>(null);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncMessage, setSyncMessage] = useState("");
  const [syncFailed, setSyncFailed] = useState(false);
  const [position, setPosition] = useState<
    { left: number; top: number } | "mobile" | null
  >(null);
  useLayoutEffect(() => {
    const place = () => {
      if (innerWidth <= 820) {
        setPosition("mobile");
        return;
      }
      const bounds = anchor.getBoundingClientRect();
      const width = panel.current?.offsetWidth ?? 350;
      const height = panel.current?.offsetHeight ?? 320;
      setPosition({
        left: Math.max(12, Math.min(bounds.right + 10, innerWidth - width - 12)),
        top: Math.max(12, Math.min(bounds.top, innerHeight - height - 12)),
      });
    };
    place();
    addEventListener("resize", place);
    addEventListener("scroll", place, true);
    return () => {
      removeEventListener("resize", place);
      removeEventListener("scroll", place, true);
    };
  }, [anchor]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => event.key === "Escape" && close();
    const pointer = (event: PointerEvent) => {
      if (panel.current && !panel.current.contains(event.target as Node))
        close();
    };
    addEventListener("keydown", key);
    setTimeout(() => addEventListener("pointerdown", pointer), 0);
    return () => {
      removeEventListener("keydown", key);
      removeEventListener("pointerdown", pointer);
    };
  }, [close]);
  return (
    <section
      ref={panel}
      className="source-quick-panel"
      style={
        position === "mobile"
          ? undefined
          : position
            ? position
            : { visibility: "hidden" }
      }
      role="dialog"
      aria-label={`${connection?.name ?? "Source"} status`}
    >
      <div className="dialog-head">
        <div>
          <p className="eyebrow">SOURCE</p>
          <h3>{connection?.name ?? "Canvas API"}</h3>
        </div>
        <button
          className="icon-button"
          aria-label="Close source details"
          onClick={close}
        >
          ×
        </button>
      </div>
      <dl>
        <div>
          <dt>Status</dt>
          <dd>
            <span className={`sidebar-status-dot ${statusTone(status)}`} />
            {status.replaceAll("_", " ")}
          </dd>
        </div>
        <div>
          <dt>Authentication</dt>
          <dd>
            {connection?.source_type === "website"
              ? String(
                  connection.config_json.authentication_method ??
                    (connection.credential_id ? "NTLM" : "Public"),
                )
              : connection?.source_type === "gradescope" || connection?.source_type === "prairielearn"
                ? "Browser Session"
                : canvasStatus?.effective_method === "browser_session" ? "Browser Session" : canvasStatus?.effective_method === "pat" ? "PAT" : canvasStatus?.effective_method === "oauth" ? "OAuth" : "Not loaded"}
          </dd>
        </div>
        <div>
          <dt>Last sync</dt>
          <dd>
            {source?.last_success_at
              ? formatUiucDateTime(source.last_success_at)
              : "No successful sync yet"}
          </dd>
        </div>
      </dl>
      <SourceHealth source={source} />
      <div className="card-actions">
        <button className="primary" disabled={syncBusy} onClick={async () => {
          if (syncBusy) return;
          setSyncBusy(true); setSyncMessage(""); setSyncFailed(false);
          try {
            const feedback = syncResultFeedback(await sync());
            setSyncMessage(feedback.message); setSyncFailed(feedback.failed);
          }
          catch (error) { setSyncMessage(syncFailureMessage(error)); setSyncFailed(true); }
          finally { setSyncBusy(false); }
        }}>
          {syncBusy ? "Syncing…" : "Sync now"}
        </button>
        <button className="secondary" onClick={manage}>
          Manage
        </button>
      </div>
      {syncMessage && <p role={syncFailed ? "alert" : "status"}>{syncMessage}</p>}
    </section>
  );
}

function Courses({
  data,
  terms,
  tasks,
  files,
  search,
  navigate,
  open,
}: {
  data: Course[];
  terms: CourseTerm[];
  tasks: Task[];
  files: FileRecord[];
  search: string;
  navigate: (path: string, replace?: boolean) => void;
  open: (id: number) => void;
}) {
  const explicit = new URLSearchParams(search).get("term");
  const defaultTerm = terms.find((row) => row.is_default)?.term_id;
  useEffect(() => {
    if (explicit === null && terms.length)
      navigate(
        `/courses?term=${encodeURIComponent(defaultTerm ?? "all")}`,
        true,
      );
  }, [explicit, terms, defaultTerm, navigate]);
  const term = explicit ?? defaultTerm ?? "all";
  const visible =
    term === "all" ? data : data.filter((course) => course.term_id === term);
  return (
    <>
      <div className="toolbar">
        <label>
          Term
          <select
            value={term}
            onChange={(event) =>
              navigate(
                `/courses?term=${encodeURIComponent(event.target.value)}`,
              )
            }
          >
            <option value="all">All active terms</option>
            {terms
              .filter((row) => row.active_course_count)
              .map((row) => (
                <option value={row.term_id} key={row.term_id}>
                  {row.display_name} ({row.active_course_count})
                </option>
              ))}
          </select>
        </label>
        <button
          className="secondary"
          onClick={() => navigate("/courses/manage")}
        >
          Manage courses
        </button>
      </div>
      <div className="course-grid">
        {visible.map((course) => (
          <CourseCard
            key={course.id}
            course={course}
            tasks={tasks}
            files={files}
            open={open}
          />
        ))}
      </div>
      {!visible.length ? (
        <div className="empty">No active courses match this term.</div>
      ) : null}
    </>
  );
}

function CourseCard({
  course,
  tasks,
  files,
  open,
  action,
}: {
  course: Course;
  tasks: Task[];
  files: FileRecord[];
  open: (id: number) => void;
  action?: React.ReactNode;
}) {
  return (
    <article
      className="course-card"
      tabIndex={0}
      onClick={() => open(course.id)}
      onKeyDown={(event) => event.key === "Enter" && open(course.id)}
      title={course.display_name ?? course.name}
    >
      <p className="eyebrow">{course.term_name ?? course.lifecycle_state}</p>
      <h2>{course.display_course_code ?? course.course_code}</h2>
      <div className="course-description">
        <p>{course.display_name ?? course.name}</p>
      </div>
      <div className="course-section">
        {course.section ?? "Section not specified"}
      </div>
      <div className="metrics">
        <span>
          <strong>
            {tasks.filter((task) => task.course_id === course.id).length}
          </strong>
          tasks
        </span>
        <span>
          <strong>
            {files.filter((file) => file.course_id === course.id).length}
          </strong>
          files
        </span>
      </div>
      {action ? (
        <div
          className="course-action"
          onClick={(event) => event.stopPropagation()}
        >
          {action}
        </div>
      ) : null}
    </article>
  );
}

function ManageCourses({
  active,
  archived,
  tasks,
  files,
  open,
  archive,
  restore,
}: {
  active: Course[];
  archived: Course[];
  tasks: Task[];
  files: FileRecord[];
  open: (id: number) => void;
  archive: (id: number) => void;
  restore: (id: number) => void;
}) {
  const [view, setView] = useState<"active" | "archived">("active");
  const rows = view === "active" ? active : archived;
  return (
    <>
      <div className="tabs">
        <button
          className={view === "active" ? "active" : ""}
          onClick={() => setView("active")}
        >
          Active
        </button>
        <button
          className={view === "archived" ? "active" : ""}
          onClick={() => setView("archived")}
        >
          Archived ({archived.length})
        </button>
      </div>
      <p className="management-copy">
        Lifecycle actions live here so the primary Courses grid stays
        navigation-focused.
      </p>
      <div className="course-grid">
        {rows.map((course) => (
          <CourseCard
            key={course.id}
            course={course}
            tasks={tasks}
            files={files}
            open={open}
            action={
              view === "active" ? (
                <button
                  className="secondary"
                  onClick={() => archive(course.id)}
                >
                  Archive
                </button>
              ) : (
                <button className="primary" onClick={() => restore(course.id)}>
                  Restore and reconcile
                </button>
              )
            }
          />
        ))}
      </div>
      {!rows.length ? <div className="empty">No {view} courses.</div> : null}
    </>
  );
}

function ArchivedCourses({
  data,
  tasks,
  files,
  open,
  restore,
  back,
}: {
  data: Course[];
  tasks: Task[];
  files: FileRecord[];
  open: (id: number) => void;
  restore: (id: number) => void;
  back: () => void;
}) {
  return (
    <>
      <div className="toolbar">
        <p>Stored locally and excluded from global workflows and polling.</p>
        <button className="secondary" onClick={back}>
          Course management
        </button>
      </div>
      <div className="course-grid">
        {data.map((course) => (
          <CourseCard
            key={course.id}
            course={course}
            tasks={tasks}
            files={files}
            open={open}
            action={
              <button className="primary" onClick={() => restore(course.id)}>
                Restore and reconcile
              </button>
            }
          />
        ))}
      </div>
      {!data.length ? <div className="empty">No archived courses.</div> : null}
    </>
  );
}

function CourseWorkspace({
  course,
  tasks,
  files,
  changes,
  back,
  openChange,
  refresh,
}: {
  course: Course | undefined;
  tasks: Task[];
  files: FileRecord[];
  changes: Change[];
  back: () => void;
  openChange: (id: number) => void;
  refresh: () => void;
}) {
  const [view, setView] = useState<"timeline" | "tasks" | "files" | "changes">(
    "timeline",
  );
  const [taskScope, setTaskScope] = useState<
    "active" | "completed" | "ignored"
  >("active");
  const [taskError, setTaskError] = useState("");
  const [reopening, setReopening] = useState<number | null>(null);
  const [changingIgnore, setChangingIgnore] = useState<number | null>(null);
  async function changeIgnore(task: Task, ignored: boolean) {
    setChangingIgnore(task.id); setTaskError("");
    try {
      await setTaskIgnored(task.id, ignored);
      await refresh();
    } catch (reason) {
      setTaskError(reason instanceof Error ? reason.message : "Could not update ignored state");
    } finally { setChangingIgnore(null); }
  }
  const scopedTasks = tasks.filter((task) =>
    taskScope === "ignored"
      ? !!task.ignored_at
      : taskScope === "completed"
        ? !task.ignored_at && (task.local_completed || ["COMPLETED", "SUBMITTED", "GRADED", "CANCELLED"].includes(task.status))
        : !task.ignored_at && !task.local_completed && !["COMPLETED", "SUBMITTED", "GRADED", "CANCELLED"].includes(task.status),
  );
  if (!course)
    return (
      <div className="not-found">
        <h2>Course not found</h2>
        <button className="secondary" onClick={back}>
          Back to Courses
        </button>
      </div>
    );
  return (
    <>
      <div className="workspace-head">
        <div>
          <button className="ghost" onClick={back}>
            ← Courses
          </button>
          <p>{course.term_name ?? course.term}</p>
          <h2>{course.display_name ?? course.name}</h2>
        </div>
        <span
          className={`badge ${course.lifecycle_state === "ACTIVE" ? "active" : "warning"}`}
        >
          {course.lifecycle_state}
        </span>
      </div>
      <div className="tabs">
        {(["timeline", "tasks", "files", "changes"] as const).map((value) => (
          <button
            key={value}
            className={view === value ? "active" : ""}
            onClick={() => setView(value)}
          >
            {value[0].toUpperCase() + value.slice(1)}
          </button>
        ))}
      </div>
      {view === "timeline" && (
        <div className="course-timeline-compact">
          <TimelineView
            tasks={tasks.filter((task) => !task.ignored_at)}
            courses={[course]}
            onCourse={() => {}}
          />
        </div>
      )}
      {view === "tasks" && (
        <>
          {taskError && <p role="alert" className="form-message error">{taskError}</p>}
          <div className="tabs compact task-scope-tabs">
            {(["active", "completed", "ignored"] as const).map((scope) => (
              <button
                key={scope}
                className={taskScope === scope ? "active" : ""}
                onClick={() => setTaskScope(scope)}
              >
                {scope[0].toUpperCase() + scope.slice(1)}
              </button>
            ))}
          </div>
          <div className="cards">
            {scopedTasks.map((task) => (
              <article className="card" key={task.id}>
                <div>
                  <h3>{task.title}</h3>
                  <p>{task.status.replaceAll("_", " ")}</p>
                  {task.local_completed && <p>Local work complete · <SubmissionStatus state={task.submission_state} /></p>}
                </div>
                <div className="task-card-actions">
                  <strong>{formatDeadline(task)}</strong>
                  {task.local_completed && <button className="secondary" disabled={reopening !== null} onClick={async () => {
                    setReopening(task.id); setTaskError("");
                    try {
                      const result = await api<{applied: boolean}>(`/api/tasks/${task.id}/progress`, json("POST", {
                        status: "IN_PROGRESS", progress_percent: 99, reopen: true,
                        expected_revision: task.state_revision, action_at: new Date().toISOString(),
                      }));
                      refresh();
                      if (!result.applied) setTaskError("Task changed elsewhere; refreshed its saved state.");
                    } catch (reason) { setTaskError(reason instanceof Error ? reason.message : "Reopen failed"); }
                    finally { setReopening(null); }
                  }}>Reopen local work</button>}
                  {task.ignored_at ? (
                    <button
                      className="secondary"
                      disabled={changingIgnore !== null}
                      onClick={() => changeIgnore(task, false)}
                    >
                      Undo ignore
                    </button>
                  ) : (
                    <button
                      className="ghost"
                      disabled={changingIgnore !== null}
                      onClick={() => changeIgnore(task, true)}
                    >
                      Ignore
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
          {!scopedTasks.length ? (
            <div className="empty compact-empty">No {taskScope} tasks.</div>
          ) : null}
        </>
      )}
      {view === "files" && <FilesView files={files} onChanged={refresh} />}{" "}
      {view === "changes" && (
        <Changes
          data={changes}
          open={openChange}
          refresh={refresh}
          showBulk={false}
        />
      )}
    </>
  );
}

function Changes({
  data,
  open,
  refresh,
  showBulk = true,
}: {
  data: Change[];
  open: (id: number) => void;
  refresh: () => void;
  showBulk?: boolean;
}) {
  const changeAction = useAsyncAction();
  async function state(id: number, isRead: boolean) {
    await api(`/api/changes/${id}/${isRead ? "unread" : "read"}`, {
      method: "POST",
    });
    refresh();
  }
  return (
    <>
      <ActionFeedback action={changeAction} />
      <div className="toolbar">
        <div>
          <h2>Change history</h2>
          <p>
            Academic changes only. Source health and crawler diagnostics stay in
            Sources and logs.
          </p>
        </div>
        {showBulk ? (
          <button
            className="secondary"
            disabled={changeAction.busy}
            onClick={() => { void changeAction.run("Mark all read", async () => {
              await api("/api/changes/read-all", { method: "POST" });
              refresh();
            }); }}
          >
            Mark all read
          </button>
        ) : null}
      </div>
      <div className="cards">
        {data.map((change) => (
          <article
            className={`change-card ${change.read_at ? "" : "unread"}`}
            key={change.id}
          >
            <span className={`semantic ${change.category}`} />
            <span className="unread-slot">
              {!change.read_at ? (
                <span className="unread-dot" aria-label="Unread" />
              ) : null}
            </span>
            <button className="change-main" onClick={() => open(change.id)}>
              <span className={`badge ${change.category}`}>
                {change.category}
              </span>
              <h3>
                {change.primary_text ??
                  change.ai_summary ??
                  change.title ??
                  change.summary}
              </h3>
              <p className="change-secondary">
                {change.event_label ?? change.change_type.replaceAll("_", " ")}{" "}
                · {formatUiucDateTime(change.detected_at)}
              </p>
            </button>
            <button
              className="ghost"
              disabled={changeAction.busy}
              onClick={() => { void changeAction.run(change.read_at ? "Mark unread" : "Mark read", () => state(change.id, !!change.read_at)); }}
            >
              {change.read_at ? "Mark unread" : "Mark read"}
            </button>
          </article>
        ))}
      </div>
    </>
  );
}
function ChangeDialog({
  detail,
  close,
}: {
  detail: ChangeDetail | undefined;
  close: () => void;
}) {
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <section className="dialog" role="dialog" aria-modal="true">
        <div className="dialog-head">
          <div>
            <p className="eyebrow">{detail?.category ?? "CHANGE"}</p>
            <h2>{detail?.primary_text ?? "Loading…"}</h2>
            {detail ? (
              <p className="change-secondary">
                {detail.event_label} · {formatUiucDateTime(detail.detected_at)}
              </p>
            ) : null}
          </div>
          <button className="icon-button" onClick={close} aria-label="Close">
            ×
          </button>
        </div>
        {detail ? (
          <>
            <dl className="detail-list">
              <div>
                <dt>Course</dt>
                <dd>{detail.course?.course_code ?? "Unknown"}</dd>
              </div>
              <div>
                <dt>Event</dt>
                <dd>{detail.event_label}</dd>
              </div>
              <div>
                <dt>Detected</dt>
                <dd>{formatUiucDateTime(detail.detected_at)}</dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd>{detail.source_type ?? "Unknown"}</dd>
              </div>
            </dl>
            {detail.source_url ? (
              <a href={detail.source_url} target="_blank" rel="noreferrer">
                Open source
              </a>
            ) : null}
            {detail.has_meaningful_diff && detail.old && detail.new ? (
              <div className="before-after">
                <Snapshot title="Before" value={detail.old} />
                <Snapshot title="After" value={detail.new} />
              </div>
            ) : null}
            <details className="internal-metadata">
              <summary>Internal metadata</summary>
              <pre>{JSON.stringify(detail.internal_metadata, null, 2)}</pre>
            </details>
          </>
        ) : null}
      </section>
    </div>
  );
}
function Snapshot({
  title,
  value,
}: {
  title: string;
  value: ChangeDetail["old"];
}) {
  return (
    <article>
      <h3>{title}</h3>
      {value ? (
        <>
          <p>{value.normalized_text || "No text value."}</p>
          <details>
            <summary>Raw metadata</summary>
            <pre>{JSON.stringify(value.structured, null, 2)}</pre>
          </details>
        </>
      ) : (
        <p>No snapshot.</p>
      )}
    </article>
  );
}
