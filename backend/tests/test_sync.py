from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, ChangeEvent, Course, DownloadedFile, SourceItem, SourceSnapshot
from app.schemas import RawSourceItem
from app.services.sync import SyncService


def test_repeated_observation_has_no_duplicate_change(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(database_url="sqlite:///:memory:", download_root=tmp_path)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="17", course_code="PHYS225", name="Physics")
        db.add(course)
        db.flush()
        raw = RawSourceItem(source_type="canvas", source_name="canvas", external_id="4102", item_type="assignment", title="Homework 2", structured={"id": 4102, "due_at": "2026-09-02T23:59:00Z"})
        service = SyncService(settings)
        first, _ = service.store_observation(db, course, raw)
        second, _ = service.store_observation(db, course, raw)
        db.commit()
        assert first is not None
        assert second is None
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 1
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 1


def test_changed_observation_creates_deadline_event(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="17", course_code="PHYS225", name="Physics")
        db.add(course)
        db.flush()
        service = SyncService(Settings(database_url="sqlite:///:memory:", download_root=tmp_path))
        base = dict(source_type="canvas", source_name="canvas", external_id="1", item_type="assignment", title="HW")
        service.store_observation(db, course, RawSourceItem(**base, structured={"due_at": "2026-09-05T00:00:00Z"}))
        event, _ = service.store_observation(db, course, RawSourceItem(**base, structured={"due_at": "2026-09-04T00:00:00Z"}))
        assert event and event.change_type == "deadline_changed" and event.importance == "critical"


def test_downloader_preserves_file_versions(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="17", course_code="PHYS225", name="Physics")
        db.add(course)
        db.flush()
        item = SourceItem(
            course_id=course.id,
            source_type="canvas",
            source_name="canvas",
            external_id="file-1",
            item_type="file",
            title="Lecture01.pdf",
            current_hash="metadata",
        )
        db.add(item)
        db.flush()
        service = SyncService(Settings(database_url="sqlite:///:memory:", download_root=tmp_path))
        first = service.downloader.save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url="https://example.invalid/Lecture01.pdf",
            content=b"%PDF-1.4\nversion one",
        )
        duplicate = service.downloader.save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url="https://example.invalid/Lecture01.pdf",
            content=b"%PDF-1.4\nversion one",
        )
        second = service.downloader.save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url="https://example.invalid/Lecture01.pdf",
            content=b"%PDF-1.4\nversion two",
        )
        db.commit()
        assert first.changed and not duplicate.changed and second.changed
        assert first.record and second.record
        assert first.record.version_number == 1 and second.record.version_number == 2
        assert first.record.local_path != second.record.local_path
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 2
