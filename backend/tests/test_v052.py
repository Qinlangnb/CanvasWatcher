from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai.queue import AnalysisWorker, enqueue_change_analysis
from app.api.routes import changes, unread_change_count
from app.config import Settings
from app.db import (
    Base,
    ChangeAnalysis,
    ChangeEvent,
    Course,
    DownloadedFile,
    Notification,
    SourceItem,
    SourceSnapshot,
)
from app.schemas import RawSourceItem
from app.services.change_policy import (
    is_user_facing_event,
    meaningful_value,
    should_emit_change,
)
from app.services.downloader import FileDownloader
from app.services.notifier import NtfyNotifier
from app.services.sync import SyncService
from app.services.today import TodayEngine
from app.sources.canvas import (
    CanvasAdapter,
    canvas_file_id_from_reference,
    extract_canvas_page_file_ids,
)
from app.sources.canvas_client import CanvasDownload


def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        download_root=tmp_path / "downloads",
        courses_config=tmp_path / "courses.yaml",
        file_rules_config=tmp_path / "rules.yaml",
        availability_config=tmp_path / "availability.yaml",
        scheduler_enabled=False,
        **overrides,
    )


def add_course_item(db: Session, item_type: str = "assignment") -> tuple[Course, SourceItem]:
    course = Course(
        source="canvas",
        external_id="canvas:72641",
        course_code="PHYS225",
        display_course_code="PHYS 225",
        name="Physics",
        lifecycle_state="ACTIVE",
    )
    db.add(course)
    db.flush()
    item = SourceItem(
        course_id=course.id,
        source_type="canvas",
        source_name="canvas:72641",
        external_id=f"{item_type}:1",
        item_type=item_type,
        title="Midterm room changed",
        current_hash="a" * 64,
    )
    db.add(item)
    db.flush()
    return course, item


@pytest.mark.parametrize(
    "reference",
    [
        "/courses/72641/files/12345",
        "/courses/72641/files/12345/download",
        "/api/v1/courses/72641/files/12345",
        "/api/v1/files/12345",
    ],
)
def test_canvas_file_references_normalize_to_stable_id(reference: str) -> None:
    assert canvas_file_id_from_reference(
        reference,
        course_id="72641",
        base_url="https://canvas.illinois.edu",
    ) == "12345"


def test_canvas_page_parser_prefers_api_metadata_and_ignores_external_links() -> None:
    body = """
    <a data-api-returntype="File"
       data-api-endpoint="/api/v1/courses/72641/files/12345"
       href="/courses/72641/files/12345/download">Lecture 01</a>
    <iframe src="/api/v1/files/67890"></iframe>
    <object data="https://canvas.illinois.edu/courses/72641/files/24680"></object>
    <a href="https://external.example/courses/72641/files/99999">External</a>
    <a href="/courses/999/files/77777">Other course</a>
    """
    assert extract_canvas_page_file_ids(
        body,
        course_id="72641",
        base_url="https://canvas.illinois.edu",
    ) == ["12345", "67890", "24680"]


class PageFileClient:
    def __init__(self):
        self.calls: list[str] = []

    async def get_json(self, path: str):
        self.calls.append(path)
        return {
            "id": 12345,
            "display_name": "lecture-01.pdf",
            "url": "https://canvas.illinois.edu/files/12345/download",
            "content-type": "application/pdf",
            "size": 10,
        }


@pytest.mark.asyncio
async def test_page_discovered_file_merges_with_existing_file_identity() -> None:
    client = PageFileClient()
    adapter = CanvasAdapter(
        "https://canvas.illinois.edu",
        "token",
        canvas_course_id="72641",
        client=client,  # type: ignore[arg-type]
    )
    grouped = {
        "page": [
            {
                "url": "lecture-slides",
                "title": "Lecture Slides",
                "body": '<a href="/courses/72641/files/12345/download">Slides</a>',
            }
        ],
        "file": [],
    }
    await adapter._merge_page_files("72641", grouped)
    assert client.calls == ["/api/v1/courses/72641/files/12345"]
    assert len(grouped["file"]) == 1
    assert grouped["file"][0]["id"] == 12345
    assert grouped["file"][0]["page_context_titles"] == ["Lecture Slides"]

    await adapter._merge_page_files("72641", grouped)
    assert client.calls == ["/api/v1/courses/72641/files/12345"]
    assert len(grouped["file"]) == 1


def test_page_context_classifies_only_when_file_name_is_generic(tmp_path: Path) -> None:
    with memory_db() as db:
        course, item = add_course_item(db, "file")
        result = FileDownloader(tmp_path / "downloads").save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url="https://canvas.illinois.edu/api/v1/files/12345",
            content=b"%PDF-1.4\n%%EOF",
            filename="notes.pdf",
            classification_context="Lecture Slides",
            mime_type="application/pdf",
        )
        assert result.record is not None
        assert result.record.source_detected_type == "lecture"


def test_system_discovery_removal_and_empty_side_policy() -> None:
    assert should_emit_change("SYSTEM", {"ok": True}, {"ok": False}) is False
    assert should_emit_change("CANVAS_FILE_ADDED", None, {"id": 1}) is False
    assert should_emit_change("CANVAS_PAGE_REMOVED", {"id": 1}, None) is False
    assert should_emit_change("updated", None, {"title": "value"}) is False
    assert should_emit_change("updated", {"title": "value"}, None) is False
    assert should_emit_change("updated", {"title": "old"}, {"title": "new"}) is True
    assert should_emit_change("CANVAS_ANNOUNCEMENT_CREATED", None, {"title": "Room"}) is True
    assert should_emit_change("CANVAS_ASSIGNMENT_CREATED", None, {"title": "HW"}) is True
    assert meaningful_value({"value": "   "}) is False


@pytest.mark.asyncio
async def test_system_events_are_excluded_from_list_unread_today_notification_and_ai_queue() -> None:
    with memory_db() as db:
        _course, item = add_course_item(db)
        old = SourceSnapshot(
            source_item_id=item.id,
            content_hash="b" * 64,
            structured_json={"fetch_status": "ok"},
            normalized_text="ok",
        )
        new = SourceSnapshot(
            source_item_id=item.id,
            content_hash="c" * 64,
            structured_json={"fetch_status": "failed"},
            normalized_text="failed",
        )
        db.add_all([old, new])
        db.flush()
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="SYSTEM_FETCH_STATUS_CHANGED",
            old_snapshot_id=old.id,
            new_snapshot_id=new.id,
            summary="technical",
            requires_action=True,
        )
        db.add(event)
        db.flush()
        assert is_user_facing_event(db, event) is False
        assert enqueue_change_analysis(db, event) is None
        db.commit()
        assert changes(limit=100, course_id=None, include_archived=False, db=db) == []
        assert unread_change_count(include_archived=False, db=db) == {"count": 0}
        assert TodayEngine().build(db)["attention"] == []
        assert await NtfyNotifier("", "").notify_change(db, event, "technical") is None
        assert db.scalar(select(func.count()).select_from(Notification)) == 0


def test_generic_canvas_discovery_stores_snapshot_without_change(tmp_path: Path) -> None:
    with memory_db() as db:
        course, _item = add_course_item(db)
        raw = RawSourceItem(
            source_type="canvas",
            source_name="canvas:72641",
            external_id="page:lecture-slides",
            item_type="page",
            title="Lecture Slides",
            structured={"url": "lecture-slides", "body": "links"},
            normalized_text="links",
        )
        event, stored = SyncService(settings(tmp_path)).store_observation(db, course, raw)
        assert event is None
        assert stored.id is not None
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 1
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 0


class StaticFileAdapter(CanvasAdapter):
    async def fetch_file_result(self, item: RawSourceItem) -> CanvasDownload:
        del item
        content = b"%PDF-1.4\n%%EOF"
        return CanvasDownload(
            content=content,
            status_code=200,
            content_type="application/pdf",
            content_length=len(content),
            etag=None,
            last_modified=None,
        )


@pytest.mark.asyncio
async def test_new_canvas_file_is_downloaded_without_generic_change(tmp_path: Path) -> None:
    with memory_db() as db:
        course, _item = add_course_item(db)
        raw = RawSourceItem(
            source_type="canvas",
            source_name="canvas:72641",
            external_id="file:12345",
            item_type="file",
            title="Lecture 01.pdf",
            url="https://canvas.illinois.edu/courses/72641/files/12345",
            structured={"id": 12345, "display_name": "Lecture 01.pdf"},
            is_file=True,
            download_url="https://canvas.illinois.edu/api/v1/files/12345",
            content_type="application/pdf",
        )
        service = SyncService(settings(tmp_path))
        metadata_event, item = service.store_observation(db, course, raw)
        assert metadata_event is None
        event = await service._sync_file(
            db,
            course,
            StaticFileAdapter(
                "https://canvas.illinois.edu",
                "token",
                canvas_course_id="72641",
            ),
            item,
            raw,
            metadata_event,
            baseline=False,
        )
        assert event is None
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 0
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1


class TrivialAssignmentProvider:
    calls = 0

    async def structured_generate(self, *, system_prompt, user_prompt, response_model):
        del system_prompt, user_prompt
        self.calls += 1
        return response_model(notify_user=False, importance="low", reason="No academic requirement changed.")


@pytest.mark.asyncio
async def test_trivial_assignment_change_stays_in_history_without_notification(
    tmp_path: Path,
) -> None:
    with memory_db() as db:
        _course, item = add_course_item(db)
        old = SourceSnapshot(
            source_item_id=item.id,
            content_hash="d" * 64,
            structured_json={"description": "Read chapter 1"},
            normalized_text="Read chapter 1",
        )
        new = SourceSnapshot(
            source_item_id=item.id,
            content_hash="e" * 64,
            structured_json={"description": "Please read chapter 1"},
            normalized_text="Please read chapter 1",
        )
        db.add_all([old, new])
        db.flush()
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="CANVAS_ASSIGNMENT_UPDATED",
            old_snapshot_id=old.id,
            new_snapshot_id=new.id,
            summary="Changed fields: description",
            importance="important",
            requires_action=True,
        )
        db.add(event)
        db.flush()
        assert enqueue_change_analysis(db, event) is not None
        db.commit()
        worker = AnalysisWorker(settings(tmp_path, llm_provider="mock"))
        provider = TrivialAssignmentProvider()
        worker.service.provider = provider

        assert await worker.run_pending(db) == 1
        assert provider.calls == 1
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
        assert len(changes(limit=100, course_id=None, include_archived=False, db=db)) == 1
        assert TodayEngine().build(db)["attention"] == []


@pytest.mark.asyncio
async def test_ai_unavailable_critical_assignment_waits_without_deterministic_push(
    tmp_path: Path,
) -> None:
    with memory_db() as db:
        _course, item = add_course_item(db)
        old = SourceSnapshot(
            source_item_id=item.id,
            content_hash="f" * 64,
            structured_json={"due_at": "2026-09-10T18:00:00Z"},
            normalized_text="Sep 10",
        )
        new = SourceSnapshot(
            source_item_id=item.id,
            content_hash="1" * 64,
            structured_json={"due_at": "2026-09-08T18:00:00Z"},
            normalized_text="Sep 8",
        )
        db.add_all([old, new])
        db.flush()
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="CANVAS_DEADLINE_CHANGED",
            old_snapshot_id=old.id,
            new_snapshot_id=new.id,
            summary="Deadline changed",
            importance="critical",
            requires_action=True,
        )
        db.add(event)
        db.flush()
        assert enqueue_change_analysis(db, event) is not None
        db.commit()

        worker = AnalysisWorker(settings(tmp_path, llm_provider="disabled"))
        assert await worker.run_pending(db) == 0
        analysis = db.scalar(
            select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id)
        )
        assert analysis.analysis_status == "PENDING"
        assert analysis.analysis_json["notify_user"] is False
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
        assert TodayEngine().build(db)["attention"] == []


def test_changes_card_payload_is_content_first_and_diff_is_semantic() -> None:
    with memory_db() as db:
        _course, item = add_course_item(db, "announcement")
        item.title = "Midterm room changed to Loomis 141"
        snapshot = SourceSnapshot(
            source_item_id=item.id,
            content_hash="2" * 64,
            structured_json={"title": item.title, "message": "Please use the east entrance."},
            normalized_text="Please use the east entrance.",
        )
        db.add(snapshot)
        db.flush()
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="CANVAS_ANNOUNCEMENT_CREATED",
            new_snapshot_id=snapshot.id,
            summary="New source item detected.",
        )
        db.add(event)
        db.commit()

        rows = changes(limit=100, course_id=None, include_archived=False, db=db)
        assert rows[0]["category"] == "announcement"
        assert rows[0]["primary_text"].startswith(
            "PHYS 225: Midterm room changed to Loomis 141"
        )
        assert rows[0]["event_label"] == "New source item detected"
        assert rows[0]["has_meaningful_diff"] is False
