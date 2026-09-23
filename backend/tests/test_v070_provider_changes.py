from copy import deepcopy

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, ChangeEvent, Course, SourceSnapshot
from app.schemas import RawSourceItem
from app.services.provider_changes import provider_change
from app.services.sync import SyncService


def data():
    return {"title": "HW 3", "source_key": "gradescope:10:100", "due_at": "2026-09-20T23:59:00-05:00",
        "provider_facts": {"provider": "gradescope", "provider_course_id": "10", "provider_assignment_id": "100",
            "due_at": "2026-09-20T23:59:00-05:00", "late_due_at": "2026-09-27T23:59:00-05:00",
            "submission_state": "submitted", "score": None, "max_score": None}}


def test_semantic_deadline_submission_grade_and_title_changes():
    old = data()
    for key, value, expected in (("due_at", "2026-09-19T23:59:00-05:00", "DEADLINE_CHANGED"),
        ("late_due_at", "2026-09-28T23:59:00-05:00", "DEADLINE_CHANGED"),
        ("submission_state", "graded", "ASSIGNMENT_SUBMISSION_CHANGED"),
        ("score", 0, "ASSIGNMENT_GRADE_CHANGED")):
        new = deepcopy(old)
        new["provider_facts"][key] = value
        assert provider_change(old, new)[0] == f"GRADESCOPE_{expected}"
    assert provider_change(old, {**old, "title": "New title"})[0].endswith("TITLE_CHANGED")
    assert provider_change(old, old) is None
    assert provider_change(None, old)[0] == "GRADESCOPE_ASSIGNMENT_CREATED"


def test_unknown_fields_preserve_prior_evidence_and_identical_sync_emits_no_churn(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(courses_config=tmp_path / "absent", download_root=tmp_path / "downloads",
        file_rules_config=tmp_path / "rules", availability_config=tmp_path / "availability", scheduler_enabled=False)
    service = SyncService(settings)
    with Session(engine) as db:
        course = Course(source="gradescope", external_id="10", course_code="TEST", name="Example")
        db.add(course)
        db.flush()
        raw = RawSourceItem(source_type="gradescope", source_name="gradescope:10", external_id="assignment:100",
            item_type="assignment", title="HW 3", structured=data())
        event, item = service.store_observation(db, course, raw)
        assert event.change_type == "GRADESCOPE_ASSIGNMENT_CREATED"
        db.commit()
        assert service.store_observation(db, course, raw)[0] is None
        unknown = deepcopy(raw)
        unknown.structured["due_at"] = None
        unknown.structured["provider_facts"].update(due_at=None, submission_state="unknown")
        service.store_observation(db, course, unknown)
        assert unknown.structured["provider_facts"]["submission_state"] == "submitted"
        assert unknown.structured["due_at"] == data()["due_at"]
        renamed = deepcopy(raw)
        renamed.title = "Renamed HW 3"
        renamed.structured["title"] = renamed.title
        event, same = service.store_observation(db, course, renamed)
        assert same.id == item.id and event.change_type == "GRADESCOPE_ASSIGNMENT_TITLE_CHANGED"
        db.commit()
        assert db.scalar(select(func.count(ChangeEvent.id))) == 2
        assert db.scalar(select(func.count(SourceSnapshot.id))) >= 2
