from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes import (
    change_detail,
    changes,
    classify_downloaded_file,
    file,
    files,
    read_all_changes,
    read_change,
    unread_change,
    unread_change_count,
)
from app.auth.store import CredentialStore
from app.config import Settings
from app.db import (
    Base,
    ChangeEvent,
    Course,
    CourseSource,
    CredentialProfile,
    Notification,
    SourceItem,
    SourceSnapshot,
)
from app.schemas import FileClassificationIn, RawSourceItem
from app.services.auth import AuthService
from app.services.downloader import FileDownloader
from app.services.sync import SyncService


@contextmanager
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        download_root=tmp_path / "downloads",
        courses_config=tmp_path / "courses.yaml",
        file_rules_config=tmp_path / "file_rules.yaml",
        availability_config=tmp_path / "availability.yaml",
        scheduler_enabled=False,
    )


def course_item(db: Session) -> tuple[Course, SourceItem]:
    course = Course(
        source="canvas",
        external_id="canvas:1",
        course_code="CS101",
        name="Computing",
        lifecycle_state="ACTIVE",
    )
    db.add(course)
    db.flush()
    item = SourceItem(
        course_id=course.id,
        source_type="canvas",
        source_name="Canvas",
        external_id="assignment:1",
        item_type="assignment",
        title="Homework 1",
        url="https://canvas.example/assignments/1",
        current_hash="initial",
    )
    db.add(item)
    db.flush()
    return course, item


def test_change_read_state_and_semantics() -> None:
    with memory_db() as db:
        _, item = course_item(db)
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="DEADLINE_CHANGED",
            summary="Deadline changed",
        )
        db.add(event)
        db.commit()

        assert unread_change_count(db=db) == {"count": 1}
        listed = changes(limit=100, course_id=None, db=db)
        assert listed[0]["category"] == "deadline"
        assert listed[0]["change_field"] == "due_at"
        assert listed[0]["read_at"] is None

        marked = read_change(event.id, db=db)
        assert marked["read_at"] is not None
        assert unread_change_count(db=db) == {"count": 0}

        unread_change(event.id, db=db)
        assert unread_change_count(db=db) == {"count": 1}
        detail = change_detail(event.id, db=db)
        assert detail["read_at"] is not None
        assert detail["category"] == "deadline"

        unread_change(event.id, db=db)
        assert read_all_changes(db=db) == {"updated": 1}
        assert unread_change_count(db=db) == {"count": 0}


def test_permission_denied_degrades_source_not_verified_credential(tmp_path: Path) -> None:
    store = CredentialStore()
    store.set_canvas("canvas_uiuc", "https://canvas.example", "memory-only-token")
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={"auth_mode": "pat", "auth_source": "memory"},
            state="ACTIVE",
        )
        db.add(profile)
        db.commit()

        service = AuthService(settings(tmp_path), store=store)
        service.mark_runtime_failure(db, "canvas_uiuc", "permission_denied")

        assert profile.state == "ACTIVE"
        assert profile.last_error_code == "permission_denied"
        assert store.get_canvas("canvas_uiuc") is not None
        assert db.scalar(select(Notification)) is None


def test_restart_clears_stale_failed_canvas_state(tmp_path: Path) -> None:
    store = CredentialStore()
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={"auth_mode": "pat", "auth_source": "memory"},
            state="FAILED",
            last_error_code="permission_denied",
        )
        db.add(profile)
        db.commit()

        AuthService(settings(tmp_path), store=store).reset_after_restart(db)

        assert profile.state == "AUTH_REQUIRED"
        assert profile.metadata_json["auth_source"] == "not_loaded"


def test_auth_failure_pending_item_preserves_last_good_snapshot(tmp_path: Path) -> None:
    with memory_db() as db:
        course, item = course_item(db)
        item.current_hash = "last-good-hash"
        snapshot = SourceSnapshot(
            source_item_id=item.id,
            content_hash="last-good-hash",
            structured_json={"title": "Protected content"},
            normalized_text="Complete protected content",
        )
        db.add(snapshot)
        db.commit()
        pending = RawSourceItem(
            source_type=item.source_type,
            source_name=item.source_name,
            external_id=item.external_id,
            item_type=item.item_type,
            title=item.title,
            url=item.url,
            structured={
                "fetch_status": "auth_required",
                "auth_scheme": "ntlm",
                "error": "credential_not_loaded",
            },
            normalized_text=item.title,
            fetch_status="auth_required",
            credential_id="uiuc_netid",
        )

        linked = SyncService(settings(tmp_path))._pending_source_item(
            db, course, pending
        )

        assert linked.id == item.id
        assert linked.current_hash == "last-good-hash"
        assert db.query(SourceSnapshot).filter_by(source_item_id=item.id).count() == 1
        assert db.scalar(select(ChangeEvent)) is None


def test_canvas_status_is_normalized_for_header_and_sources(tmp_path: Path) -> None:
    store = CredentialStore()
    store.set_canvas("canvas_uiuc", "https://canvas.example", "memory-only-token")
    with memory_db() as db:
        course, _ = course_item(db)
        db.add_all(
            [
                CredentialProfile(
                    credential_id="canvas_uiuc",
                    auth_type="canvas_token",
                    display_name="Canvas",
                    username="Student",
                    probe_url="https://canvas.example/api/v1/users/self/profile",
                    metadata_json={
                        "auth_mode": "pat",
                        "auth_source": "memory",
                        "account": {"id": "7", "name": "Student"},
                    },
                    state="ACTIVE",
                ),
                CourseSource(
                    course_id=course.id,
                    name="Canvas",
                    source_type="canvas",
                    enabled=True,
                    state="DEGRADED",
                ),
            ]
        )
        db.commit()

        status = AuthService(settings(tmp_path), store=store).canvas_status(db)
        assert status["verified"] is True
        assert status["credential_state"] == "ACTIVE"
        assert status["source_health"] == "DEGRADED"
        assert status["account_display_name"] == "Student"


@pytest.mark.asyncio
async def test_pat_success_clears_stale_failed_and_exposes_expiry(
    tmp_path: Path, monkeypatch
) -> None:
    class SuccessfulClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def probe(self):
            return {"id": 7, "name": "Student", "login_id": "student"}

    monkeypatch.setattr("app.services.auth.CanvasClient", SuccessfulClient)
    store = CredentialStore()
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={"auth_mode": "pat"},
            state="FAILED",
            last_error_code="permission_denied",
        )
        db.add(profile)
        db.commit()
        service = AuthService(settings(tmp_path), store=store)

        assert await service.set_canvas_and_verify(
            db, profile, "https://canvas.example", "replacement-token"
        )
        status = service.canvas_status(db)

        assert profile.state == "ACTIVE"
        assert profile.last_error_code is None
        assert status["verified"] is True
        assert status["days_remaining"] is None
        assert status["expiration_source"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_browser_session_success_clears_stale_failed(
    tmp_path: Path, monkeypatch
) -> None:
    class SuccessfulClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def probe(self):
            return {"id": 7, "name": "Student", "login_id": "student"}

    monkeypatch.setattr("app.services.auth.CanvasClient", SuccessfulClient)
    store = CredentialStore()
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={"auth_mode": "browser_session"},
            state="FAILED",
        )
        db.add(profile)
        db.commit()
        service = AuthService(settings(tmp_path), store=store)

        assert await service.set_canvas_session_and_verify(
            db,
            profile,
            "https://canvas.example",
            {"canvas_session": "session-secret"},
        )
        status = service.canvas_status(db)

        assert status["credential_state"] == "ACTIVE"
        assert status["verified"] is True
        assert status["auth_mode"] == "browser_session"
        assert status["expires_at"] is None
        assert status["days_remaining"] is None


def test_file_classification_override_survives_new_versions(tmp_path: Path) -> None:
    with memory_db() as db:
        course, item = course_item(db)
        downloader = FileDownloader(tmp_path / "downloads")
        first = downloader.save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url="https://canvas.example/files/lecture-01.txt",
            filename="lecture-01.txt",
            content=b"version one",
            mime_type="text/plain",
        ).record
        assert first is not None
        assert first.source_detected_type == "lecture"

        changed = classify_downloaded_file(
            first.id, FileClassificationIn(type="reading"), db=db
        )
        assert changed["effective_type"] == "reading"
        assert file(first.id, db=db)["effective_type"] == "reading"

        second = downloader.save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url="https://canvas.example/files/lecture-01.txt",
            filename="lecture-01.txt",
            content=b"version two",
            mime_type="text/plain",
        ).record
        assert second is not None
        assert second.source_detected_type == "lecture"
        assert second.user_override_type == "reading"
        assert second.category == "reading"
        output = files(
            course_id=None, type="reading", latest_only=True, limit=200, db=db
        )
        assert [row["id"] for row in output] == [second.id]


def test_file_classification_rejects_unknown_types() -> None:
    with pytest.raises(ValidationError):
        FileClassificationIn(type="slides")
