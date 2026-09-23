from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.auth.store import CredentialStore
from app.config import Settings, get_settings
from app.db import (
    Base,
    ChangeEvent,
    Course,
    CredentialProfile,
    DownloadedFile,
    SourceConnection,
    SourceItem,
    Task,
    get_db,
)
from app.services.auth import AuthService
from app.sources.canvas_client import CanvasClient


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(canvas_base_url="https://canvas.example", courses_config=tmp_path / "absent",
                            canvas_access_token="environment-fixture", scheduler_enabled=False)
        store = CredentialStore()
        service = AuthService(settings, store=store)
        profile = CredentialProfile(credential_id="canvas_uiuc", auth_type="canvas_token",
            display_name="Canvas", probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={"base_url": "https://canvas.example", "account": {"id": "42"}}, state="ACTIVE")
        db.add(profile)
        db.commit()
        app = FastAPI()
        app.include_router(routes.router)
        def database():
            yield db
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_settings] = lambda: settings
        monkeypatch.setattr(routes, "_auth_profile_or_404", lambda *args: (service, profile))
        yield db, settings, store, service, profile, app


@pytest.mark.asyncio
async def test_repeated_secondary_calendar_and_changes_mutations_are_idempotent(context):
    db, settings, store, service, profile, app = context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = {"date": "2026-10-01", "available_intervals": [{"start": "10:00", "end": "11:00"}]}
        for _ in range(2):
            saved = await client.put("/api/calendar/availability/overrides/2026-10-01", json=payload)
            assert saved.status_code == 200
            assert (await client.post("/api/changes/read-all")).status_code == 200
        rows = (await client.get("/api/calendar/availability/overrides")).json()
        assert len([row for row in rows if row["date"] == "2026-10-01"]) == 1


@pytest.mark.asyncio
async def test_ignore_unignore_http_round_trip(context):
    db, settings, store, service, profile, app = context
    course = Course(source="canvas", external_id="ignore-contract", course_code="TEST", name="Test")
    db.add(course)
    db.flush()
    task = Task(course_id=course.id, title="Ignore contract fixture")
    db.add(task)
    db.commit()
    task_id = task.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ignored = await client.post(f"/api/tasks/{task_id}/ignore")
        assert ignored.status_code == 200
        assert ignored.json()["ignored_at"] is not None
        active = await client.get("/api/tasks?active_only=true")
        assert active.status_code == 200
        assert task_id not in [item["id"] for item in active.json()]
        restored = await client.post(f"/api/tasks/{task_id}/unignore")
        assert restored.status_code == 200
        assert restored.json()["ignored_at"] is None
        active = await client.get("/api/tasks?active_only=true")
        assert task_id in [item["id"] for item in active.json()]
        for action in ("ignore", "unignore"):
            assert (await client.post(f"/api/tasks/{task_id + 999}/{action}")).status_code == 404


@pytest.mark.asyncio
async def test_canvas_pat_origin_guard_executes_before_probe_and_preserves_slot(context, monkeypatch):
    db, settings, store, service, profile, app = context
    store.set_canvas("canvas_uiuc", settings.canvas_base_url, "old-fixture")
    original = store.get_canvas("canvas_uiuc")
    calls = []
    async def probe(self):
        calls.append(self)
        return {"id": 42, "name": "Fixture"}
    async def sync(self, database):
        return 0, SimpleNamespace(items_seen=0, changes=0, errors=[])
    monkeypatch.setattr(CanvasClient, "probe", probe)
    monkeypatch.setattr(routes.SyncService, "sync_canvas", sync)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for base, code in [("https://evil.example", 400), ("http://canvas.example", 422)]:
            response = await client.post("/api/auth/profiles/canvas_uiuc/canvas-token",
                json={"base_url": base, "token": "candidate-fixture"})
            assert response.status_code == code
            assert store.get_canvas("canvas_uiuc") is original
            assert not calls
        response = await client.post("/api/auth/profiles/canvas_uiuc/canvas-token",
            json={"base_url": settings.canvas_base_url, "token": "candidate-fixture"})
        assert response.status_code == 200
        assert response.json()["verified"] is True
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/calendar/events", {"summary": "invalid", "start_at": "2026-09-14T14:00:00Z",
        "end_at": "2026-09-14T15:00:00Z", "timezone": "Not/AZone"}),
    ("PUT", "/api/calendar/events/999", {"summary": "invalid", "start_at": "2026-09-14T14:00:00Z",
        "end_at": "2026-09-14T15:00:00Z", "timezone": "Not/AZone"}),
    ("PUT", "/api/settings/general", {"academic_timezone": "Not/AZone"}),
])
async def test_unknown_timezone_returns_422(context, method, path, payload):
    *_, app = context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=payload)
    assert response.status_code == 422
    assert "Unknown timezone" in response.text


def test_last_session_removal_keeps_environment_eligible_but_not_falsely_verified(context):
    db, settings, store, service, profile, app = context
    store.set_canvas_session("canvas_uiuc", settings.canvas_base_url, {"session": "fixture"})
    profile.metadata_json = {**profile.metadata_json, "auth_mode": "browser_session"}
    db.commit()
    service.remove_canvas_method(db, profile, "browser_session")
    assert service.canvas_credential().token == "environment-fixture"
    assert service.canvas_status(db)["credential_state"] == "AUTH_REQUIRED"
    service.reset_after_restart(db)
    assert service.canvas_credential().token == "environment-fixture"
    assert service.canvas_status(db)["effective_method"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUBMITTED", "GRADED", "CANCELLED", "READY_TO_SUBMIT", "IN_PROGRESS"])
async def test_status_only_progress_cannot_forge_submission_or_bypass_completion(context, status):
    db, settings, store, service, profile, app = context
    course = Course(source="canvas", external_id="test", name="Test", course_code="TEST")
    db.add(course)
    db.flush()
    task = Task(course_id=course.id, title="Test", submission_state="unsubmitted")
    db.add(task)
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/tasks/{task.id}/progress", json={"status": status})
        assert response.status_code == 422
    db.refresh(task)
    assert task.status == "NOT_STARTED" and not task.local_completed
    assert task.state_revision == 0 and task.submission_state == "unsubmitted"


def test_ai_source_status_uses_effective_method_and_excludes_secrets(context, monkeypatch):
    import app.services.auth as auth_module
    from app.services.ai_chat import SourceArgs, _source
    db, settings, store, service, profile, app = context
    store.set_canvas("canvas_uiuc", settings.canvas_base_url, "private-fixture-pat")
    store.set_canvas_session("canvas_uiuc", settings.canvas_base_url, {"session": "private-fixture-cookie"})
    store.remove_canvas_slot("canvas_uiuc", "pat", state="INVALID")
    row = SourceConnection(source_type="canvas", external_key="test", name="Test",
        base_url=settings.canvas_base_url, config_json={"default_auth_method": "pat"})
    db.add(row)
    db.commit()
    monkeypatch.setattr(auth_module, "AuthService", lambda value: service)
    result = _source(db, SourceArgs(source_id=row.id), settings)
    assert result["authentication_method"] == "browser_session"
    assert result["authentication_status"]["connection_state"] == "ACTIVE"
    assert "private-fixture" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_code", [None, "FALL 2026", "ASTR 405"])
async def test_course_labels_consistent_on_direct_file_and_change_reads(context, cached_code):
    db, *_, app = context
    raw = "Fall 2026-ASTR 405-Planetary Systems"
    course = Course(source="canvas", external_id="label-fixture", course_code=raw,
                    name=raw, term_name="Fall 2026", display_course_code=cached_code)
    db.add(course)
    db.flush()
    item = SourceItem(course_id=course.id, source_type="canvas", source_name="fixture",
                      external_id="fixture-file", item_type="file", title="Lecture", current_hash="a" * 64)
    db.add(item)
    db.flush()
    downloaded = DownloadedFile(course_id=course.id, source_item_id=item.id,
        source_url="https://canvas.example/files/1", original_filename="lecture.pdf",
        local_path="unused-fixture.pdf", size_bytes=1, sha256="a" * 64)
    change = ChangeEvent(source_item_id=item.id, change_type="new_file", summary="New lecture")
    db.add_all([downloaded, change])
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # No prior GET /courses: deep links must not depend on a warmed display cache.
        listing = await client.get(f"/api/files?course_id={course.id}")
        detail = await client.get(f"/api/files/{downloaded.id}")
        classified = await client.patch(f"/api/files/{downloaded.id}/classification", json={"type": "lecture"})
        for response in (listing, detail, classified):
            assert response.status_code == 200, response.text
            row = response.json()[0] if isinstance(response.json(), list) else response.json()
            assert row["course_code"] == "ASTR 405"
            assert row["course_name"] == "Planetary Systems"
        changed = await client.get(f"/api/changes/{change.id}")
        assert changed.status_code == 200, changed.text
        assert changed.json()["primary_text"].startswith("ASTR 405: ")
        assert changed.json()["course"] == {"id": course.id, "course_code": "ASTR 405", "name": "Planetary Systems"}
        courses = await client.get("/api/courses")
        row = next(row for row in courses.json() if row["id"] == course.id)
        assert row["display_course_code"] == "ASTR 405"
        assert row["display_name"] == "Planetary Systems"
    db.refresh(course)
    assert course.course_code == raw and course.name == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_code", [None, "FALL 2026", "ASTR 405"])
async def test_today_and_ai_derive_labels_without_course_list_read(context, cached_code):
    from app.services.ai_chat import _course_row, _task_row
    from app.services.calendar_ai import calendar_context

    db, *_, app = context
    raw = "Fall 2026-ASTR 405-Planetary Systems"
    course = Course(source="canvas", external_id="today-label", course_code=raw, name=raw,
        display_course_code=cached_code, display_name="stale title", term_name="Fall 2026")
    db.add(course)
    db.flush()
    task = Task(course_id=course.id, title="Label regression", due_at=datetime.now(UTC))
    db.add(task)
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/today")
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["work"] if row["task_id"] == task.id)
        assert row["course_code"] == "ASTR 405"
    assert _course_row(course)["course_code"] == "ASTR 405"
    assert _course_row(course)["name"] == "Planetary Systems"
    assert _task_row(db, task)["course"] == "ASTR 405"
    calendar_course = next(row for row in calendar_context(db)["courses"] if row["id"] == course.id)
    assert calendar_course["label"] == "ASTR 405"
    db.refresh(course)
    assert course.display_course_code == cached_code  # Read-only derivation, no cache mutation.
    assert course.course_code == raw and course.name == raw
