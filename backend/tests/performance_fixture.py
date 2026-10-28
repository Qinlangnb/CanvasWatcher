"""Synthetic, credential-free fixtures shared by the benchmark and regressions."""
from datetime import UTC, datetime, timedelta

from app.db import (
    AIRun,
    AnalysisJob,
    CalendarEvent,
    Course,
    SourceItem,
    SourceSnapshot,
    StudyAvailabilityOverride,
    StudyAvailabilityRule,
    Task,
    TaskProgress,
    TaskSourceLink,
    TaskWorkSession,
)
from app.services.ai_settings import (
    _identity,
    _value,
    ai_probe_secrets,
    set_ai_credential,
    set_ai_probe_settings,
)
from app.services.assignment_summaries import summary_input

NOW = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)


def seed_performance_data(db, count=30, *, ai=True):
    ai_probe_secrets.configure_persistence(None)
    ai_probe_secrets.clear()
    if ai:
        set_ai_probe_settings(db, "", "synthetic-model", "deepseek")
        set_ai_credential(db, "synthetic-not-a-real-key", "deepseek")
        ai_probe_secrets.mark_verified(_identity(_value(db)))
    course = Course(source="canvas", external_id="synthetic", course_code="TEST101",
                    name="Synthetic", commute_minutes=10)
    db.add(course)
    db.flush()
    for day in range(7):
        db.add(StudyAvailabilityRule(weekday=day, start_local_time="08:00", end_local_time="21:00"))
    db.add(StudyAvailabilityOverride(date=(NOW + timedelta(days=2)).date(), available_intervals=[]))
    for number in range(3):
        db.add(CalendarEvent(source="manual", summary="Synthetic class", course_id=course.id,
            start_at=NOW + timedelta(minutes=20 + number * 90),
            end_at=NOW + timedelta(minutes=60 + number * 90),
            timezone="America/Chicago", event_type="class", recurrence_rule="FREQ=DAILY;COUNT=20"))
    tasks = []
    for index in range(count):
        task = Task(course_id=course.id, title=f"Task {index}", task_type="homework",
            due_at=NOW + timedelta(minutes=20 + index % 120),
            manual_time_adjustment_minutes=index % 11 - 5,
            local_completed=index % 13 == 0, local_completed_at=NOW if index % 13 == 0 else None)
        item = SourceItem(course_id=course.id, source_type="canvas", source_name="canvas:synthetic",
            external_id=f"assignment:{index}", item_type="assignment", title=f"Task {index}",
            current_hash=str(index), url=f"https://canvas.example/courses/1/assignments/{index}")
        db.add_all([task, item])
        db.flush()
        db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id))
        db.add(SourceSnapshot(source_item_id=item.id, content_hash=f"old-{index}",
            structured_json={"description": "Obsolete fixture"}))
        db.add(SourceSnapshot(source_item_id=item.id, content_hash=str(index), structured_json={
            "description": f"Solve exercise {index} and submit a PDF.", "due_at": task.due_at.isoformat(),
            "submission": {"workflow_state": "submitted" if index % 13 == 0 else "unsubmitted"}}))
        for number in range(3):
            db.add(TaskProgress(task_id=task.id, recorded_at=NOW - timedelta(minutes=5-number),
                status="IN_PROGRESS", progress_percent=(index * 3 + number) % 100))
        for number in range(2):
            db.add(TaskWorkSession(task_id=task.id, started_at=NOW-timedelta(hours=3-number),
                ended_at=NOW-timedelta(hours=3-number, minutes=-10), duration_seconds=600+index))
        db.flush()
        if ai:
            _, digest = summary_input(db, item.id)
            if index % 3 == 0:
                db.add(AIRun(purpose=f"assignment_summary:{item.id}", provider="deepseek",
                    model="synthetic-model", input_hash=digest, output_json={"summary": f"Synthetic summary {index}"}))
            elif index % 3 == 1:
                db.add(AnalysisJob(kind="assignment_summary", entity_id=item.id, input_hash=digest, state="FAILED", attempts=2))
        tasks.append(task)
    db.commit()
    return [task.id for task in tasks]
