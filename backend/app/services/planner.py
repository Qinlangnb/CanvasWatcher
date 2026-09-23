from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Course, Plan, PlanBlock, Task, TaskAnalysis
from app.timezone import UIUC_TZ, as_uiuc, as_utc, planning_cutoff


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


def split_minutes(total_minutes: int, preferred: list[int] | None = None) -> list[int]:
    preferred = [value for value in (preferred or [90, 60, 45]) if value > 0]
    blocks: list[int] = []
    remaining = max(0, total_minutes)
    while remaining:
        fit = next((value for value in preferred if value <= remaining), min(remaining, preferred[-1]))
        blocks.append(fit)
        remaining -= fit
    return blocks


def parse_availability(path: Path, start_day: date, days: int = 7) -> list[Window]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = (raw or {}).get("availability", {})
    windows: list[Window] = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for start_text, end_text in config.get(day.strftime("%A").lower(), []):
            start_time = time.fromisoformat(start_text)
            end_time = time.fromisoformat(end_text)
            windows.append(
                Window(
                    datetime.combine(day, start_time, UIUC_TZ),
                    datetime.combine(day, end_time, UIUC_TZ),
                )
            )
    return windows


class Planner:
    def __init__(self, availability_path: Path):
        self.availability_path = availability_path

    def rebuild(self, db: Session, now: datetime | None = None) -> Plan:
        now = as_uiuc(now) if now else datetime.now(UIUC_TZ)
        for old in db.scalars(select(Plan).where(Plan.status == "active")):
            old.status = "superseded"
        plan = Plan(
            window_start=as_utc(now),
            window_end=as_utc(now + timedelta(days=7)),
            status="active",
        )
        db.add(plan)
        db.flush()
        rows = db.execute(
            select(Task, TaskAnalysis)
            .join(TaskAnalysis, TaskAnalysis.task_id == Task.id, isouter=True)
            .join(Course, Course.id == Task.course_id)
            .where(
                Task.status.not_in(["SUBMITTED", "GRADED", "CANCELLED"]),
                Task.local_completed.is_(False),
                Task.ignored_at.is_(None),
                Course.lifecycle_state == "ACTIVE",
            )
            .order_by(TaskAnalysis.priority_score.desc().nullslast(), Task.due_at.asc().nullslast())
        ).all()
        windows = parse_availability(self.availability_path, now.date())
        cursor_by_window = [max(window.start, now) for window in windows]
        for task, analysis in rows:
            remaining = int(round((analysis.remaining_effort_hours if analysis else 1.0) * 60))
            for length in split_minutes(remaining):
                placed = False
                for index, window in enumerate(windows):
                    cursor = cursor_by_window[index]
                    end = cursor + timedelta(minutes=length)
                    deadline = task.due_at or (
                        planning_cutoff(task.due_date_local)
                        if task.due_date_local
                        else None
                    )
                    if deadline and deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=UTC).astimezone(UIUC_TZ)
                    if end <= window.end and (deadline is None or end <= deadline):
                        db.add(
                            PlanBlock(
                                plan_id=plan.id,
                                task_id=task.id,
                                start_at=as_utc(cursor),
                                end_at=as_utc(end),
                                objective=f"Advance {task.title}",
                                status="planned",
                            )
                        )
                        cursor_by_window[index] = end + timedelta(minutes=10)
                        placed = True
                        break
                if not placed:
                    break
        db.commit()
        return plan
