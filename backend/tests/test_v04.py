from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.routes import course_terms, courses, tasks
from app.auth.models import CanvasCredential
from app.auth.store import CredentialStore
from app.config import Settings
from app.db import Base, Course, CourseSource, CredentialProfile, Notification, Task
from app.schemas import DiscoveredCourse
from app.services.auth import AuthService, CanvasCredentialReplacementError
from app.services.canvas_lifecycle import (
    ExpirationSource,
    ExpirationState,
    expiration_status,
    new_pat_metadata,
)
from app.services.course_terms import normalize_course_display, normalize_term
from app.services.sync import SyncService
from app.sources.canvas import CanvasAdapter


@contextmanager
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def settings(tmp_path: Path, **values) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        download_root=tmp_path / "downloads",
        courses_config=tmp_path / "courses.yaml",
        file_rules_config=tmp_path / "file_rules.yaml",
        availability_config=tmp_path / "availability.yaml",
        scheduler_enabled=False,
        canvas_access_token=SecretStr(""),
        **values,
    )


def test_pat_expiration_metadata_and_thresholds() -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    fallback = new_pat_metadata({}, now=now)
    assert fallback["expiration_source"] == ExpirationSource.UNKNOWN
    assert expiration_status(fallback["expires_at"], now).days_remaining is None

    exact = new_pat_metadata({}, now=now, expiration_date=date(2026, 9, 3))
    assert exact["expiration_source"] == ExpirationSource.USER_ENTERED
    assert expiration_status(now + timedelta(days=7), now).state is ExpirationState.WARNING
    assert expiration_status(now + timedelta(days=3), now).state is ExpirationState.URGENT
    assert expiration_status(now + timedelta(days=1), now).state is ExpirationState.CRITICAL
    assert expiration_status(now, now).state is ExpirationState.EXPIRED


def test_expiration_notifications_are_deduplicated(tmp_path: Path) -> None:
    store = CredentialStore()
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            state="ACTIVE",
            metadata_json=new_pat_metadata(
                {}, exact_expires_at=datetime.now(UTC) + timedelta(hours=20)
            ),
        )
        db.add(profile)
        db.commit()
        service = AuthService(settings(tmp_path), store=store)
        service.ensure_profiles(db)
        service.ensure_profiles(db)
        assert db.scalar(select(func.count()).select_from(Notification)) == 1


@pytest.mark.asyncio
async def test_pat_rotation_is_atomic_and_rejects_other_account(
    tmp_path: Path, monkeypatch
) -> None:
    class OtherAccountClient:
        def __init__(self, base_url: str, token: str, concurrency: int):
            del base_url, token, concurrency

        async def probe(self):
            return {"id": 99, "name": "Other Student"}

    monkeypatch.setattr("app.services.auth.CanvasClient", OtherAccountClient)
    store = CredentialStore()
    store.set_canvas("canvas_uiuc", "https://canvas.example", "current-token")
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            state="ACTIVE",
            metadata_json={"auth_mode": "pat", "account": {"id": "42"}},
        )
        db.add(profile)
        db.commit()
        with pytest.raises(CanvasCredentialReplacementError) as captured:
            await AuthService(settings(tmp_path), store=store).set_canvas_and_verify(
                db, profile, "https://canvas.example", "candidate-token"
            )
        assert captured.value.code == "account_mismatch"
        assert store.get_canvas("canvas_uiuc").token == "current-token"
        assert profile.state == "ACTIVE"


def test_term_and_course_display_normalization() -> None:
    term = normalize_term("2026-Fall", term_id=17)
    display = normalize_course_display(
        "esl_111_120248_241230",
        "ESL 111 - Introduction to Academic Writing - Section A - Fall 2026",
        term.display_name,
    )
    assert term.display_name == "Fall 2026"
    assert term.sort_key == "2026-4"
    assert display.course_code == "ESL 111"
    assert display.name == "Introduction to Academic Writing"
    assert display.section == "Section A"


def test_archived_courses_are_excluded_from_active_lists_and_tasks() -> None:
    with memory_db() as db:
        active = Course(
            source="canvas",
            external_id="canvas:1",
            course_code="PHYS225",
            name="Physics",
            lifecycle_state="ACTIVE",
        )
        archived = Course(
            source="canvas",
            external_id="canvas:2",
            course_code="OLD101",
            name="Old course",
            lifecycle_state="ARCHIVED",
            active=False,
        )
        db.add_all([active, archived])
        db.flush()
        db.add_all(
            [
                Task(course_id=active.id, title="Current", source_key="current"),
                Task(course_id=archived.id, title="Archived", source_key="archived"),
            ]
        )
        db.commit()
        assert [row.id for row in courses(db=db)] == [active.id]
        assert [row.id for row in courses(archived=True, db=db)] == [archived.id]
        assert [row.title for row in tasks(db=db)] == ["Current"]


@pytest.mark.asyncio
async def test_canvas_rediscovery_updates_but_does_not_restore_archived_course(
    tmp_path: Path, monkeypatch
) -> None:
    async def discovered(_adapter):
        return [
            DiscoveredCourse(
                source="canvas",
                external_id="123",
                course_code="CS 101",
                name="CS 101 - Computing - Section A",
                term="Fall 2026",
                term_id="fall-2026",
                metadata={"id": 123},
            )
        ]

    monkeypatch.setattr(CanvasAdapter, "discover_courses", discovered)
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:123",
            course_code="CS101",
            name="Old name",
            lifecycle_state="ARCHIVED",
            active=False,
        )
        db.add(course)
        db.flush()
        db.add(
            CourseSource(
                course_id=course.id,
                name="canvas:123",
                source_type="canvas",
                external_id="123",
                enabled=True,
            )
        )
        db.commit()
        pairs = await SyncService(settings(tmp_path)).discover_canvas(
            db, CanvasCredential("https://canvas.example", "token")
        )
        assert pairs == []
        assert course.lifecycle_state == "ARCHIVED"
        assert course.display_name == "Computing"
        assert course.term_id == "fall-2026"


def test_terms_endpoint_groups_active_and_archived() -> None:
    with memory_db() as db:
        db.add_all(
            [
                Course(
                    source="canvas",
                    external_id="1",
                    course_code="A1",
                    name="Active",
                    term="Fall 2026",
                    lifecycle_state="ACTIVE",
                ),
                Course(
                    source="canvas",
                    external_id="2",
                    course_code="A2",
                    name="Archived",
                    term="Fall 2026",
                    lifecycle_state="ARCHIVED",
                    active=False,
                ),
            ]
        )
        db.commit()
        result = course_terms(db=db)
        assert result[0]["display_name"] == "Fall 2026"
        assert result[0]["active_course_count"] == 1
        assert result[0]["archived_course_count"] == 1
