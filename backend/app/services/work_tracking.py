from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import Task, TaskProgress, TaskWorkSession
from app.timezone import as_utc


def _now(value: datetime | None = None) -> datetime:
    return as_utc(value or datetime.now(UTC))


def session_seconds(session: TaskWorkSession, now: datetime | None = None) -> int:
    if session.duration_seconds is not None:
        return max(0, session.duration_seconds)
    end = as_utc(session.ended_at) if session.ended_at else _now(now)
    return max(0, round((end - as_utc(session.started_at)).total_seconds()))


def active_session(db: Session) -> TaskWorkSession | None:
    return db.scalar(
        select(TaskWorkSession)
        .where(TaskWorkSession.ended_at.is_(None))
        .order_by(TaskWorkSession.started_at.desc(), TaskWorkSession.id.desc())
        .limit(1)
    )


def task_work_summary(
    db: Session, task_id: int, now: datetime | None = None, *, include_sessions: bool = True
) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise LookupError("Task not found")
    rows = list(
        db.scalars(
            select(TaskWorkSession)
            .where(TaskWorkSession.task_id == task_id)
            .order_by(TaskWorkSession.started_at, TaskWorkSession.id)
        )
    )
    total_seconds = sum(session_seconds(row, now) for row in rows)
    tracked_minutes = max(0, round(total_seconds / 60))
    adjustment = int(task.manual_time_adjustment_minutes or 0)
    effective = max(0, tracked_minutes + adjustment)
    opened = next((row for row in reversed(rows) if row.ended_at is None), None)
    return {
        "task_id": task.id,
        "state_revision": task.state_revision or 0,
        "local_completed": bool(task.local_completed),
        "tracked_minutes": tracked_minutes,
        "manual_adjustment_minutes": adjustment,
        "effective_used_minutes": effective,
        "active_session_id": opened.id if opened else None,
        "active_started_at": opened.started_at if opened else None,
        "active_elapsed_seconds": session_seconds(opened, now) if opened else 0,
        "sessions": rows if include_sessions else [],
    }


def stop_session(session: TaskWorkSession, ended_at: datetime | None = None) -> None:
    end = _now(ended_at)
    session.ended_at = end
    session.duration_seconds = session_seconds(session, end)


def start_task_work(
    db: Session, task_id: int, now: datetime | None = None
) -> tuple[dict, int | None]:
    task = db.get(Task, task_id)
    if task is None:
        raise LookupError("Task not found")
    moment = _now(now)
    if task.local_completed:
        raise RuntimeError("Reopen completed work before starting a timer")
    opened = active_session(db)
    switched_from: int | None = None
    if opened and opened.task_id == task_id:
        return task_work_summary(db, task_id, moment), None
    if opened:
        switched_from = opened.task_id
        stop_session(opened, moment)
    created = TaskWorkSession(task_id=task_id, started_at=moment, source="timer")
    db.add(created)
    if task.status == "NOT_STARTED":
        task.status = "IN_PROGRESS"
    db.commit()
    return task_work_summary(db, task_id, moment), switched_from


def stop_task_work(db: Session, task_id: int, now: datetime | None = None) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise LookupError("Task not found")
    opened = active_session(db)
    if opened and opened.task_id == task_id:
        stop_session(opened, now)
        db.commit()
    return task_work_summary(db, task_id, now)


def set_effective_used_minutes(
    db: Session, task_id: int, requested_minutes: int, now: datetime | None = None
) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise LookupError("Task not found")
    requested = max(0, int(requested_minutes))
    current = task_work_summary(db, task_id, now, include_sessions=False)
    task.manual_time_adjustment_minutes = requested - current["tracked_minutes"]
    db.commit()
    return task_work_summary(db, task_id, now)


def record_work_heartbeat(
    db: Session,
    task_id: int,
    *,
    progress_percent: float | None = None,
    manual_adjustment_minutes: int | None = None,
    remaining_minutes: int | None = None,
    client_timestamp: datetime | None = None,
    now: datetime | None = None,
    expected_revision: int | None = None,
) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise LookupError("Task not found")
    opened = active_session(db)
    if opened is None or opened.task_id != task_id:
        if task.local_completed:
            return task_work_summary(db, task_id, now)
        raise RuntimeError("Task does not have the active work session")
    if expected_revision is not None and expected_revision != (task.state_revision or 0):
        return task_work_summary(db, task_id, now)
    if task.local_completed:
        return task_work_summary(db, task_id, now)
    moment = _now(now)
    if client_timestamp is not None:
        if client_timestamp.tzinfo is None:
            raise ValueError("Heartbeat timestamp must include a timezone")
        stamp = as_utc(client_timestamp)
        if stamp > moment + timedelta(minutes=5) or stamp < moment - timedelta(days=1):
            raise ValueError("Heartbeat timestamp is outside the synchronization window")
    else:
        stamp = moment
    if client_timestamp and task.last_client_reported_at and as_utc(client_timestamp) < as_utc(task.last_client_reported_at):
        return task_work_summary(db, task_id, now)
    latest_progress = db.scalar(select(TaskProgress).where(
        TaskProgress.task_id == task_id).order_by(TaskProgress.id.desc()).limit(1))
    if client_timestamp and latest_progress and as_utc(client_timestamp) < as_utc(latest_progress.recorded_at):
        return task_work_summary(db, task_id, now)
    # A timer-only heartbeat also needs a DB guard, not merely an ORM check.
    revision = task.state_revision or 0
    claimed = db.execute(update(Task).where(
        Task.id == task_id, Task.state_revision == revision,
        Task.local_completed.is_(False),
    ).values(state_revision=revision).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.refresh(task)
        return task_work_summary(db, task_id, now)
    changed = False
    if progress_percent is not None:
        from app.services.completion import apply_progress

        _, changed = apply_progress(db, task, progress_percent, origin="heartbeat",
                                     action_at=client_timestamp, now=now, expected_revision=expected_revision)
    if manual_adjustment_minutes is not None:
        task.manual_time_adjustment_minutes = int(manual_adjustment_minutes)
    if remaining_minutes is not None and not task.local_completed:
        task.last_client_remaining_minutes = max(0, int(remaining_minutes))
    elif manual_adjustment_minutes is not None and not task.local_completed:
        task.last_client_remaining_minutes = None
    task.last_client_reported_at = stamp
    db.commit()
    if changed:
        from app.config import get_settings
        from app.services.planner import Planner
        Planner(get_settings().availability_config).rebuild(db, now=now)
    return task_work_summary(db, task_id, now)
