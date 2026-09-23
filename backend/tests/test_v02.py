import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.routes import change_detail, changes
from app.auth.models import FetchResult, FetchStatus
from app.config import Settings
from app.db import (
    Base,
    ChangeEvent,
    Course,
    SourceItem,
    SourceSnapshot,
    Task,
)
from app.schemas import RawSourceItem
from app.services.downloader import FileDownloader, validate_course_file
from app.services.sync import SyncService
from app.services.task_engine import task_from_assignment
from app.sources.phys225_schedule import parse_phys225_schedule
from app.sources.website import WebsiteAdapter
from app.timezone import UIUC_TZ, as_uiuc, planning_cutoff

FIXTURES = Path(__file__).parent / "fixtures"
SCHEDULE_URL = "https://courses.physics.illinois.edu/phys225/fa2026/schedule.html"


def test_uiuc_timezone_uses_dst_database_and_date_only_stays_local() -> None:
    summer = as_uiuc(datetime(2026, 7, 1, 18, tzinfo=UTC))
    winter = as_uiuc(datetime(2026, 12, 1, 18, tzinfo=UTC))
    assert summer.utcoffset() == timedelta(hours=-5)
    assert winter.utcoffset() == timedelta(hours=-6)
    assert summer.tzname() != winter.tzname()
    due_date = date(2026, 9, 3)
    assert planning_cutoff(due_date).astimezone(UIUC_TZ).date() == due_date


def test_phys225_schedule_parser_all_homework_dates() -> None:
    rows = parse_phys225_schedule(
        (FIXTURES / "phys225_schedule_fa2026.html").read_bytes(),
        week_1_monday="2026-08-24",
        schedule_url=SCHEDULE_URL,
    )
    due = {row.number: row.due_date.isoformat() for row in rows if row.due_date}
    assigned = {
        row.number: row.assigned_date.isoformat() for row in rows if row.assigned_date
    }
    assert due == {
        1: "2026-09-03",
        2: "2026-09-10",
        3: "2026-09-17",
        4: "2026-09-24",
        5: "2026-10-01",
        6: "2026-10-08",
        7: "2026-10-12",
        8: "2026-10-29",
        9: "2026-11-05",
        10: "2026-11-12",
        11: "2026-11-19",
        12: "2026-12-03",
        13: "2026-12-09",
    }
    assert assigned[1] == "2026-08-27"
    assert assigned[8] == "2026-10-22"
    assert assigned[13] == "2026-12-03"


class ScheduleFetcher:
    async def fetch(self, url: str, auth_rule=None) -> FetchResult:
        del auth_rule
        return FetchResult(
            status=FetchStatus.OK,
            url=url,
            status_code=200,
            content=(FIXTURES / "phys225_schedule_fa2026.html").read_bytes(),
            content_type="text/html",
        )


@pytest.mark.asyncio
async def test_schedule_sync_creates_and_updates_thirteen_tasks_without_duplicates(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(
            source="website",
            external_id="configured:PHYS225",
            course_code="PHYS225",
            name="Physics 225",
        )
        db.add(course)
        db.commit()
        adapter = WebsiteAdapter(
            name="phys225_fa2026",
            course_code="PHYS225",
            base_url=SCHEDULE_URL,
            public_pages=[SCHEDULE_URL],
            discovery={"max_depth": 0},
            timezone="America/Chicago",
            term_calendar={"week_1_monday": "2026-08-24"},
            fetcher=ScheduleFetcher(),
        )
        service = SyncService(
            Settings(
                database_url="sqlite:///:memory:",
                download_root=tmp_path / "downloads",
                courses_config=tmp_path / "none.yaml",
                availability_config=tmp_path / "availability.yaml",
            )
        )
        await service.run(db, [(course, adapter)])
        await service.run(db, [(course, adapter)])
        assert db.scalar(select(func.count()).select_from(Task)) == 13
        hw1 = db.scalar(select(Task).where(Task.source_key == "homework:1"))
        hw7 = db.scalar(select(Task).where(Task.source_key == "homework:7"))
        hw13 = db.scalar(select(Task).where(Task.source_key == "homework:13"))
        assert hw1 and hw1.due_date_local == date(2026, 9, 3)
        assert hw1.deadline_precision == "DATE_ONLY" and hw1.due_at is None
        assert hw7 and hw7.due_date_local == date(2026, 10, 12)
        assert hw13 and hw13.due_date_local == date(2026, 12, 9)


def test_schedule_deadline_change_updates_same_task_and_creates_change(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="website", external_id="p", course_code="PHYS225", name="P")
        db.add(course)
        db.flush()
        service = SyncService(
            Settings(database_url="sqlite:///:memory:", download_root=tmp_path)
        )
        base = {
            "source_type": "website",
            "source_name": "phys225_fa2026",
            "external_id": f"{SCHEDULE_URL}#homework-4",
            "item_type": "assignment",
            "title": "HW 4",
            "url": SCHEDULE_URL,
        }
        first_raw = RawSourceItem(
            **base,
            structured={
                "source_key": "homework:4",
                "due_date_local": "2026-09-24",
                "deadline_precision": "DATE_ONLY",
                "deadline_timezone": "America/Chicago",
                "deadline_source_rank": 3,
            },
        )
        _, item = service.store_observation(db, course, first_raw)
        first_task = task_from_assignment(db, item, first_raw.structured)
        changed_raw = first_raw.model_copy(deep=True)
        changed_raw.structured["due_date_local"] = "2026-09-25"
        event, same_item = service.store_observation(db, course, changed_raw)
        changed_task = task_from_assignment(db, same_item, changed_raw.structured)
        assert event and event.change_type == "deadline_changed"
        assert changed_task.id == first_task.id
        assert changed_task.due_date_local == date(2026, 9, 25)
        assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_file_signatures_accept_pdf_and_office_and_reject_html() -> None:
    assert validate_course_file(b"%PDF-1.7\nbody", url="https://example.test/a.pdf").valid
    disguised = validate_course_file(
        b"<!DOCTYPE html><html>401 Unauthorized</html>",
        url="https://example.test/a.pdf",
        mime_type="text/html",
    )
    assert not disguised.valid and disguised.status == "INVALID_DOWNLOAD"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", "<document/>")
    assert validate_course_file(
        archive.getvalue(), url="https://example.test/notes.docx"
    ).valid


def _source(db: Session, course: Course, external_id: str, title: str) -> SourceItem:
    item = SourceItem(
        course_id=course.id,
        source_type="website",
        source_name="phys225_fa2026",
        external_id=external_id,
        item_type="website_file",
        title=title,
        current_hash="metadata",
    )
    db.add(item)
    db.flush()
    return item


def test_flat_export_integrity_dedup_and_current_version(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    export_root = tmp_path / "export"
    export_root.mkdir()
    downloader = FileDownloader(
        tmp_path / "archive",
        exports={"PHYS225": {"enabled": True, "flat": True, "path": str(export_root)}},
    )
    with Session(engine) as db:
        course = Course(
            source="website", external_id="p", course_code="PHYS225", name="Physics"
        )
        db.add(course)
        db.flush()
        lecture = _source(db, course, "lecture", "Lecture 1")
        homework = _source(db, course, "homework", "HW 1")
        solutions = _source(db, course, "solutions", "HW 1 solutions")
        first = downloader.save_bytes(
            db,
            course_code="PHYS225",
            course_id=course.id,
            source_item=lecture,
            url="https://example.test/lecture-01.pdf",
            content=b"%PDF-1.4\nlecture-v1",
            filename="Lecture 1",
            mime_type="application/pdf",
        )
        downloader.save_bytes(
            db,
            course_code="PHYS225",
            course_id=course.id,
            source_item=homework,
            url="https://example.test/HW-01.pdf",
            content=b"%PDF-1.4\nhomework",
        )
        downloader.save_bytes(
            db,
            course_code="PHYS225",
            course_id=course.id,
            source_item=solutions,
            url="https://example.test/HW-01-solutions.pdf",
            content=b"%PDF-1.4\nsolutions",
        )
        assert {path.name for path in export_root.iterdir()} == {
            "PHYS225_Lecture_01.pdf",
            "PHYS225_HW_01.pdf",
            "PHYS225_HW_01_Solutions.pdf",
        }
        assert not any(path.is_dir() for path in export_root.iterdir())
        exported = export_root / "PHYS225_Lecture_01.pdf"
        unchanged_mtime = exported.stat().st_mtime_ns
        duplicate = downloader.save_bytes(
            db,
            course_code="PHYS225",
            course_id=course.id,
            source_item=lecture,
            url="https://example.test/lecture-01.pdf",
            content=b"%PDF-1.4\nlecture-v1",
        )
        assert not duplicate.changed and exported.stat().st_mtime_ns == unchanged_mtime
        updated = downloader.save_bytes(
            db,
            course_code="PHYS225",
            course_id=course.id,
            source_item=lecture,
            url="https://example.test/lecture-01.pdf",
            content=b"%PDF-1.4\nlecture-v2",
        )
        assert first.record and updated.record
        assert first.record.local_path != updated.record.local_path
        assert exported.read_bytes().endswith(b"lecture-v2")


def test_invalid_download_is_not_exported(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    export_root = tmp_path / "export"
    export_root.mkdir()
    downloader = FileDownloader(
        tmp_path / "archive",
        exports={"PHYS225": {"enabled": True, "path": str(export_root)}},
    )
    with Session(engine) as db:
        course = Course(source="website", external_id="p", course_code="PHYS225", name="P")
        db.add(course)
        db.flush()
        item = _source(db, course, "bad", "HW 1")
        result = downloader.save_bytes(
            db,
            course_code="PHYS225",
            course_id=course.id,
            source_item=item,
            url="https://example.test/HW-01.pdf",
            content=b"<!doctype html><html>login</html>",
            mime_type="text/html",
        )
        assert result.record and result.record.integrity_status == "INVALID_DOWNLOAD"
        assert result.record.local_path == "" and not list(export_root.iterdir())


def test_interrupted_atomic_write_leaves_no_final_or_part(tmp_path: Path) -> None:
    class InterruptedDownloader(FileDownloader):
        def _write_part(self, path: Path, content: bytes) -> None:
            path.write_bytes(content[:5])
            raise OSError("simulated interruption")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="website", external_id="p", course_code="PHYS225", name="P")
        db.add(course)
        db.flush()
        item = _source(db, course, "partial", "HW 1")
        downloader = InterruptedDownloader(tmp_path / "archive")
        with pytest.raises(OSError, match="simulated interruption"):
            downloader.save_bytes(
                db,
                course_code="PHYS225",
                course_id=course.id,
                source_item=item,
                url="https://example.test/HW-01.pdf",
                content=b"%PDF-1.4\ncomplete",
            )
        assert not list(tmp_path.rglob("*.pdf"))
        assert not list(tmp_path.rglob("*.part"))


def test_change_list_is_lightweight_and_detail_has_before_after() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="website", external_id="p", course_code="PHYS225", name="P")
        db.add(course)
        db.flush()
        item = _source(db, course, "schedule#homework-1", "HW 1")
        old = SourceSnapshot(
            source_item_id=item.id,
            content_hash="a" * 64,
            structured_json={"due_date_local": "2026-09-03"},
            normalized_text="HW 1 due Thursday",
        )
        new = SourceSnapshot(
            source_item_id=item.id,
            content_hash="b" * 64,
            structured_json={"due_date_local": "2026-09-04"},
            normalized_text="HW 1 due Friday",
        )
        db.add_all([old, new])
        db.flush()
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="deadline_changed",
            old_snapshot_id=old.id,
            new_snapshot_id=new.id,
            importance="critical",
            requires_action=True,
            summary="Deadline changed.",
        )
        db.add(event)
        db.commit()
        listing = changes(limit=100, db=db)
        assert listing and not hasattr(listing[0], "old_snapshot")
        detail = change_detail(event.id, db)
        assert detail["course"]["course_code"] == "PHYS 225"
        assert course.course_code == "PHYS225"  # Presentation never changes source identity.
        assert detail["old"]["structured"]["due_date_local"] == "2026-09-03"
        assert detail["new"]["structured"]["due_date_local"] == "2026-09-04"
