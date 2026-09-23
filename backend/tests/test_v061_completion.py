from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base, Course, Task, TaskProgress, TaskWorkSession
from app.services.completion import apply_progress
from app.services.today import TodayEngine
from app.services.work_tracking import record_work_heartbeat


@pytest.fixture
def database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="fixture", course_code="TEST", name="Test")
        db.add(course)
        db.flush()
        task = Task(course_id=course.id, title="Homework", submission_state="unsubmitted")
        db.add(task)
        db.commit()
        yield db, task


@pytest.mark.parametrize("moment", [datetime(2026, 9, 12, 4, 59, tzinfo=UTC), datetime(2026, 1, 12, 5, 59, tzinfo=UTC)])
def test_midnight_completion_is_context_only_then_disappears(database, moment):
    from app.timezone import as_uiuc, as_utc
    db, task = database
    task.due_date_local = as_uiuc(moment).date()
    task.deadline_precision = "DATE_ONLY"
    db.add(TaskWorkSession(task_id=task.id, started_at=moment - timedelta(minutes=10)))
    db.commit()
    apply_progress(db, task, 100, action_at=moment, now=moment + timedelta(minutes=2))
    db.commit()
    assert task.submission_state == "unsubmitted"
    assert task.local_completed
    assert as_utc(task.local_completed_at) == moment
    row = db.scalar(select(TaskWorkSession))
    assert row.duration_seconds == 600
    before = TodayEngine().build(db, moment)
    assert before["work"] == []
    assert before["completed"][0]["today_reason"] == "Completed · Due today"
    after = TodayEngine().build(db, moment + timedelta(minutes=2))
    assert after["work"] == after["completed"] == []


def test_duplicate_completion_and_late_heartbeat_preserve_state(database):
    db, task = database
    apply_progress(db, task, 100)
    db.commit()
    revision, stamp = task.state_revision, task.local_completed_at
    assert apply_progress(db, task, 100)[1] is False
    record_work_heartbeat(db, task.id, progress_percent=99)
    assert task.state_revision == revision and task.local_completed_at == stamp
    assert db.scalar(select(func.count()).select_from(TaskProgress)) == 1
    assert apply_progress(db, task, 99, expected_revision=0, reopen=True)[1] is False
    assert apply_progress(db, task, 99, expected_revision=revision, reopen=True)[1] is True
    assert not task.local_completed
    assert task.submission_state == "unsubmitted"


def test_completion_timestamp_rejected_before_mutation(database):
    db, task = database
    with pytest.raises(ValueError):
        apply_progress(db, task, 100, action_at=datetime.now(UTC) + timedelta(days=10))
    assert not task.local_completed


def test_stale_sqlalchemy_session_cannot_overwrite_completion(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as first, Session(engine) as stale:
        course = Course(source="website", external_id="test", course_code="TEST", name="Test")
        first.add(course)
        first.flush()
        task = Task(course_id=course.id, title="Test")
        first.add(task)
        first.commit()
        stale_task = stale.get(Task, task.id)
        stamp = datetime.now(UTC)
        apply_progress(first, task, 100, now=stamp)
        first.commit()
        assert not stale_task.local_completed
        assert apply_progress(stale, stale_task, 99, expected_revision=0,
                              now=stamp + timedelta(seconds=1))[1] is False
        stale.commit()
        first.refresh(task)
        assert task.local_completed and task.state_revision == 1
        assert first.scalar(select(func.count()).select_from(TaskProgress)) == 1


def test_old_heartbeat_cannot_write_manual_time_or_remaining(database):
    db, task = database
    stamp = datetime.now(UTC)
    db.add(TaskWorkSession(task_id=task.id, started_at=stamp - timedelta(minutes=10)))
    apply_progress(db, task, 60, now=stamp)
    db.commit()
    record_work_heartbeat(db, task.id, progress_percent=50,
        manual_adjustment_minutes=999, remaining_minutes=999,
        client_timestamp=stamp - timedelta(seconds=1), now=stamp)
    db.refresh(task)
    assert task.manual_time_adjustment_minutes == 0
    assert task.last_client_remaining_minutes is None
    assert db.scalar(select(func.count()).select_from(TaskProgress)) == 1


def test_manual_only_heartbeat_advances_watermark(database):
    from app.timezone import as_utc
    db, task = database
    stamp = datetime.now(UTC)
    db.add(TaskWorkSession(task_id=task.id, started_at=stamp - timedelta(minutes=10)))
    db.commit()
    record_work_heartbeat(db, task.id, manual_adjustment_minutes=40,
                          client_timestamp=stamp, now=stamp)
    record_work_heartbeat(db, task.id, manual_adjustment_minutes=0,
                          client_timestamp=stamp - timedelta(seconds=5), now=stamp)
    db.refresh(task)
    assert task.manual_time_adjustment_minutes == 40
    assert as_utc(task.last_client_reported_at) == stamp
    assert db.scalar(select(func.count()).select_from(TaskProgress)) == 0


@pytest.mark.parametrize("submitted", ["unsubmitted", "submitted", "graded"])
def test_today_exposes_submission_separately_from_local_completion(database, submitted):
    from app.schemas import TodayWorkOut
    db, task = database
    task.submission_state = submitted
    stamp = datetime.now(UTC)
    apply_progress(db, task, 100, now=stamp)
    db.commit()
    row = TodayWorkOut.model_validate(TodayEngine().build(db, stamp)["completed"][0])
    assert row.local_completed and row.submission_state == submitted
