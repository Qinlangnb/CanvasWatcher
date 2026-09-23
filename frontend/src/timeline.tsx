import { useEffect, useMemo, useRef, useState } from "react";
import type { Course, Task } from "./types";
import { formatDeadline, UIUC_TIME_ZONE } from "./timezone";
import { openTask, taskTarget } from "./taskNavigation";
import { submissionStatus } from "./SubmissionStatus";

const DAY = 86_400_000;
const AXIS_START = 180;
const DAY_WIDTH = 58;
const MIN_TICK_SPACING = 68;
type DateParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
};
function zonedParts(value: Date, timeZone = UIUC_TIME_ZONE): DateParts {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(value);
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((part) => part.type === type)?.value ?? 0);
  return {
    year: get("year"),
    month: get("month"),
    day: get("day"),
    hour: get("hour"),
    minute: get("minute"),
    second: get("second"),
  };
}
function zonedMidnight(
  year: number,
  month: number,
  day: number,
  timeZone = UIUC_TIME_ZONE,
): Date {
  const rough = new Date(Date.UTC(year, month - 1, day));
  const parts = zonedParts(rough, timeZone);
  const represented = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
  );
  return new Date(rough.getTime() - (represented - rough.getTime()));
}
function shiftedCalendarDate(parts: DateParts, days: number) {
  const shifted = new Date(
    Date.UTC(parts.year, parts.month - 1, parts.day + days),
  );
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}
function dueDate(task: Task): Date | null {
  if (task.due_at) return new Date(task.due_at);
  if (!task.due_date_local) return null;
  const [y, m, d] = task.due_date_local.split("-").map(Number);
  return new Date(
    zonedMidnight(y, m, d).getTime() + 23 * 60 * 60 * 1000 + 59 * 60 * 1000,
  );
}

export function timelineWindow(now = new Date(), tasks: Task[] = []) {
  const current = zonedParts(now);
  const first = shiftedCalendarDate(current, -7);
  const last = shiftedCalendarDate(current, 24);
  const start = zonedMidnight(first.year, first.month, first.day);
  const baseEnd = zonedMidnight(last.year, last.month, last.day);
  const latest = tasks
    .map(dueDate)
    .filter((value): value is Date => value !== null)
    .reduce<Date | null>(
      (max, value) => (!max || value > max ? value : max),
      null,
    );
  const latestPlusFive = latest
    ? new Date(latest.getTime() + 5 * DAY)
    : baseEnd;
  return {
    start,
    end: latestPlusFive > baseEnd ? latestPlusFive : baseEnd,
    baseEnd,
  };
}
export function timelineX(value: Date, window: { start: Date }) {
  return (
    AXIS_START + ((value.getTime() - window.start.getTime()) / DAY) * DAY_WIDTH
  );
}
export function currentTimeX(
  now: Date,
  window: { start: Date; end: Date },
): number | null {
  return now >= window.start && now < window.end
    ? timelineX(now, window)
    : null;
}
export function compactDate(value: Date) {
  const parts = zonedParts(value);
  return `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][parts.month - 1]}.${parts.day}`;
}
export function sparseDueTicks(nodes: { date: Date; x: number }[]) {
  const unique = new Map<string, { date: Date; x: number }>();
  for (const node of nodes) {
    const parts = zonedParts(node.date);
    const key = `${parts.year}-${parts.month}-${parts.day}`;
    if (!unique.has(key))
      unique.set(key, {
        date: zonedMidnight(parts.year, parts.month, parts.day),
        x: timelineX(zonedMidnight(parts.year, parts.month, parts.day), {
          start: new Date(
            nodes.length
              ? nodes[0].date.getTime() -
                ((nodes[0].x - AXIS_START) / DAY_WIDTH) * DAY
              : 0,
          ),
        }),
      });
  }
  const ordered = [...unique.values()].sort((a, b) => a.x - b.x);
  let last = -Infinity;
  return ordered.filter((row) => {
    if (row.x - last < MIN_TICK_SPACING) return false;
    last = row.x;
    return true;
  });
}
export function layoutTimeline(
  tasks: Task[],
  courses: Course[],
  now = new Date(),
) {
  const window = timelineWindow(now, tasks);
  const lanes = courses.map((course, index) => ({ course, index }));
  const rawNodes = tasks.flatMap((task) => {
    const date = dueDate(task);
    if (!date || date < window.start || date > window.end) return [];
    const lane = lanes.find((row) => row.course.id === task.course_id);
    if (!lane) return [];
    return [
      {
        task,
        date,
        lane: lane.index,
        x: timelineX(date, window),
        y: 78 + lane.index * 96,
      },
    ];
  });
  // Place nearby labels into separate tracks within a lane. The circle stays on
  // the course line, while its text moves above or below to remain readable.
  const labelTracks = new Map<number, number[]>();
  const labelOffsets = [-13, 21, -35];
  const nodes = [...rawNodes]
    .sort((a, b) => a.lane - b.lane || a.x - b.x)
    .map((node) => {
      const ends = labelTracks.get(node.lane) ?? [
        -Infinity,
        -Infinity,
        -Infinity,
      ];
      let labelTrack = ends.findIndex((end) => node.x + 14 > end + 10);
      if (labelTrack < 0) labelTrack = ends.indexOf(Math.min(...ends));
      const labelWidth = Math.min(24, node.task.title.length) * 7.4;
      ends[labelTrack] = node.x + 14 + labelWidth;
      labelTracks.set(node.lane, ends);
      return { ...node, labelY: node.y + labelOffsets[labelTrack] };
    });
  const rawTicks = [
    ...new Map(
      nodes.map((node) => {
        const p = zonedParts(node.date);
        const date = zonedMidnight(p.year, p.month, p.day);
        return [
          `${p.year}-${p.month}-${p.day}`,
          { date, x: timelineX(date, window) },
        ];
      }),
    ).values(),
  ].sort((a, b) => a.x - b.x);
  let last = -Infinity;
  const ticks = rawTicks.filter((row) => {
    if (row.x - last < MIN_TICK_SPACING) return false;
    last = row.x;
    return true;
  });
  const domainDays = Math.max(
    1,
    (window.end.getTime() - window.start.getTime()) / DAY,
  );
  const width = AXIS_START + domainDays * DAY_WIDTH;
  const laneLines = lanes
    .filter((lane) => nodes.some((node) => node.lane === lane.index))
    .map((lane) => ({
      courseId: lane.course.id,
      x1: AXIS_START,
      x2: width,
      y: 78 + lane.index * 96,
    }));
  return {
    window,
    lanes,
    nodes,
    ticks,
    laneLines,
    nowX: currentTimeX(now, window),
    width,
    height: 110 + lanes.length * 96,
  };
}

export function TimelineView({
  tasks,
  courses,
  onCourse,
}: {
  tasks: Task[];
  courses: Course[];
  onCourse: (id: number) => void;
}) {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const layout = useMemo(
    () => layoutTimeline(tasks, courses, clock),
    [tasks, courses, clock],
  );
  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<{
    x: number;
    y: number;
    left: number;
    top: number;
  } | null>(null);
  const [dragging, setDragging] = useState(false);
  const suppressClick = useRef(false);
  function down(event: React.PointerEvent) {
    if (event.button !== 0) return;
    suppressClick.current = false;
    const el = viewport.current;
    if (!el) return;
    drag.current = {
      x: event.clientX,
      y: event.clientY,
      left: el.scrollLeft,
      top: el.scrollTop,
    };
  }
  function move(event: React.PointerEvent) {
    const el = viewport.current;
    if (!el || !drag.current) return;
    if (Math.hypot(event.clientX - drag.current.x, event.clientY - drag.current.y) <= 6 && !suppressClick.current) return;
    suppressClick.current = true;
    setDragging(true);
    if (!el.hasPointerCapture(event.pointerId)) el.setPointerCapture(event.pointerId);
    el.scrollLeft = drag.current.left - (event.clientX - drag.current.x);
    el.scrollTop = drag.current.top - (event.clientY - drag.current.y);
  }
  function up() {
    drag.current = null;
    setDragging(false);
  }
  return (
    <div
      className={`timeline-viewport ${dragging ? "dragging" : ""}`}
      ref={viewport}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={() => { suppressClick.current = true; up(); }}
      onClickCapture={event => { if (suppressClick.current && event.detail !== 0) { event.preventDefault(); event.stopPropagation(); } }}
    >
      <svg
        width={layout.width}
        height={layout.height}
        role="group"
        aria-label="Academic timeline with sparse due-date axis"
      >
        {layout.ticks.map((tick) => (
          <g key={tick.date.toISOString()}>
            <line
              x1={tick.x}
              x2={tick.x}
              y1={48}
              y2={layout.height}
              className="day-grid"
            />
            <text x={tick.x + 4} y={29} className="axis-label">
              {compactDate(tick.date)}
            </text>
          </g>
        ))}
        {layout.lanes.map(({ course, index }) => (
          <g key={course.id}>
            <text
              x={16}
              y={83 + index * 96}
              className="course-label"
              onClick={() => onCourse(course.id)}
            >
              {course.display_course_code ?? course.course_code}
            </text>
          </g>
        ))}
        {layout.laneLines.map((line) => (
          <line
            key={line.courseId}
            x1={line.x1}
            x2={line.x2}
            y1={line.y}
            y2={line.y}
            className="sequence-line"
          />
        ))}
        {layout.nowX !== null ? (
          <g className="now-marker" aria-label={`Now in ${UIUC_TIME_ZONE}`}>
            <line
              x1={layout.nowX}
              x2={layout.nowX}
              y1={42}
              y2={layout.height}
            />
            <rect x={layout.nowX - 21} y={7} width={42} height={23} rx={11} />
            <text x={layout.nowX} y={23}>
              Now
            </text>
          </g>
        ) : null}
        {layout.nodes.map((node) => (
          <g
            key={node.task.id}
            className={`task-node task-interactive ${taskTarget(node.task) ? "has-task-target" : ""}`}
            tabIndex={taskTarget(node.task) ? 0 : undefined}
            role={taskTarget(node.task) ? "link" : undefined}
            aria-label={`${node.task.title}, ${formatDeadline(node.task)}, ${submissionStatus(node.task.submission_state).label}`}
            onClick={() => { if (!suppressClick.current) openTask(node.task); }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                openTask(node.task);
              }
            }}
          >
            <title>{`${node.task.title}\n${formatDeadline(node.task)}\n${submissionStatus(node.task.submission_state).label}`}</title>
            <circle cx={node.x} cy={node.y} r={9} />
            <text x={node.x + 14} y={node.labelY}>
              {node.task.title.length > 24
                ? `${node.task.title.slice(0, 22)}…`
                : node.task.title}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}
