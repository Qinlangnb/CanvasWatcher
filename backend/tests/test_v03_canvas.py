from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

from app.auth.models import CanvasCredential
from app.auth.store import CredentialStore
from app.config import Settings
from app.db import (
    Base,
    ChangeEvent,
    Course,
    CoursePolicy,
    CourseSource,
    CredentialProfile,
    DownloadedFile,
    SourceItem,
    Task,
    TaskSourceLink,
)
from app.schemas import CredentialProfileOut, DiscoveredCourse, RawSourceItem
from app.services.auth import AuthService
from app.services.sync import SyncService
from app.services.task_engine import task_from_assignment
from app.sources.canvas import CanvasAdapter
from app.sources.canvas_client import (
    CanvasAPIError,
    CanvasClient,
    CanvasDownload,
    CanvasErrorCode,
)


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


def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.mark.asyncio
async def test_canvas_pagination_collects_all_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        number = int(page)
        headers = {}
        if number < 3:
            headers["Link"] = (
                f'<https://canvas.example/api/v1/courses?page={number + 1}>; rel="next"'
            )
        return httpx.Response(
            200,
            json=[{"id": number}],
            headers=headers,
            request=request,
        )

    client = CanvasClient(
        "https://canvas.example",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    rows = await client.get_all_pages("/api/v1/courses")
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert "secret-token" not in repr(client)


@pytest.mark.asyncio
async def test_canvas_course_discovery_filters_inactive_rows() -> None:
    rows = [
        {"id": 1, "course_code": "PHYS 225", "name": "Physics", "workflow_state": "available"},
        {"id": 2, "course_code": "OLD 100", "name": "Old", "workflow_state": "completed"},
        {"id": 3, "course_code": "LOCKED", "name": "Locked", "access_restricted_by_date": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows, request=request)

    adapter = CanvasAdapter(
        "https://canvas.example",
        "token",
        client=CanvasClient(
            "https://canvas.example",
            "token",
            transport=httpx.MockTransport(handler),
        ),
    )
    discovered = await adapter.discover_courses()
    assert [row.external_id for row in discovered] == ["1"]


class ProbeClient:
    result = {"id": 42, "name": "Example Student", "login_id": "student"}
    error: CanvasAPIError | None = None

    def __init__(self, base_url: str, token: str, max_concurrency: int):
        del base_url, token, max_concurrency

    async def probe(self):
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_canvas_token_is_verified_in_memory_and_never_serialized(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.services.auth.CanvasClient", ProbeClient)
    store = CredentialStore()
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={},
            state="AUTH_REQUIRED",
        )
        db.add(profile)
        db.commit()
        service = AuthService(settings(tmp_path), store=store)
        verified = await service.set_canvas_and_verify(
            db, profile, "https://canvas.example", "top-secret"
        )
        assert verified
        assert profile.state == "ACTIVE"
        assert store.get_canvas("canvas_uiuc").token == "top-secret"
        serialized = CredentialProfileOut.model_validate(profile).model_dump_json()
        assert "top-secret" not in serialized
        assert "token" not in {column.name for column in inspect(Base.metadata.tables["credential_profiles"]).columns}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        (CanvasErrorCode.INVALID_TOKEN, "FAILED"),
        (CanvasErrorCode.PERMISSION_DENIED, "FAILED"),
        (CanvasErrorCode.TIMEOUT, "DEGRADED"),
        (CanvasErrorCode.SERVER_ERROR, "DEGRADED"),
    ],
)
async def test_canvas_probe_distinguishes_failures(
    tmp_path: Path, monkeypatch, code: CanvasErrorCode, expected_state: str
) -> None:
    class FailingProbe(ProbeClient):
        error = CanvasAPIError(code, "safe failure")

    monkeypatch.setattr("app.services.auth.CanvasClient", FailingProbe)
    store = CredentialStore()
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={},
            state="AUTH_REQUIRED",
        )
        db.add(profile)
        db.commit()
        service = AuthService(settings(tmp_path), store=store)
        assert not await service.set_canvas_and_verify(
            db, profile, "https://canvas.example", "secret"
        )
        assert profile.state == expected_state
        assert profile.last_error_code == code.value


def test_legacy_env_token_and_memory_precedence(tmp_path: Path) -> None:
    service = AuthService(
        settings(tmp_path, canvas_base_url="https://canvas.example", canvas_credential_id="canvas_uiuc", canvas_access_token=SecretStr("environment-token")),
        store=CredentialStore(),
    )
    assert service.canvas_credential().token == "environment-token"
    service.store.set_canvas("canvas_uiuc", "https://memory.example", "memory-token")
    credential = service.canvas_credential()
    assert credential == CanvasCredential("https://memory.example", "memory-token")


@pytest.mark.asyncio
async def test_discovery_links_canvas_to_existing_canonical_course(
    tmp_path: Path, monkeypatch
) -> None:
    async def discovered(_adapter):
        return [
            DiscoveredCourse(
                source="canvas",
                external_id="123",
                course_code="PHYS 225",
                name="Physics 225 Canvas",
                metadata={"id": 123},
            )
        ]

    monkeypatch.setattr(CanvasAdapter, "discover_courses", discovered)
    store = CredentialStore()
    store.set_canvas("canvas_uiuc", "https://canvas.example", "token")
    with memory_db() as db:
        website_course = Course(
            source="website",
            external_id="configured:PHYS225",
            course_code="PHYS225",
            name="Physics 225",
        )
        db.add(website_course)
        db.commit()
        pairs = await SyncService(settings(tmp_path)).discover_canvas(
            db, CanvasCredential("https://canvas.example", "token")
        )
        assert len(pairs) == 1
        assert pairs[0][0].id == website_course.id
        assert db.scalar(select(func.count()).select_from(Course)) == 1
        source = db.scalar(select(CourseSource))
        assert source.external_id == "123"
        assert source.course_id == website_course.id


def test_cross_source_homework_links_to_one_task_with_canvas_exact_time(
    tmp_path: Path,
) -> None:
    with memory_db() as db:
        course = Course(
            source="website",
            external_id="configured:PHYS225",
            course_code="PHYS225",
            name="Physics",
        )
        db.add(course)
        db.flush()
        service = SyncService(settings(tmp_path))
        website = RawSourceItem(
            source_type="website",
            source_name="phys225",
            external_id="schedule-hw-3",
            item_type="assignment",
            title="HW 3",
            structured={
                "source_key": "homework:3",
                "due_date_local": "2026-09-17",
                "deadline_precision": "DATE_ONLY",
                "deadline_source_rank": 4,
            },
        )
        _, website_item = service.store_observation(db, course, website)
        task_from_assignment(db, website_item, website.structured)
        canvas = RawSourceItem(
            source_type="canvas",
            source_name="canvas:123",
            external_id="assignment:77",
            item_type="assignment",
            title="Homework 3",
            structured={
                "source_key": "homework:3",
                "due_at": "2026-09-18T04:59:00Z",
                "deadline_precision": "EXACT_DATETIME",
                "deadline_source_rank": 1,
                "submission": {"workflow_state": "submitted", "submitted_at": "2026-09-17T20:00:00Z"},
            },
        )
        _, canvas_item = service.store_observation(db, course, canvas)
        task = task_from_assignment(db, canvas_item, canvas.structured)
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(func.count()).select_from(TaskSourceLink)) == 2
        assert task.deadline_precision == "EXACT_DATETIME"
        assert task.due_at.isoformat().startswith("2026-09-18T04:59")
        assert task.status == "SUBMITTED"


def canvas_file(
    *,
    download_url: str,
    modified_at: str = "2026-08-01T00:00:00Z",
    display_name: str = "lecture.pdf",
) -> RawSourceItem:
    return RawSourceItem(
        source_type="canvas",
        source_name="canvas:123",
        external_id="file:100",
        item_type="file",
        title=display_name,
        url="https://canvas.example/courses/123/files/100",
        structured={
            "id": 100,
            "display_name": display_name,
            "filename": display_name,
            "modified_at": modified_at,
            "size": 16,
            "content-type": "application/pdf",
        },
        is_file=True,
        download_url=download_url,
        content_type="application/pdf",
    )


@pytest.mark.asyncio
async def test_canvas_file_identity_versions_and_signed_url_churn(
    tmp_path: Path,
) -> None:
    pdf_v1 = b"%PDF-1.4\nfirst"
    pdf_v2 = b"%PDF-1.4\nsecond"
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="TEST100",
            name="Test",
        )
        db.add(course)
        db.flush()
        adapter = CanvasAdapter(
            "https://canvas.example", "token", canvas_course_id="123"
        )
        service = SyncService(settings(tmp_path))
        current = pdf_v1

        async def fetch(_raw):
            return CanvasDownload(
                current,
                200,
                "application/pdf",
                len(current),
                None,
                None,
            )

        adapter.fetch_file_result = fetch
        first = canvas_file(download_url="https://canvas.example/files/100?verifier=a")
        event, item = service.store_observation(
            db, course, first, emit_initial_event=False
        )
        assert event is None
        await service._sync_file(db, course, adapter, item, first, event, True)
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1

        signed_url_only = canvas_file(
            download_url="https://canvas.example/files/100?verifier=b"
        )
        event, _ = service.store_observation(db, course, signed_url_only)
        assert event is None
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1

        current = pdf_v2
        changed = canvas_file(
            download_url="https://canvas.example/files/100?verifier=c",
            modified_at="2026-08-02T00:00:00Z",
        )
        event, item = service.store_observation(db, course, changed)
        content_event = await service._sync_file(
            db, course, adapter, item, changed, event, False
        )
        assert content_event is None
        assert event.change_type == "CANVAS_FILE_UPDATED"
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 2

        renamed = canvas_file(
            download_url="https://canvas.example/files/100?verifier=d",
            modified_at="2026-08-03T00:00:00Z",
            display_name="renamed.pdf",
        )
        event, item = service.store_observation(db, course, renamed)
        assert event.change_type == "CANVAS_FILE_RENAMED"
        await service._sync_file(db, course, adapter, item, renamed, event, False)
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 2


@pytest.mark.asyncio
async def test_canvas_invalid_file_never_becomes_valid(tmp_path: Path) -> None:
    html = b"<html>login required</html>"
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="TEST100",
            name="Test",
        )
        db.add(course)
        db.flush()
        adapter = CanvasAdapter(
            "https://canvas.example", "token", canvas_course_id="123"
        )

        async def fetch(_raw):
            return CanvasDownload(
                html, 200, "text/html", len(html), None, None
            )

        adapter.fetch_file_result = fetch
        service = SyncService(settings(tmp_path))
        raw = canvas_file(download_url="https://canvas.example/files/100")
        event, item = service.store_observation(
            db, course, raw, emit_initial_event=False
        )
        invalid_event = await service._sync_file(
            db, course, adapter, item, raw, event, True
        )
        record = db.scalar(select(DownloadedFile))
        assert record.integrity_status == "INVALID_DOWNLOAD"
        assert not record.local_path
        assert invalid_event.change_type == "CANVAS_FILE_INVALID"


def test_canvas_reconciliation_marks_removed_file(tmp_path: Path) -> None:
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="TEST100",
            name="Test",
        )
        source = CourseSource(
            course_id=1,
            name="canvas:123",
            source_type="canvas",
            external_id="123",
            enabled=True,
        )
        db.add(course)
        db.flush()
        source.course_id = course.id
        db.add(source)
        db.flush()
        item = SourceItem(
            course_id=course.id,
            course_source_id=source.id,
            source_type="canvas",
            source_name="canvas:123",
            external_id="file:100",
            item_type="file",
            title="old.pdf",
            current_hash="a" * 64,
        )
        db.add(item)
        db.commit()
        adapter = CanvasAdapter(
            "https://canvas.example",
            "token",
            canvas_course_id="123",
            course_source_id=source.id,
        )
        count = SyncService(settings(tmp_path))._reconcile_canvas(
            db, course, adapter, {}, {"file"}, emit_events=True
        )
        assert count == 1
        assert item.is_deleted
        assert db.scalar(select(ChangeEvent)).change_type == "CANVAS_FILE_REMOVED"


def test_module_item_relationship_is_not_a_duplicate_file(tmp_path: Path) -> None:
    adapter = CanvasAdapter(
        "https://canvas.example", "token", canvas_course_id="123"
    )
    module_item = adapter._normalize(
        "module_item",
        {
            "id": 9,
            "title": "Lecture",
            "type": "File",
            "content_id": 100,
            "url": "https://canvas.example/api/v1/courses/123/modules/items/9",
        },
        "123",
        {},
    )
    assert module_item.item_type == "module_item"
    assert not module_item.is_file
    assert module_item.structured["content_id"] == 100


@pytest.mark.asyncio
async def test_initial_canvas_sync_builds_baseline_without_change_spam(
    tmp_path: Path,
) -> None:
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="TEST100",
            name="Test",
        )
        db.add(course)
        db.flush()
        source = CourseSource(
            course_id=course.id,
            name="canvas:123",
            source_type="canvas",
            external_id="123",
            enabled=True,
            state="NEW",
            config_json={"baseline_complete": False},
        )
        db.add(source)
        db.flush()
        adapter = CanvasAdapter(
            "https://canvas.example",
            "token",
            canvas_course_id="123",
            course_source_id=source.id,
        )

        async def fetch_items(_course, since=None):
            del since
            adapter.successful_item_types = {"assignment", "announcement"}
            adapter.errors = {}
            return [
                RawSourceItem(
                    source_type="canvas",
                    source_name="canvas:123",
                    external_id="assignment:1",
                    item_type="assignment",
                    title="Essay",
                    structured={
                        "source_key": "canvas_assignment:123:1",
                        "due_at": "2026-09-10T04:59:00Z",
                        "deadline_precision": "EXACT_DATETIME",
                        "deadline_source_rank": 1,
                    },
                ),
                RawSourceItem(
                    source_type="canvas",
                    source_name="canvas:123",
                    external_id="announcement:2",
                    item_type="announcement",
                    title="Welcome",
                    structured={"id": 2, "message": "Welcome"},
                    normalized_text="Welcome",
                ),
            ]

        adapter.fetch_items = fetch_items
        state = await SyncService(settings(tmp_path)).run(db, [(course, adapter)])
        assert state.state == "healthy"
        assert state.changes == 0
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 0
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert source.config_json["baseline_complete"] is True


@pytest.mark.asyncio
async def test_canvas_endpoint_failure_preserves_successful_collections(
    tmp_path: Path,
) -> None:
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="TEST100",
            name="Test",
        )
        db.add(course)
        db.flush()
        source = CourseSource(
            course_id=course.id,
            name="canvas:123",
            source_type="canvas",
            external_id="123",
            enabled=True,
            state="HEALTHY",
            config_json={"baseline_complete": True},
        )
        db.add(source)
        db.flush()
        adapter = CanvasAdapter(
            "https://canvas.example",
            "token",
            canvas_course_id="123",
            course_source_id=source.id,
        )

        async def partial(_course, since=None):
            del since
            adapter.successful_item_types = {"assignment"}
            adapter.errors = {"page": "server_error"}
            return [
                RawSourceItem(
                    source_type="canvas",
                    source_name="canvas:123",
                    external_id="assignment:1",
                    item_type="assignment",
                    title="Project",
                    structured={
                        "source_key": "canvas_assignment:123:1",
                        "due_at": "2026-10-01T04:59:00Z",
                        "deadline_precision": "EXACT_DATETIME",
                        "deadline_source_rank": 1,
                    },
                )
            ]

        adapter.fetch_items = partial
        state = await SyncService(settings(tmp_path)).run(db, [(course, adapter)])
        assert state.state == "degraded"
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert source.state == "DEGRADED"
        assert "page:server_error" in source.last_error


def test_canvas_page_semantic_change_and_structured_grading_policy(
    tmp_path: Path,
) -> None:
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="TEST100",
            name="Test",
        )
        db.add(course)
        db.flush()
        service = SyncService(settings(tmp_path))
        base = {
            "source_type": "canvas",
            "source_name": "canvas:123",
            "external_id": "page:introduction",
            "item_type": "page",
            "title": "Introduction",
        }
        service.store_observation(
            db,
            course,
            RawSourceItem(
                **base,
                structured={"page_id": 1, "body": "<p>Read chapter one.</p>"},
                normalized_text="Read chapter one.",
            ),
            emit_initial_event=False,
        )
        event, _ = service.store_observation(
            db,
            course,
            RawSourceItem(
                **base,
                structured={"page_id": 1, "body": "<p>Read chapters one and two.</p>"},
                normalized_text="Read chapters one and two.",
            ),
        )
        assert event.change_type == "CANVAS_PAGE_UPDATED"

        group = RawSourceItem(
            source_type="canvas",
            source_name="canvas:123",
            external_id="assignment_group:7",
            item_type="assignment_group",
            title="Homework",
            structured={
                "id": 7,
                "group_weight": 35,
                "rules": {"drop_lowest": 1, "never_drop": [99]},
            },
        )
        service._apply_canvas_grading_policy(db, course, [group])
        policy = db.scalar(select(CoursePolicy))
        assert policy.policy_json["grading_authority"] == "canvas_structured"
        assert policy.policy_json["grading_categories"][0]["drop_lowest"] == 1
        assert policy.confidence == 1.0
