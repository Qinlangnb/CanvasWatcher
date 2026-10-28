"""Behavior and query-growth guards for request-local read models."""

from datetime import UTC, datetime, timedelta

import pytest
from performance_fixture import NOW, seed_performance_data
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db import (
    AIRun,
    AnalysisJob,
    Base,
    CalendarEvent,
    Course,
    SourceItem,
    StudyAvailabilityOverride,
    StudyAvailabilityRule,
    Task,
    TaskProgress,
    TaskWorkSession,
)
from app.services import today as today_module
from app.services.ai_settings import ai_probe_secrets
from app.services.assignment_summaries import SummaryReader, enqueue_visible_summaries
from app.services.calendar_capacity import CapacityReader
from app.services.today import TodayEngine
from app.services.work_tracking import task_work_summaries, task_work_summary


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    ai_probe_secrets.configure_persistence(None)
    ai_probe_secrets.clear()
    with Session(engine) as session:
        yield session
    ai_probe_secrets.clear()
    engine.dispose()


@pytest.mark.parametrize("count", [1, 120, 510])
def test_bounded_queries_and_discovery_membership(db, monkeypatch, count):
    ids = seed_performance_data(db, count)
    reads = []

    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        full = TodayEngine().build(db, NOW)
        assert len(reads) <= (22 if count > 400 else 15)
        visible = {row["task_id"] for row in full["work"] + full["completed"]}
        assert visible == set(ids)

        def unnecessary(*_args, **_kwargs):
            raise AssertionError("Discovery must not read page decorations")

        monkeypatch.setattr(today_module, "task_source_links", unnecessary)
        monkeypatch.setattr(today_module, "task_attachments", unnecessary)
        monkeypatch.setattr(TodayEngine, "_attention", unnecessary)
        light = TodayEngine().build(db, NOW, include_details=False)
        assert {row["task_id"] for row in light["work"] + light["completed"]} == visible
        original = TodayEngine.build
        monkeypatch.setattr(TodayEngine, "build", lambda self, db, **kw: original(self, db, NOW, **kw))
        reads.clear()
        enqueue_visible_summaries(db)
        assert len(reads) <= (19 if count > 400 else 12)
    finally:
        event.remove(db.bind, "before_cursor_execute", record)
    reader = SummaryReader(db, ids)
    expected = {item_id for item_id in reader.inputs if not reader.summaries.get(item_id)}
    jobs = db.scalars(select(AnalysisJob).where(AnalysisJob.kind == "assignment_summary")).all()
    assert {job.entity_id for job in jobs} == expected
    assert all(job.input_hash == reader.inputs[job.entity_id][1] for job in jobs)


def test_latest_progress_and_active_work_rounding(db):
    ids = seed_performance_data(db, 2, ai=False)
    task = db.get(Task, ids[1])
    db.add_all([
        TaskProgress(task_id=task.id, recorded_at=NOW, status="IN_PROGRESS", progress_percent=40),
        TaskProgress(task_id=task.id, recorded_at=NOW, status="IN_PROGRESS", progress_percent=80),
        TaskWorkSession(task_id=task.id, started_at=NOW-timedelta(seconds=91)),
    ])
    db.commit()
    assert TodayEngine._latest_progress(db, ids)[task.id].progress_percent == 80
    bulk = task_work_summaries(db, [task], NOW)[task.id]
    assert bulk == task_work_summary(db, task.id, now=NOW, include_sessions=False)
    assert bulk["active_elapsed_seconds"] == 91
    assert bulk["effective_used_minutes"] == max(0, bulk["tracked_minutes"] - 4)


def test_summary_latest_invalid_output_and_next_request_credential_gate(db):
    ids = seed_performance_data(db, 1)
    first = SummaryReader(db, ids)
    item_id, (_, digest) = next(iter(first.inputs.items()))
    assert first.facts[ids[0]][0]["description_summary_status"] == "ready"
    db.add(AIRun(purpose=f"assignment_summary:{item_id}", provider="deepseek", model="synthetic-model",
                 input_hash=digest, output_json={"invalid": "Do not reuse older valid output"}))
    db.commit()
    assert SummaryReader(db, ids).facts[ids[0]][0]["description_summary_status"] == "pending"
    ai_probe_secrets.clear()
    assert SummaryReader(db, ids).facts == {ids[0]: []}


def test_summary_filters_deleted_and_cross_course_links(db):
    ids = seed_performance_data(db, 2)
    items = db.scalars(select(SourceItem).order_by(SourceItem.id)).all()
    items[0].is_deleted = True
    other = Course(source="canvas", external_id="other", course_code="OTHER", name="Other")
    db.add(other)
    db.flush()
    items[1].course_id = other.id
    db.commit()
    assert SummaryReader(db, ids).facts == dict.fromkeys(ids, [])


def test_capacity_subwindow_selection_and_next_request_edits(db):
    start = datetime(2026, 10, 19, 13, tzinfo=UTC)  # 08:00 Chicago
    course = Course(source="manual", external_id="1", course_code="TEST", name="Test", commute_minutes=30)
    db.add(course)
    db.flush()
    rule = StudyAvailabilityRule(weekday=0, start_local_time="08:00", end_local_time="12:00")
    db.add_all([rule, CalendarEvent(source="manual", summary="Future class", course_id=course.id,
        start_at=start+timedelta(minutes=70), end_at=start+timedelta(minutes=100),
        timezone="America/Chicago", event_type="class")])
    db.commit()
    reader = CapacityReader(db, start, start+timedelta(hours=4))
    assert reader.between(start, start+timedelta(hours=1)) == (60, "availability")
    assert reader.between(start, start+timedelta(hours=2)) == (40, "calendar")
    rule.enabled = False
    db.commit()
    assert CapacityReader(db, start, start+timedelta(hours=1)).between(start, start+timedelta(hours=1)) == (0, "availability")


def test_capacity_override_dst_and_conservative_invalid_rule(db):
    start = datetime(2026, 11, 1, 5, tzinfo=UTC)  # Midnight on fall-back day
    end = start + timedelta(hours=4)
    db.add(StudyAvailabilityOverride(date=start.date(), available_intervals=[{"start": "00:00", "end": "03:00"}]))
    db.commit()
    assert CapacityReader(db, start, end).between(start, end) == (240, "availability")
    db.add(CalendarEvent(source="manual", summary="Invalid rule", start_at=start,
                         end_at=start+timedelta(minutes=30), timezone="America/Chicago",
                         recurrence_rule="FREQ=INVALID"))
    db.commit()
    assert CapacityReader(db, start, end).between(start, end) == (0, "calendar")
