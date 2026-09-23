import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, json } from "./api";
import { CommitButton } from "./CommitButton";
import { ProviderFacts } from "./ProviderFacts";
import { SubmissionStatus } from "./SubmissionStatus";
import { embeddedControl, openTask, taskTarget } from "./taskNavigation";
import type { TodayData, TodayWork, WorkSummary } from "./types";
import { formatTodayDeadline } from "./timezone";

function duration(minutes: number) {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return hours ? `${hours}h ${mins ? `${mins}m` : ""}`.trim() : `${mins}m`;
}
function elapsed(seconds: number) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return `${hours ? `${hours}:` : ""}${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}
export function roundFive(value: number, complete = false) {
  if (complete || value <= 0) return 0;
  return Math.max(5, 5 * Math.round(value / 5));
}
export function localRemaining(
  item: TodayWork,
  progress: number,
  used = item.effective_used_minutes,
) {
  if (progress >= 100) return 0;
  if (used > 0 && progress > 0) {
    const p = progress / 100;
    return roundFive((used * (1 - p)) / p);
  }
  return roundFive(item.effective_total_minutes * (1 - progress / 100));
}

export function TodayView({
  data,
  openChange,
}: {
  data: TodayData | undefined;
  openChange: (id: number) => void;
}) {
  const first = data?.work[0];
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [reopening, setReopening] = useState<number | null>(null);
  return (
    <>
      <section className="hero-card">
        <div>
          <p className="eyebrow">TODAY EXECUTION SIGNAL</p>
          <h2>{first?.title ?? "Everything is clear"}</h2>
          <p>
            {first
              ? `${first.course_code} · ${first.today_reason}`
              : "No active work candidates."}
          </p>
        </div>
        <div className="score">
          <strong>{data?.work.length ?? 0}</strong>
          <span>
            work candidates · {data?.capacity_minutes ?? "—"}m{" "}
            {data?.capacity_source ?? "fallback"} capacity
          </span>
        </div>
      </section>
      {message ? (
        <div className="toast dismissible" role="status">
          {message}
          <button aria-label="Dismiss message" onClick={() => setMessage("")}>
            ×
          </button>
        </div>
      ) : null}
      <section>
        <div className="section-title">
          <div>
            <p className="eyebrow">WORK</p>
            <h2>What to work on, in order</h2>
          </div>
        </div>
        <div className="today-grid">
          {data?.work.map((item) => (
            <WorkCard key={item.task_id} item={item} notify={setMessage} />
          ))}
        </div>
        {data && !data.work.length ? (
          <div className="empty">No work needs to start today.</div>
        ) : null}
      </section>
      <section className="attention-section">
        {data?.completed?.length ? <section>
          <h2>Completed work</h2>
          <div className="cards completed-work-list">
          {data.completed.map(item => <article className="work-card completed-work-card" key={item.task_id}>
            <h3>{taskTarget(item) ? <a href={taskTarget(item)!} target="_blank" rel="noopener noreferrer">{item.title}</a> : item.title}</h3>
            {item.source_link_kind === "course_page" ? <small>Original course page</small> : null}
            <p>{item.today_reason} · {item.local_completed ? "Local work complete" : "Completed upstream"}</p>
            <p><SubmissionStatus state={item.submission_state} /> · Local completion does not submit to any provider</p>
            <ProviderFacts facts={item.provider_facts} />
            {item.local_completed && <button className="secondary" disabled={reopening !== null} onClick={async () => {
              setReopening(item.task_id); setMessage("");
              try {
              const result = await api<{applied: boolean}>(`/api/tasks/${item.task_id}/progress`, json("POST", {
                status: "IN_PROGRESS", progress_percent: 99, reopen: true,
                expected_revision: item.state_revision, action_at: new Date().toISOString(),
              }));
              await Promise.all([["today"], ["tasks"], ["active-work"]].map(queryKey => queryClient.invalidateQueries({queryKey})));
              if (!result.applied) setMessage("Task changed elsewhere; refreshed its saved state.");
              } catch (reason) { setMessage(String(reason)); }
              finally { setReopening(null); }
            }}>Reopen</button>}
          </article>)}
          </div>
        </section> : null}
        <div className="section-title">
          <div>
            <p className="eyebrow">ATTENTION</p>
            <h2>Changes that may need action</h2>
          </div>
        </div>
        <div className="cards">
          {data?.attention.map((item) => (
            <article className="attention-card" key={item.change_id}>
              <div>
                <span className={`badge ${item.severity}`}>
                  {item.severity}
                </span>
                <p className="eyebrow">{item.course_code}</p>
                <h3>{item.summary}</h3>
                {item.ai_reviewed && <small>AI reviewed{item.reviewed_at ? ` · ${new Date(item.reviewed_at).toLocaleString()}` : ""}</small>}
                <p>{item.recommended_action ?? "Review this change."}</p>
              </div>
              <button
                className="secondary"
                onClick={() => openChange(item.change_id)}
              >
                Review
              </button>
            </article>
          ))}
        </div>
        {data && !data.attention.length ? (
          <div className="empty">No actionable changes.</div>
        ) : null}
      </section>
    </>
  );
}

function WorkCard({
  item,
  notify,
}: {
  item: TodayWork;
  notify: (message: string) => void;
}) {
  const query = useQueryClient();
  const [progress, setProgress] = useState(item.progress);
  const [used, setUsed] = useState(String(item.effective_used_minutes));
  const [clock, setClock] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [working, setWorking] = useState(item.is_working);
  const [startedAt, setStartedAt] = useState(item.active_started_at);
  const savedProgress = useRef(item.progress);
  const revision = useRef(item.state_revision ?? 0);
  const actionAt = useRef(new Date().toISOString());
  const dragging = useRef(false);
  const gesture = useRef({ x: 0, y: 0, blocked: false });
  const saving = useRef<Promise<void> | null>(null);
  const progressRef = useRef(item.progress);
  const failures = useRef(0);
  const warningShown = useRef(false);
  useEffect(() => {
    setProgress(item.progress);
    savedProgress.current = item.progress;
    revision.current = item.state_revision ?? 0;
  }, [item.progress, item.state_revision]);
  useEffect(() => {
    progressRef.current = progress;
  }, [progress]);
  useEffect(
    () => setUsed(String(item.effective_used_minutes)),
    [item.effective_used_minutes],
  );
  useEffect(() => {
    setWorking(item.is_working);
    setStartedAt(item.active_started_at);
  }, [item.is_working, item.active_started_at]);
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [working]);
  const liveSeconds =
    working && startedAt
      ? Math.max(0, Math.floor((clock - new Date(startedAt).getTime()) / 1000))
      : item.active_elapsed_seconds;
  const effectiveUsed =
    item.effective_used_minutes +
    (working
      ? Math.max(
          0,
          Math.round((liveSeconds - item.active_elapsed_seconds) / 60),
        )
      : 0);
  const remaining = localRemaining(item, progress, effectiveUsed);
  async function refresh() {
    await Promise.all(
      [["today"], ["active-work"], ["tasks"]].map((queryKey) =>
        query.invalidateQueries({ queryKey }),
      ),
    );
  }
  async function saveProgress(value = progress, refreshAfter = true) {
    if (saving.current) await saving.current;
    if (value === savedProgress.current) return;
    const persist = async () => {
    const result = await api<{state_revision: number; applied: boolean}>(
      `/api/tasks/${item.task_id}/progress`,
      json("POST", {
        status:
          value >= 100
            ? "READY_TO_SUBMIT"
            : value > 0
              ? "IN_PROGRESS"
              : "NOT_STARTED",
        progress_percent: value,
        expected_revision: revision.current,
        action_at: actionAt.current,
      }),
    );
    revision.current = result.state_revision;
    if (!result.applied) { await refresh(); throw new Error("Task changed elsewhere; refreshed the saved state."); }
    savedProgress.current = value;
    if (value === 100) { setWorking(false); setStartedAt(null); }
    if (refreshAfter) await refresh();
    };
    const pending = persist();
    saving.current = pending;
    try { await pending; } finally { if (saving.current === pending) saving.current = null; }
  }
  useEffect(() => {
    if (working || progress === savedProgress.current) return;
    const timer = window.setInterval(
      () => {
        if (dragging.current || saving.current) return;
        void saveProgress(progressRef.current).catch(() =>
          notify(
            "Progress is still local; synchronization will keep retrying.",
          ),
        );
      },
      5000,
    );
    return () => window.clearInterval(timer);
  }, [progress, working]);
  useEffect(() => {
    if (!working) return;
    const heartbeat = async () => {
      if (dragging.current || saving.current) return;
      const seconds = startedAt
        ? Math.max(
            0,
            Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000),
          )
        : item.active_elapsed_seconds;
      const remainingNow = localRemaining(
        item,
        progressRef.current,
        item.effective_used_minutes +
          Math.max(0, Math.round((seconds - item.active_elapsed_seconds) / 60)),
      );
      try {
        await saveProgress(progressRef.current, false);
        if (progressRef.current === 100) { await refresh(); return; }
        await api(
          `/api/tasks/${item.task_id}/work/heartbeat`,
          json("POST", {
            expected_revision: revision.current,
            elapsed_snapshot_seconds: seconds,
            remaining_minutes: remainingNow,
            client_timestamp: new Date().toISOString(),
          }),
        );
        if (warningShown.current) {
          notify("Connection restored. Work progress is synchronized.");
          warningShown.current = false;
        }
        failures.current = 0;
      } catch {
        failures.current += 1;
        if (failures.current >= 3 && !warningShown.current) {
          warningShown.current = true;
          notify(
            "Connection interrupted. Your timer and edits remain active locally; synchronization will keep retrying.",
          );
        }
      }
    };
    const timer = window.setInterval(() => void heartbeat(), 5000);
    return () => window.clearInterval(timer);
  }, [
    working,
    item.task_id,
    startedAt,
    item.active_elapsed_seconds,
    item.effective_used_minutes,
  ]);
  async function start() {
    const previousStarted = startedAt;
    setWorking(true);
    setStartedAt(new Date().toISOString());
    setClock(Date.now());
    setBusy(true);
    try {
      const result = await api<WorkSummary>(
        `/api/tasks/${item.task_id}/work/start`,
        { method: "POST" },
      );
      setStartedAt(result.active_started_at);
      notify(
        result.switched_from_task_id
          ? "Previous timer stopped; this task is now active."
          : "Work timer started.",
      );
      await refresh();
    } catch (error) {
      setWorking(false);
      setStartedAt(previousStarted);
      notify(
        error instanceof Error ? error.message : "Work timer could not start.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    const previousStarted = startedAt;
    setWorking(false);
    setBusy(true);
    try {
      await saveProgress(progress, false);
      await api(
        `/api/tasks/${item.task_id}/work/heartbeat`,
        json("POST", {
          expected_revision: revision.current,
          elapsed_snapshot_seconds: liveSeconds,
          remaining_minutes: remaining,
          client_timestamp: new Date().toISOString(),
        }),
      );
      await api(`/api/tasks/${item.task_id}/work/stop`, { method: "POST" });
      notify("Work timer stopped and saved.");
      setStartedAt(null);
      await refresh();
    } catch (error) {
      setWorking(true);
      setStartedAt(previousStarted);
      notify(
        error instanceof Error
          ? error.message
          : "Work timer could not stop; synchronization will retry.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function saveUsed() {
    const value = Math.max(0, Math.round(Number(used) || 0));
    await api(
      `/api/tasks/${item.task_id}/time-used`,
      json("PATCH", { total_minutes: value }),
    );
    notify("Used time corrected without rewriting session history.");
    await refresh();
  }
  return (
    <article className={`work-card task-interactive ${taskTarget(item) ? "has-task-target" : ""} ${working ? "working-on" : ""}`}
      onPointerDownCapture={event => {
        gesture.current = { x: event.clientX, y: event.clientY,
          blocked: event.button !== 0 || embeddedControl(event.target) };
      }}
      onPointerMoveCapture={event => {
        if (event.buttons && Math.hypot(event.clientX - gesture.current.x, event.clientY - gesture.current.y) > 6) gesture.current.blocked = true;
      }}
      onPointerCancel={() => { gesture.current.blocked = true; dragging.current = false; }}
      onClick={event => {
      if (gesture.current.blocked || dragging.current || embeddedControl(event.target) || window.getSelection()?.toString()) return;
      openTask(item);
    }}>
      <div className="work-state-row">
        <span className={`working-label ${working ? "visible" : ""}`}>
          {working ? "WORKING ON" : "READY"}
        </span>
        {working ? (
          <strong className="live-timer" aria-live="off">
            {elapsed(liveSeconds)}
          </strong>
        ) : null}
      </div>
      <div className="work-card-head">
        <div>
          <p className="eyebrow">{item.course_code}</p>
          <h3 className="task-title">{taskTarget(item) ? (
            <a href={taskTarget(item)!} target="_blank" rel="noopener noreferrer">
              {item.title}
            </a>
          ) : item.title}</h3>
          {item.source_link_kind === "course_page" ? <small>Original course page</small> : null}
          <ProviderFacts facts={item.provider_facts} />
          {!item.source_url ? <a href={`/courses/${item.course_id}`}>Course details</a> : null}
        </div>
        <span className={`priority ${item.priority_bucket.toLowerCase()}`}>
          {item.priority_bucket}
        </span>
      </div>
      <dl>
        <div>
          <dt>Deadline</dt>
          <dd>
            {formatTodayDeadline(item)}
          </dd>
        </div>
        <div>
          <dt>Remaining</dt>
          <dd>
            {duration(remaining)} ·{" "}
            {effectiveUsed > 0 && progress > 0
              ? "observed ratio"
              : item.estimate_source.replaceAll("_", " ")}
          </dd>
        </div>
        <div>
          <dt>Used time</dt>
          <dd className="used-time-editor">
            <input
              aria-label={`Used minutes for ${item.title}`}
              type="number"
              min="0"
              step="5"
              value={used}
              onChange={(event) => setUsed(event.target.value)}
            />
            <CommitButton
              label="Save used time"
              dirty={
                Math.max(0, Math.round(Number(used) || 0)) !==
                item.effective_used_minutes
              }
              onCommit={saveUsed}
            />
          </dd>
        </div>
      </dl>
      <label className="progress-control">
        <span>
          Progress <strong>{Math.round(progress)}%</strong>
        </span>
        <input
          type="range"
          min="0"
          max="100"
          step="1"
          value={progress}
          aria-label={`Progress for ${item.title}`}
          onChange={(event) => { actionAt.current = new Date().toISOString(); progressRef.current = Number(event.target.value); setProgress(progressRef.current); }}
          onPointerDown={() => { dragging.current = true; }}
            onPointerCancel={() => { dragging.current = false; }}
            onLostPointerCapture={() => { dragging.current = false; }}
          onPointerUp={(event) => { dragging.current = false; actionAt.current = new Date().toISOString(); if(Number(event.currentTarget.value) === 100) void saveProgress(100).catch(() => notify("Completion not saved; please retry.")); }}
          onKeyUp={(event) => { if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) void saveProgress(Number(event.currentTarget.value)).catch(reason => notify(String(reason))); }}
        />
      </label>
      <div className="work-actions">
        {working ? (
          <button className="secondary" onClick={stop}>
            Stop
          </button>
        ) : (
          <button className="primary" onClick={start}>
            Start
          </button>
        )}
      </div>
      <p className="today-reason">{item.today_reason}</p>
    </article>
  );
}
