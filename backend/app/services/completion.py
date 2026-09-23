"""Atomic local progress transitions. Callers own commit and plan invalidation."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import Task, TaskAnalysis, TaskProgress, TaskWorkSession
from app.timezone import as_utc


def apply_progress(
    db: Session, task: Task, percent: float, *, origin: str = "user",
    expected_revision: int | None = None, action_at: datetime | None = None,
    now: datetime | None = None, reopen: bool = False,
) -> tuple[TaskProgress | None, bool]:
    if not 0 <= percent <= 100:
        raise ValueError("Progress must be between zero and one hundred")
    revision = task.state_revision or 0
    if expected_revision is not None and expected_revision != revision:
        return None, False
    if task.local_completed and (percent == 100 or not reopen):
        return None, False
    if task.local_completed and expected_revision is None:
        raise ValueError("Reopening requires the current state revision")
    moment = as_utc(now or datetime.now(UTC))
    if action_at is not None:
        if action_at.tzinfo is None:
            raise ValueError("Action timestamp must include a timezone")
        action_at = as_utc(action_at)
        if action_at > moment + timedelta(minutes=5) or action_at < moment - timedelta(days=1):
            raise ValueError("Action timestamp is outside the accepted synchronization window")
    else:
        action_at = moment
    latest = db.scalar(select(TaskProgress).where(TaskProgress.task_id == task.id).order_by(
        TaskProgress.recorded_at.desc(), TaskProgress.id.desc()).limit(1))
    if latest and action_at < as_utc(latest.recorded_at):
        return None, False
    if latest and latest.progress_percent == percent and percent < 100 and not task.local_completed:
        return latest, False
    opened = db.scalar(select(TaskWorkSession).where(
        TaskWorkSession.task_id == task.id, TaskWorkSession.ended_at.is_(None))) if percent == 100 else None
    if opened and action_at < as_utc(opened.started_at):
        raise ValueError("Completion predates the active work session")
    # Comparing an ORM object's revision alone is insufficient: another request
    # may have committed after this Session loaded it. Claim the DB revision in
    # the same transaction before writing progress, timer or completion fields.
    claimed = db.execute(update(Task).where(
        Task.id == task.id, Task.state_revision == revision,
        Task.local_completed == bool(task.local_completed),
    ).values(state_revision=revision + 1).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.refresh(task)
        return None, False
    task.state_revision = revision + 1
    task.local_completed = percent == 100
    task.local_completed_at = action_at if percent == 100 else None
    task.completion_origin = origin if percent == 100 else None
    # Upstream facts remain separately stored in submission_state.
    if task.submission_state not in {"submitted", "graded"}:
        task.status = "READY_TO_SUBMIT" if percent == 100 else "IN_PROGRESS" if percent else "NOT_STARTED"
    if percent == 100:
        task.last_client_remaining_minutes = 0
        if opened:
            opened.ended_at = action_at
            opened.duration_seconds = max(0, round((action_at - as_utc(opened.started_at)).total_seconds()))
    progress = TaskProgress(task_id=task.id, status=task.status, progress_percent=percent,
                            recorded_at=action_at)
    db.add(progress)
    analysis = db.scalar(select(TaskAnalysis).where(TaskAnalysis.task_id == task.id))
    if analysis:
        analysis.remaining_effort_hours = max(0, analysis.estimated_effort_hours * (1 - percent / 100))
    if percent < 100:
        task.last_client_remaining_minutes = None
        task.last_client_reported_at = None
    db.flush()
    return progress, True
