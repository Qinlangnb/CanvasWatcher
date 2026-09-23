import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai.queue import enqueue_task_analysis
from app.db import Base, Course, SourceItem, TaskAnalysis
from app.services.task_engine import task_from_assignment


@pytest.mark.parametrize("code,title", [("ASTR 405", "In-Class Activity 1"), ("PHYS 225", "Homework 1")])
def test_cached_assignment_shape_with_production_autoflush_disabled(code, title):
    """Sanitized actual ASTR cache shape; use the real SessionLocal flush policy."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        course = Course(source="canvas", external_id="fixture", course_code=code, name="Fixture")
        db.add(course)
        db.flush()
        item = SourceItem(course_id=course.id, source_type="canvas", source_name="canvas:fixture",
                          external_id="assignment:fixture", item_type="assignment", title=title, current_hash="fixture")
        db.add(item)
        db.flush()
        payload = {"description": "", "due_at": "2026-08-27T04:59:59Z", "deadline_precision": "EXACT_DATETIME",
                   "source_key": "canvas_assignment:fixture:1", "submission": {"workflow_state": "graded"}}
        task = task_from_assignment(db, item, payload)
        enqueue_task_analysis(db, task, "")
        db.commit()
        assert db.scalar(select(func.count()).select_from(TaskAnalysis)) == 1


def test_partial_assignment_preserves_known_snapshot_but_explicit_removal_clears(tmp_path):
    from app.config import Settings
    from app.db import SourceSnapshot
    from app.schemas import RawSourceItem
    from app.services.sync import SyncService

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="fixture", course_code="TEST", name="Test")
        db.add(course)
        db.commit()
        service = SyncService(Settings(courses_config=tmp_path / "absent", download_root=tmp_path))
        initial = RawSourceItem(source_type="canvas", source_name="canvas:fixture", external_id="assignment:1",
            item_type="assignment", title="Homework", normalized_text="Known instructions",
            structured={"description": "Known instructions", "description_state": "known",
                "due_at": "2026-09-15T04:59:00Z", "due_at_state": "known", "deadline_precision": "EXACT_DATETIME",
                "deadline_timezone": "America/Chicago", "deadline_source_rank": 1,
                "source_deadline_text": "2026-09-15T04:59:00Z", "source_key": "canvas:assignment:1"})
        _, item = service.store_observation(db, course, initial)
        task = task_from_assignment(db, item, initial.structured)
        task.local_completed = True
        task.manual_time_adjustment_minutes = 17
        db.commit()
        partial = initial.model_copy(deep=True)
        partial.normalized_text = ""
        partial.structured = {**initial.structured, "description": "", "description_state": "missing",
                              "due_at": None, "due_at_state": "missing"}
        event, item = service.store_observation(db, course, partial)
        assert event is None
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 1
        assert partial.structured["description"] == "Known instructions"
        assert partial.structured["due_at"] == initial.structured["due_at"]
        cleared = initial.model_copy(deep=True)
        cleared.normalized_text = ""
        cleared.structured.update(description="", description_state="known", due_at=None,
                                  due_at_state="removed", source_deadline_text=None, deadline_precision=None)
        _, item = service.store_observation(db, course, cleared)
        task = task_from_assignment(db, item, cleared.structured)
        db.commit()
        assert task.description == "" and task.due_at is None
        assert task.local_completed and task.manual_time_adjustment_minutes == 17


@pytest.mark.parametrize("effective", [None, "2026-09-20T04:59:00Z"])
def test_personalized_due_date_is_not_replaced_with_other_audience_dates(effective):
    from app.sources.canvas import CanvasAdapter

    adapter = CanvasAdapter("https://canvas.example", "synthetic")
    raw = adapter._normalize("assignment", {"id": 1, "name": "Different audience dates",
        "due_at": effective, "description": "Instructions", "has_overrides": True,
        "all_dates": [{"base": True, "due_at": None},
                      {"id": 23, "title": "Other section", "due_at": "2026-09-10T04:59:00Z"}]}, "fixture", {})
    assert raw.structured["due_at"] == effective
    assert raw.structured["due_at_state"] == ("known" if effective else "removed")


@pytest.mark.asyncio
async def test_assignment_detail_requests_personalized_dates_explicitly():
    from app.sources.canvas import CanvasAdapter

    class Client:
        async def get_json(self, path, params):
            assert dict(params)["override_assignment_dates"] == "true"
            assert dict(params)["all_dates"] == "true"
            return {"id": 1, "due_at": "2026-09-20T04:59:00Z", "description": "Body"}
    adapter = CanvasAdapter("https://canvas.example", "synthetic")
    adapter.client = Client()
    result = await adapter._assignment_detail("fixture", {"id": 1})
    assert result["due_at"] == "2026-09-20T04:59:00Z"
