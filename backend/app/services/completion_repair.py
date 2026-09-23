"""Auditable repair for legacy local TaskProgress, never source-derived percentages.

The V0.6.0 writers of task_progress were the local progress/work/confirmed-AI
commands. Source synchronization writes Task submission facts, not TaskProgress.
Do not reuse this backfill for arbitrary imported percentage columns.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import Task, TaskAnalysis, TaskProgress, TaskWorkSession
from app.timezone import as_utc


def completion_repair_plan(db: Session, now: datetime | None = None) -> dict:
    now = as_utc(now or datetime.now(UTC))
    values = list(db.scalars(select(TaskProgress.progress_percent).where(
        TaskProgress.progress_percent.is_not(None))))
    scale = {"count": len(values), "min": min(values, default=None),
             "max": max(values, default=None),
             "fractional_below_one": sum(0 < value < 1 for value in values)}
    latest = select(TaskProgress.task_id, func.max(TaskProgress.id).label("id")).group_by(
        TaskProgress.task_id).subquery()
    rows = db.execute(select(Task, TaskProgress).join(
        TaskProgress, Task.id == TaskProgress.task_id).join(
        latest, latest.c.id == TaskProgress.id).where(
        Task.local_completed.is_(False), TaskProgress.progress_percent == 100
    ).order_by(Task.id)).all()
    candidates, skipped = [], []
    for task, progress in rows:
        if db.scalar(select(TaskWorkSession.id).where(
            TaskWorkSession.task_id == task.id, TaskWorkSession.ended_at.is_(None)
        )) is not None:
            skipped.append({"task_id": task.id, "reason": "open_timer_requires_review"})
            continue
        stamp = as_utc(progress.recorded_at) if progress.recorded_at else None
        if stamp and stamp > now:
            stamp = None
        candidates.append({"task_id": task.id, "progress_id": progress.id,
            "expected_revision": task.state_revision or 0,
            "completion_at": stamp.isoformat() if stamp else None,
            "origin": "repair_local_progress" if stamp else "repair_unknown_day"})
    return {"scale": scale, "candidates": candidates, "skipped": skipped}


def apply_completion_repair(db: Session, approved_plan: dict) -> int:
    """Apply an already inspected plan atomically; caller commits/rebuilds once."""
    current = completion_repair_plan(db)
    if current != approved_plan:
        raise ValueError("Repair evidence changed; generate and review a new dry run")
    changed = 0
    for row in current["candidates"]:
        task = db.get(Task, row["task_id"])
        status = task.status
        if task.submission_state not in {"submitted", "graded"}:
            status = "READY_TO_SUBMIT"
        result = db.execute(update(Task).where(
            Task.id == row["task_id"], Task.local_completed.is_(False),
            Task.state_revision == row["expected_revision"],
        ).values(local_completed=True,
            local_completed_at=datetime.fromisoformat(row["completion_at"]) if row["completion_at"] else None,
            completion_origin=row["origin"], state_revision=row["expected_revision"] + 1,
            last_client_remaining_minutes=0, status=status))
        if result.rowcount != 1:
            raise ValueError("Task changed during repair; roll back and review again")
        db.execute(update(TaskAnalysis).where(TaskAnalysis.task_id == task.id).values(
            remaining_effort_hours=0))
        changed += 1
    db.flush()
    return changed
