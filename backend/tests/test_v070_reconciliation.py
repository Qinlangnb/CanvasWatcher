from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base, Course, SourceItem, SourceSnapshot, Task, TaskSourceLink
from app.services.task_engine import task_from_assignment
from app.services.task_providers import task_provider_facts, terminal_state


def snapshot(db, course, provider, data, title="HW 3"):
    item = SourceItem(course_id=course.id, source_type=provider, source_name=provider,
        external_id=f"{provider}:100", item_type="assignment", title=title,
        url="https://www.gradescope.com/courses/10" if provider == "gradescope" else "https://canvas.example/courses/1/assignments/100",
        current_hash="fixture")
    db.add(item)
    db.flush()
    db.add(SourceSnapshot(source_item_id=item.id, structured_json=data, content_hash="fixture"))
    db.flush()
    return item


@pytest.mark.parametrize("first_provider", ["canvas", "gradescope"])
def test_strong_identity_one_task_preserves_user_state_and_provider_deadlines(first_provider):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    data = {
        "canvas": {"source_key": "homework:3", "due_at": "2026-09-17T23:59:00-05:00", "deadline_source_rank": 1,
            "external_tool_tag_attributes": {"url": "https://www.gradescope.com/courses/10/assignments/100"},
            "submission": {"workflow_state": "unsubmitted"}},
        "gradescope": {"source_key": "gradescope:10:100", "due_at": "2026-09-20T23:59:00-05:00", "deadline_source_rank": 0,
            "provider_facts": {"provider": "gradescope", "provider_course_id": "10", "provider_assignment_id": "100",
                "due_at": "2026-09-20T23:59:00-05:00", "late_due_at": "2026-09-27T23:59:00-05:00", "submission_state": "submitted"},
            "submission": {"workflow_state": "submitted"}},
    }
    with Session(engine) as db:
        course = Course(source="canvas", external_id="1", course_code="TEST", name="Test")
        db.add(course)
        db.flush()
        first = snapshot(db, course, first_provider, data[first_provider])
        task = task_from_assignment(db, first, data[first_provider])
        task.local_completed = True
        task.local_completed_at = datetime(2026, 9, 16, tzinfo=UTC)
        task.ignored_at = datetime(2026, 9, 16, tzinfo=UTC)
        task.manual_time_adjustment_minutes = 37
        other = "gradescope" if first_provider == "canvas" else "canvas"
        second = snapshot(db, course, other, data[other], "Renamed Homework 3")
        merged = task_from_assignment(db, second, data[other])
        assert task.id == merged.id
        assert db.scalar(select(func.count(Task.id))) == 1
        assert db.scalar(select(func.count(TaskSourceLink.id))) == 2
        assert task.local_completed and task.manual_time_adjustment_minutes == 37 and task.ignored_at
        assert task.submission_state == "submitted" and task.status == "SUBMITTED"
        assert task.due_at.day == 20
        facts = task_provider_facts(db, task.id)
        assert len(facts) == 2
        assert {row["provider"] for row in facts} == {"canvas", "gradescope"}
        assert next(row for row in facts if row["provider"] == "canvas")["due_at"].startswith("2026-09-17")


def test_weak_similar_titles_do_not_auto_merge_and_prairielearn_progress_is_not_submission():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="1", course_code="TEST", name="Test")
        db.add(course)
        db.flush()
        for provider in ("canvas", "gradescope"):
            data = {"source_key": f"{provider}:10:100", "due_at": "2026-09-20T00:00:00Z"}
            item = snapshot(db, course, provider, data)
            if provider == "gradescope":
                with pytest.raises(ValueError, match="PROVIDER_TASK_MAPPING_REQUIRED"):
                    task_from_assignment(db, item, data)
                data["confirmed_separate"] = True
            task_from_assignment(db, item, data)
        assert db.scalar(select(func.count(Task.id))) == 2
    assert terminal_state([{"provider": "prairielearn", "submission_state": "submitted", "progress": 100}]) is None
    assert terminal_state([{"provider": "canvas", "submission_state": "unknown"},
                           {"provider": "gradescope", "submission_state": "graded"}]) == "graded"
