from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base, Course, Task, TaskProgress, TaskWorkSession
from app.services.completion_repair import apply_completion_repair, completion_repair_plan
from app.timezone import as_utc


def test_repair_uses_latest_local_progress_and_preserves_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    stamp = datetime(2026, 9, 4, 3, 42, tzinfo=UTC)
    with Session(engine) as db:
        course = Course(source="website", external_id="test", name="Test", course_code="TEST")
        db.add(course)
        db.flush()
        tasks = [Task(course_id=course.id, title=str(i), submission_state="unsubmitted",
                      manual_time_adjustment_minutes=17, ignored_at=stamp) for i in range(5)]
        db.add_all(tasks)
        db.flush()
        for task, percent in zip(tasks, [100, 100, 1, 100, 100], strict=True):
            db.add(TaskProgress(task_id=task.id, status="IN_PROGRESS", progress_percent=percent,
                                recorded_at=stamp))
        db.flush()
        db.add(TaskProgress(task_id=tasks[1].id, status="NOT_STARTED", progress_percent=0,
                            recorded_at=stamp + timedelta(minutes=1)))
        db.add(TaskWorkSession(task_id=tasks[3].id, started_at=stamp))
        # Completion truth is known but an impossible historical day is not fabricated.
        future = db.scalar(select(TaskProgress).where(TaskProgress.task_id == tasks[4].id))
        future.recorded_at = datetime.now(UTC) + timedelta(days=10)
        db.commit()
        plan = completion_repair_plan(db)
        assert [row["task_id"] for row in plan["candidates"]] == [tasks[0].id, tasks[4].id]
        assert plan["skipped"] == [{"task_id": tasks[3].id, "reason": "open_timer_requires_review"}]
        assert not any(task.local_completed for task in tasks)
        assert apply_completion_repair(db, plan) == 2
        db.commit()
        assert as_utc(tasks[0].local_completed_at) == stamp
        assert tasks[4].local_completed and tasks[4].local_completed_at is None
        assert tasks[4].completion_origin == "repair_unknown_day"
        assert all(task.manual_time_adjustment_minutes == 17 and task.ignored_at for task in tasks)
        assert all(task.submission_state == "unsubmitted" for task in tasks)
        assert db.scalar(select(func.count()).select_from(TaskProgress)) == 6
        assert db.scalar(select(TaskWorkSession)).ended_at is None
        assert completion_repair_plan(db)["candidates"] == []
        with pytest.raises(ValueError, match="evidence changed"):
            apply_completion_repair(db, plan)
