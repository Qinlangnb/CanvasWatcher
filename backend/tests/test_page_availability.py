from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, Course, CourseSource, SourceItem, Task
from app.services.source_health import source_health_view
from app.services.sync import SyncService
from app.sources.canvas import CanvasAdapter
from app.sources.canvas_client import CanvasClient


def adapter_fixture(
    *,
    status=404,
    tabs=None,
    tab_status=200,
    modules=None,
    module_failure=False,
    assignment_failure=False,
    detail_failure=False,
):
    state = {"status": status, "tabs": [] if tabs is None else tabs}
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/pages"):
            return httpx.Response(
                state["status"], json=[{"page_id": 2, "url": "intro"}] if detail_failure else []
            )
        if path.endswith("/pages/intro"):
            return httpx.Response(404, json={})
        if path.endswith("/tabs"):
            return httpx.Response(tab_status, json=state["tabs"])
        if path.endswith("/modules"):
            return httpx.Response(200, json=[{"id": 1}] if modules is not None else [])
        if path.endswith("/modules/1/items"):
            return httpx.Response(403 if module_failure else 200, json=modules or [])
        if path.endswith("/assignments"):
            return httpx.Response(
                401 if assignment_failure else 200,
                json=[
                    {"id": 5, "name": "HW 1", "description": "HW", "due_at": "2026-10-09T05:00:00Z"}
                ],
            )
        if path == "/api/v1/courses/7":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(200, json=[])

    adapter = CanvasAdapter(
        "https://canvas.example",
        canvas_course_id="7",
        client=CanvasClient(
            "https://canvas.example", "fixture", transport=httpx.MockTransport(handler)
        ),
    )
    return adapter, state, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("tabs", [[], [{"id": "pages", "hidden": True}]])
async def test_confirmed_unavailable_is_explicit_not_empty_inventory(tabs):
    adapter, _, _ = adapter_fixture(tabs=tabs)
    items = await adapter.fetch_items(Course(external_id="7"))
    assert adapter.errors == {}
    assert adapter.unavailable_resources[0]["code"] == "unavailable"
    assert adapter.unavailable_resources[0]["http_status"] == 404
    assert adapter.unavailable_resources[0]["reason"] == (
        "navigation_hidden" if tabs else "not_in_navigation"
    )
    assert "page" not in adapter.successful_item_types
    assert "file" not in adapter.successful_item_types
    assert (
        next(item for item in items if item.item_type == "assignment").structured["due_at"]
        == "2026-10-09T05:00:00Z"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"tabs": [{"id": "pages", "hidden": False}]},
        {"tabs": [{"id": "pages"}]},
        {"tabs": [{"id": "pages", "hidden": "true"}]},
        {"tabs": [{"unexpected": True}]},
        {"tab_status": 403},
        {"modules": [{"type": "Page", "page_url": "intro"}]},
        {"modules": [{}]},
        {"modules": [], "module_failure": True},
        {"status": 401},
        {"status": 403},
        {"assignment_failure": True},
        {"status": 200, "detail_failure": True},
    ],
)
async def test_real_or_ambiguous_failures_are_not_suppressed(options):
    adapter, _, _ = adapter_fixture(**options)
    await adapter.fetch_items(Course(external_id="7"))
    assert "page" in adapter.errors
    assert adapter.unavailable_resources == []


@pytest.mark.asyncio
async def test_resource_enablement_clears_old_unavailability():
    adapter, state, calls = adapter_fixture()
    await adapter.fetch_items(Course(external_id="7"))
    assert adapter.unavailable_resources
    state["status"] = 200
    calls.clear()
    await adapter.fetch_items(Course(external_id="7"))
    assert adapter.unavailable_resources == []
    assert "page" in adapter.successful_item_types
    assert "file" in adapter.successful_item_types
    assert not any(path.endswith("/tabs") for path in calls)


@pytest.mark.asyncio
async def test_sync_persists_availability_and_never_deletes_existing_pages_files(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="canvas", external_id="7", course_code="TEST", name="Test")
        db.add(course)
        db.flush()
        source = CourseSource(
            course_id=course.id,
            name="canvas:7",
            source_type="canvas",
            external_id="7",
            config_json={"baseline_complete": True},
        )
        db.add(source)
        db.flush()
        old_items = [
            SourceItem(
                course_id=course.id,
                course_source_id=source.id,
                source_type="canvas",
                source_name="canvas:7",
                external_id=f"{kind}:old",
                item_type=kind,
                title="Fixture",
                current_hash="old",
            )
            for kind in ("page", "file")
        ]
        db.add_all(old_items)
        db.flush()
        adapter, _, _ = adapter_fixture()
        adapter.course_source_id = source.id
        service = SyncService(
            Settings(
                database_url="sqlite:///:memory:",
                scheduler_enabled=False,
                download_root=tmp_path,
                courses_config=tmp_path / "courses.yaml",
                file_rules_config=tmp_path / "rules.yaml",
                availability_config=tmp_path / "availability.yaml",
            )
        )
        state = await service.run(db, [(course, adapter)])
        assert state.state == "healthy" and state.errors == []
        assert source.state == "HEALTHY" and source.last_error is None
        assert all(not item.is_deleted for item in old_items)
        assert source.metadata_json["unavailable_resources"][0]["resource"] == "page"
        task = db.scalar(select(Task))
        assert task is not None and task.due_at.isoformat().startswith("2026-10-09T05:00:00")
        view = source_health_view(source)
        assert view["health_diagnostics"][0]["code"] == "unavailable"
        assert "existing pages and files are preserved" in view["health_summary"]


def test_unavailable_diagnostics_are_not_mutated_and_do_not_hide_auth_failure():
    unavailable = [
        {
            "resource": "page",
            "code": "unavailable",
            "http_status": 404,
            "reason": "not_in_navigation",
        }
    ]
    errors = [{"resource": "assignment", "code": "invalid_token", "http_status": 401}]
    source = SimpleNamespace(
        metadata_json={"resource_errors": errors, "unavailable_resources": unavailable},
        state="AUTH_REQUIRED",
        last_error=None,
    )
    for _ in range(2):
        view = source_health_view(source)
        assert len(view["health_diagnostics"]) == 2
        assert view["health_summary"].startswith("Sign in again")
    assert len(errors) == 1
