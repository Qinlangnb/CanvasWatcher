import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import timeGridPlugin from "@fullcalendar/timegrid";
import interactionPlugin from "@fullcalendar/interaction";
import luxonPlugin from "@fullcalendar/luxon3";
import type { EventApi } from "@fullcalendar/core";
import { DateTime } from "luxon";
import { api, json } from "./api";
import { ActionFeedback, useAsyncAction } from "./ActionFeedback";
import { formatUiucDateTime, UIUC_TIME_ZONE } from "./timezone";
import type { CalendarEvent, Course } from "./types";
import "./calendar.css";

type CalendarView = {
  warnings?: { event: CalendarEvent; code: string; message: string }[];
  instances: { key: string; event: CalendarEvent; start: string; end: string; start_date: string; end_date: string }[];
  layers: { kind: "availability" | "commute" | "short_gap"; start: string; end: string }[];
};
export type EventDraft = { start_at: string; end_at: string; all_day: boolean };
const views = ["timeGridDay", "timeGridWeek", "dayGridMonth"];
const stored = () => {
  try { return JSON.parse(localStorage.getItem("aw-calendar-view") ?? "{}"); }
  catch { return {}; }
};

export function VisualCalendar({ courses, edit, create }: {
  courses: Course[]; edit: (event: CalendarEvent) => void; create: (draft: EventDraft) => void;
}) {
  const ref = useRef<FullCalendar>(null);
  const client = useQueryClient();
  const deleteAction = useAsyncAction();
  const [initial] = useState(stored);
  const [range, setRange] = useState<{ start: string; end: string } | null>(null);
  const [hidden, setHidden] = useState<string[]>([]);
  const [layers, setLayers] = useState(true);
  const [detail, setDetail] = useState<CalendarEvent | null>(null);
  const [occurrence, setOccurrence] = useState<{ start: string; end: string } | null>(null);
  const [focusDate, setFocusDate] = useState("");
  const [error, setError] = useState("");
  const detailCourse = courses.find(course => course.id === detail?.course_id);
  const data = useQuery({
    queryKey: ["calendar-events", "view", range], enabled: !!range,
    queryFn: () => api<CalendarView>(`/api/calendar/view?${new URLSearchParams(range!)}`),
  });
  const sources = [...new Map((data.data?.instances ?? []).map(({ event }) => [
    `${event.source}:${event.calendar_id}`, `${event.source.toUpperCase()} · ${event.calendar_id}`,
  ])).entries()];
  async function refresh() {
    await Promise.all(["calendar-events", "today", "availability"].map(key => client.invalidateQueries({ queryKey: [key] })));
  }
  async function move(event: EventApi, revert: () => void) {
    const source = event.extendedProps.master as CalendarEvent;
    if (source.source !== "manual" || source.read_only || source.recurrence_rule) { revert(); return; }
    try {
      setError("");
      await api(`/api/calendar/events/${source.id}`, json("PUT", {
        summary: source.summary, description: source.description, location: source.location,
        start_at: event.start!.toISOString(), end_at: event.end!.toISOString(),
        all_day: event.allDay, timezone: source.timezone, recurrence_rule: null,
        event_type: source.event_type, course_id: source.course_id,
      }));
      await refresh();
    } catch (reason) { revert(); setError(reason instanceof Error ? reason.message : "Unable to move event"); }
  }
  const eventRows = (data.data?.instances ?? []).filter(({ event }) => !hidden.includes(`${event.source}:${event.calendar_id}`)).map(row => ({
    id: row.key, title: `${row.event.source !== "manual" || row.event.read_only ? "🔒 " : ""}${row.event.summary}`,
    start: row.event.all_day ? row.start_date : row.start, end: row.event.all_day ? row.end_date : row.end,
    allDay: row.event.all_day,
    editable: row.event.source === "manual" && !row.event.read_only && !row.event.recurrence_rule,
    classNames: [`calendar-source-${row.event.source}`], extendedProps: { master: row.event },
  }));
  const background = layers ? (data.data?.layers ?? []).map((row, i) => ({
    id: `layer-${i}`, start: row.start, end: row.end, display: "background",
    classNames: [`calendar-layer-${row.kind}`], editable: false,
  })) : [];
  return <section className="visual-calendar" aria-label="Calendar">
    {!detail && <ActionFeedback action={deleteAction} />}
    <div className="calendar-options">
      <label>Go to date <input type="date" value={focusDate} onChange={e => { if (e.target.value) { setFocusDate(e.target.value); ref.current?.getApi().gotoDate(e.target.value); } }} /></label>
      <span>America/Chicago · 15 minute snap</span>
      <details><summary>Display filters</summary><div className="calendar-filters">
        {sources.map(([id, label]) => <label key={id}><input type="checkbox" checked={!hidden.includes(id)} onChange={e => setHidden(old => e.target.checked ? old.filter(x => x !== id) : [...old, id])} />{label}</label>)}
        <label><input type="checkbox" checked={layers} onChange={e => setLayers(e.target.checked)} />Study windows / commute / short class gaps</label>
        <small>Display only. Hidden events still affect study capacity.</small>
      </div></details>
    </div>
    {(error || data.error) && <p role="alert" className="form-message error">{error || String(data.error)}</p>}
    {data.isFetching && <p role="status">Loading calendar…</p>}
    {data.data?.warnings?.map(warning => <p role="alert" className="form-message error" key={warning.event.id}>
      {warning.event.summary}: {warning.message} {warning.event.source === "manual" && !warning.event.read_only &&
        <button type="button" className="secondary" onClick={() => edit(warning.event)}>Repair event</button>}
    </p>)}
    <FullCalendar ref={ref} plugins={[dayGridPlugin, timeGridPlugin, interactionPlugin, luxonPlugin]}
      initialView={views.includes(initial.view) ? initial.view : "timeGridWeek"}
      initialDate={typeof initial.date === "string" && DateTime.fromISO(initial.date).isValid ? initial.date : undefined}
      timeZone={UIUC_TIME_ZONE} firstDay={1} nowIndicator allDaySlot
      headerToolbar={{ left: "today prev,next", center: "title", right: "timeGridDay,timeGridWeek,dayGridMonth" }}
      buttonText={{ today: "Today", day: "Day", week: "Week", month: "Month" }}
      height={660} slotMinTime="00:00:00" slotMaxTime="24:00:00" scrollTime="08:00:00"
      snapDuration="00:15:00" selectable selectMirror dayMaxEvents={3} eventDisplay="block"
      eventInteractive eventTimeFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
      events={[...eventRows, ...background]}
      datesSet={info => {
        setRange(old => old?.start === info.startStr && old.end === info.endStr ? old : { start: info.startStr, end: info.endStr });
        const date = DateTime.fromJSDate(ref.current?.getApi().getDate() ?? info.view.currentStart, { zone: UIUC_TIME_ZONE }).toISODate() ?? "";
        setFocusDate(date);
        try { localStorage.setItem("aw-calendar-view", JSON.stringify({ view: info.view.type, date })); } catch { /* UI preference only */ }
      }}
      select={info => create({ start_at: info.start.toISOString(), end_at: info.end.toISOString(), all_day: info.allDay })}
      eventClick={info => { if (info.event.extendedProps.master) {
        setDetail(info.event.extendedProps.master);
        setOccurrence({ start: info.event.startStr, end: info.event.endStr });
      } }}
      eventDrop={info => { void move(info.event, info.revert); }}
      eventResize={info => { void move(info.event, info.revert); }}
    />
    {detail && <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && setDetail(null)}>
      <section className="dialog" role="dialog" aria-modal="true" aria-label="Event details" onKeyDown={e => e.key === "Escape" && setDetail(null)} tabIndex={-1}>
        <div className="dialog-head"><h2>{detail.summary}</h2><button autoFocus className="icon-button" aria-label="Close details" onClick={() => setDetail(null)}>×</button></div>
        <p>{detail.source.toUpperCase()} · {detail.source === "manual" && !detail.read_only ? "Editable" : "Read-only · manage through import source"}</p>
        <p>{detail.all_day ? `${occurrence?.start.slice(0, 10) ?? detail.start_date} · All day (ends ${occurrence?.end.slice(0, 10) ?? detail.end_date}, exclusive)` : `${formatUiucDateTime(occurrence?.start ?? detail.start_at)} – ${formatUiucDateTime(occurrence?.end ?? detail.end_at)}`}</p>
        <p>{detailCourse?.display_course_code || detailCourse?.course_code} {detail.location}</p><p>{detail.description}</p>
        {detail.source === "manual" && !detail.read_only && <>
          {detail.recurrence_rule && <p>Recurring series: editing applies to the entire series. Individual occurrence dragging is disabled.</p>}
          <ActionFeedback action={deleteAction} />
          <button className="secondary" disabled={deleteAction.busy} onClick={() => { edit(detail); setDetail(null); }}>{detail.recurrence_rule ? "Edit entire series" : "Edit event"}</button>
          <button className="ghost danger-text" disabled={deleteAction.busy} onClick={() => {
            void deleteAction.run("Delete event", async () => {
              if (!window.confirm(detail.recurrence_rule ? "Delete this entire recurring series?" : "Delete this event?")) return false;
              await api(`/api/calendar/events/${detail.id}`, { method: "DELETE" }); setDetail(null); await refresh();
            });
          }}>{detail.recurrence_rule ? "Delete entire series" : "Delete event"}</button>
        </>}
      </section>
    </div>}
  </section>;
}
