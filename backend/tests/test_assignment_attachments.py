from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import router
from app.db import (
    Base,
    Course,
    DownloadedFile,
    SourceItem,
    SourceSnapshot,
    Task,
    TaskSourceLink,
    get_db,
)
from app.services.task_attachments import task_attachments
from app.sources.canvas import CanvasAdapter, canvas_file_id_from_reference
from app.sources.canvas_client import CanvasClient


@pytest.mark.parametrize(
    "path",
    [
        "/courses/7/files/9",
        "/courses/7/files/9/preview",
        "/courses/7/files/9/download?download_frd=1",
        "/api/v1/files/9",
        "/files/9/download",
    ],
)
def test_file_url_forms(path):
    assert (
        canvas_file_id_from_reference(path, course_id="7", base_url="https://canvas.example") == "9"
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/files/9",
        "/courses/8/files/9",
        "/courses/7/assignments/9",
        "javascript:alert(1)",
        "/files/9/other",
    ],
)
def test_reject_unrelated_or_external_links(path):
    assert (
        canvas_file_id_from_reference(path, course_id="7", base_url="https://canvas.example")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pages", [False, True])
async def test_hidden_assignment_file_without_pages_and_dedup(pages):
    calls = []
    body = '<a href="/courses/7/files/9/preview" data-api-endpoint="/api/v1/files/9">HW PDF</a>'

    def handler(request):
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/assignments"):
            value = [
                {"id": 5, "name": "HW 1", "due_at": "2026-10-09T05:00:00Z", "description": body}
            ]
        elif path.endswith("/pages"):
            value = [{"id": 1, "url": "intro"}] if pages else []
        elif path.endswith("/pages/intro"):
            value = {"id": 1, "url": "intro", "body": body}
        elif path.endswith("/files/9"):
            value = {
                "id": 9,
                "course_id": 7,
                "display_name": "hw1.pdf",
                "url": "https://canvas.example/files/9/download",
            }
        elif path == "/api/v1/courses/7":
            value = {"id": 7}
        else:
            value = []
        return httpx.Response(200, json=value)

    adapter = CanvasAdapter(
        "https://canvas.example",
        canvas_course_id="7",
        client=CanvasClient(
            "https://canvas.example", "test", transport=httpx.MockTransport(handler)
        ),
    )
    items = await adapter.fetch_items(Course(external_id="7"))
    assignment = next(item for item in items if item.item_type == "assignment")
    assert assignment.structured["attachment_file_ids"] == ["9"]
    assert assignment.structured["due_at"] == "2026-10-09T05:00:00Z"
    files = [item for item in items if item.is_file]
    assert len(files) == 1 and files[0].external_id == "file:9"
    assert files[0].structured["discovered_via_assignments"] == ["5"]
    assert calls.count("/api/v1/courses/7/files/9") == 1


@pytest.mark.asyncio
async def test_metadata_identity_failure_keeps_assignment():
    adapter = CanvasAdapter("https://canvas.example", canvas_course_id="7")

    async def bad(*args, **kwargs):
        return {"id": 999, "course_id": 8}

    adapter.client.get_json = bad
    adapter.successful_item_types.add("file")
    grouped = {
        "assignment": [{"id": 5, "description": '<a href="/files/9">HW</a>', "due_at": "future"}]
    }
    await adapter._merge_page_files("7", grouped)
    assert grouped["file"] == [] and grouped["assignment"][0]["due_at"] == "future"
    assert "page_file:9" in adapter.errors
    assert "file" not in adapter.successful_item_types


@pytest.mark.asyncio
async def test_listed_attachment_does_not_fetch_again():
    adapter = CanvasAdapter("https://canvas.example")

    async def unexpected(*args, **kwargs):
        raise AssertionError("Listed file should not be fetched again")

    adapter.client.get_json = unexpected
    grouped = {
        "assignment": [{"id": 5, "description": '<a href="/files/9">HW</a>'}],
        "file": [{"id": 9, "display_name": "hw.pdf"}],
    }
    await adapter._merge_page_files("7", grouped)
    assert len(grouped["file"]) == 1
    assert grouped["assignment"][0]["attachment_file_ids"] == ["9"]


def test_partial_description_preserves_last_good_attachment_links(tmp_path):
    from app.config import Settings
    from app.services.sync import SyncService

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="7", course_code="TEST", name="Test")
        db.add(course)
        db.flush()
        service = SyncService(Settings(database_url="sqlite:///:memory:", download_root=tmp_path))
        adapter = CanvasAdapter("https://canvas.example")
        raw = adapter._normalize(
            "assignment",
            {
                "id": 5,
                "name": "HW",
                "description": "HW",
                "attachment_file_ids": ["9"],
                "due_at": "2026-10-09T05:00:00Z",
            },
            "7",
            {},
        )
        service.store_observation(db, course, raw)
        partial = adapter._normalize(
            "assignment", {"id": 5, "name": "HW", "due_at": "2026-10-10T05:00:00Z"}, "7", {}
        )
        service.store_observation(db, course, partial)
        assert partial.structured["attachment_file_ids"] == ["9"]
        assert partial.structured["description_state"] == "retained"


def seed(db, tmp_path):
    course = Course(source="canvas", external_id="7", course_code="TEST", name="Test")
    db.add(course)
    db.flush()
    task = Task(course_id=course.id, title="HW", due_at=datetime.now(UTC) + timedelta(hours=1))
    assignment = SourceItem(
        course_id=course.id,
        source_type="canvas",
        source_name="canvas:7",
        external_id="assignment:5",
        item_type="assignment",
        title="HW",
        current_hash="x",
    )
    file_item = SourceItem(
        course_id=course.id,
        source_type="canvas",
        source_name="canvas:7",
        external_id="file:9",
        item_type="file",
        title="hw.pdf",
        current_hash="x",
    )
    db.add_all([task, assignment, file_item])
    db.flush()
    db.add(TaskSourceLink(task_id=task.id, source_item_id=assignment.id))
    snapshot = SourceSnapshot(
        source_item_id=assignment.id,
        content_hash="x",
        structured_json={"attachment_file_ids": ["9", "9", "10"]},
    )
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    file = DownloadedFile(
        course_id=course.id,
        source_item_id=file_item.id,
        source_url="https://canvas.example/files/9",
        original_filename="hw.pdf",
        local_path=str(path),
        size_bytes=path.stat().st_size,
        sha256="fixture",
        integrity_status="VALID",
        mime_type="application/pdf",
    )
    db.add_all([snapshot, file])
    db.flush()
    return task, assignment, file_item, file, snapshot


def test_exact_source_and_latest_version_only(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task, assignment, item, file, snapshot = seed(db, tmp_path)
        assert task_attachments(db, [task.id])[task.id] == [
            {"file_id": file.id, "filename": "hw.pdf"}
        ]
        item.source_name = "canvas:8"
        db.flush()
        assert task_attachments(db, [task.id])[task.id] == []
        item.source_name = assignment.source_name
        db.add(
            DownloadedFile(
                course_id=task.course_id,
                source_item_id=item.id,
                source_url=file.source_url,
                original_filename="hw.pdf",
                local_path=file.local_path,
                size_bytes=0,
                sha256="bad",
                version_number=2,
                integrity_status="INVALID_DOWNLOAD",
            )
        )
        db.flush()
        assert task_attachments(db, [task.id])[task.id] == []


@pytest.mark.parametrize(
    "failure", ["deleted", "missing", "wrong_course", "wrong_source", "cleared"]
)
def test_attachment_scope_and_availability(tmp_path, failure):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task, assignment, item, file, snapshot = seed(db, tmp_path)
        if failure == "deleted":
            assignment.is_deleted = True
        elif failure == "missing":
            file.local_path = str(tmp_path / "not-downloaded.pdf")
        elif failure == "wrong_course":
            item.course_id = task.course_id + 1
        elif failure == "wrong_source":
            item.course_source_id = 999
        elif failure == "cleared":
            db.add(
                SourceSnapshot(
                    source_item_id=assignment.id,
                    content_hash="new",
                    structured_json={"attachment_file_ids": []},
                )
            )
        db.flush()
        assert task_attachments(db, [task.id])[task.id] == []


@pytest.mark.asyncio
async def test_today_and_content_http_contract(tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        task, assignment, item, file, snapshot = seed(db, tmp_path)
        db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/today")
            assert response.status_code == 200
            assert response.json()["work"][0]["attachments"] == [
                {"file_id": file.id, "filename": "hw.pdf"}
            ]
            content = await client.get(f"/api/files/{file.id}/content")
            assert content.status_code == 200 and content.content.startswith(b"%PDF")
            assert "attachment" in content.headers["content-disposition"]
            file.integrity_status = "INVALID_DOWNLOAD"
            db.commit()
            assert (await client.get(f"/api/files/{file.id}/content")).status_code == 404
            assert (await client.get("/api/today")).json()["work"][0]["attachments"] == []
