from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.provider import OpenAICompatibleProvider, provider_from_settings
from app.api.routes import create_calendar_event
from app.config import Settings
from app.db import (
    AppSetting,
    Base,
    CalendarEvent,
    Course,
    CourseSource,
    SourceConnection,
    StudyAvailabilityOverride,
    StudyAvailabilityRule,
    Task,
    TaskProgress,
    TaskWorkSession,
)
from app.schemas import CalendarEventIn
from app.services.ai_settings import (
    ai_probe_secrets,
    get_ai_probe_settings,
    list_probe_models,
    probe_selected_model,
    set_ai_probe_settings,
)
from app.services.calendar_capacity import expand_event, free_capacity_between
from app.services.source_connections import (
    create_canvas_connection,
    create_website_connection,
    ensure_existing_connections,
)
from app.services.today import TodayEngine, round_remaining_minutes
from app.services.work_tracking import (
    active_session,
    set_effective_used_minutes,
    start_task_work,
    stop_task_work,
    task_work_summary,
)


@contextmanager
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def add_course(db: Session, code: str = "TEST101") -> Course:
    value = Course(
        source="manual",
        external_id=f"manual:{code}",
        course_code=code,
        name=code,
        lifecycle_state="ACTIVE",
        active=True,
    )
    db.add(value)
    db.flush()
    return value


def add_task(db: Session, course: Course, title: str, due: datetime | None = None) -> Task:
    value = Task(
        course_id=course.id,
        title=title,
        task_type="homework",
        status="NOT_STARTED",
        due_at=due,
    )
    db.add(value)
    db.flush()
    return value


def test_one_active_work_session_switches_atomically_and_survives_queries() -> None:
    now = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    with memory_db() as db:
        course = add_course(db)
        first = add_task(db, course, "First")
        second = add_task(db, course, "Second")
        db.commit()

        first_summary, switched = start_task_work(db, first.id, now)
        assert switched is None
        assert first_summary["active_session_id"] is not None
        same_summary, switched = start_task_work(db, first.id, now + timedelta(minutes=5))
        assert switched is None
        assert same_summary["active_session_id"] == first_summary["active_session_id"]
        assert db.scalar(select(func.count()).select_from(TaskWorkSession)) == 1

        second_summary, switched = start_task_work(db, second.id, now + timedelta(minutes=30))
        assert switched == first.id
        assert second_summary["active_session_id"] is not None
        assert active_session(db).task_id == second.id
        first_history = task_work_summary(db, first.id, now + timedelta(minutes=30))
        assert first_history["tracked_minutes"] == 30
        assert first_history["sessions"][0].duration_seconds == 1800

        stopped = stop_task_work(db, second.id, now + timedelta(minutes=45))
        assert stopped["tracked_minutes"] == 15
        assert active_session(db) is None


def test_database_rejects_two_open_work_sessions() -> None:
    now = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    with memory_db() as db:
        course = add_course(db)
        first = add_task(db, course, "First")
        second = add_task(db, course, "Second")
        db.flush()
        db.add_all(
            [
                TaskWorkSession(task_id=first.id, started_at=now, source="timer"),
                TaskWorkSession(task_id=second.id, started_at=now, source="timer"),
            ]
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_manual_used_time_correction_preserves_work_history() -> None:
    now = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    with memory_db() as db:
        task = add_task(db, add_course(db), "Correction")
        db.add(
            TaskWorkSession(
                task_id=task.id,
                started_at=now,
                ended_at=now + timedelta(minutes=60),
                duration_seconds=3600,
                source="timer",
            )
        )
        db.commit()
        result = set_effective_used_minutes(db, task.id, 90, now + timedelta(hours=1))
        assert result["tracked_minutes"] == 60
        assert result["manual_adjustment_minutes"] == 30
        assert result["effective_used_minutes"] == 90
        session = db.scalar(select(TaskWorkSession))
        assert session.duration_seconds == 3600
        result = set_effective_used_minutes(db, task.id, 0, now + timedelta(hours=1))
        assert result["manual_adjustment_minutes"] == -60
        assert result["effective_used_minutes"] == 0


@pytest.mark.parametrize(
    ("used", "progress", "expected"),
    [(60, 25, 180), (75, 50, 75)],
)
def test_observed_remaining_time_precedes_fallback(used: int, progress: int, expected: int) -> None:
    now = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    with memory_db() as db:
        course = add_course(db)
        task = add_task(db, course, "Observed", now + timedelta(hours=1))
        task.manual_time_adjustment_minutes = used
        db.add(TaskProgress(task_id=task.id, status="IN_PROGRESS", progress_percent=progress))
        db.commit()
        row = TodayEngine().build(db, now)["work"][0]
        assert row["remaining_minutes"] == expected
        assert row["estimate_source"] == "observed_ratio"


def test_remaining_time_edges_and_five_minute_rounding() -> None:
    assert round_remaining_minutes(62) == 60
    assert round_remaining_minutes(63) == 65
    assert round_remaining_minutes(3) == 5
    assert round_remaining_minutes(500, complete=True) == 0
    now = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    with memory_db() as db:
        course = add_course(db)
        zero = add_task(db, course, "Zero", now + timedelta(hours=1))
        done = add_task(db, course, "Done", now + timedelta(hours=1))
        db.add_all(
            [
                TaskProgress(task_id=zero.id, status="NOT_STARTED", progress_percent=0),
                TaskProgress(task_id=done.id, status="READY_TO_SUBMIT", progress_percent=100),
            ]
        )
        db.commit()
        rows = {row["title"]: row for row in TodayEngine().build(db, now)["work"]}
        assert rows["Zero"]["estimate_source"] == "fallback"
        assert rows["Done"]["remaining_minutes"] == 0


def test_calendar_capacity_applies_commute_and_short_class_gap() -> None:
    monday = date(2026, 9, 7)
    start = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)  # 08:00 Chicago
    end = datetime(2026, 9, 7, 23, 0, tzinfo=UTC)  # 18:00 Chicago
    with memory_db() as db:
        course = add_course(db)
        course.commute_minutes = 15
        db.add(StudyAvailabilityRule(weekday=0, start_local_time="08:00", end_local_time="18:00"))
        db.add(
            CalendarEvent(
                summary="Class",
                start_at=datetime(2026, 9, 7, 15, 0, tzinfo=UTC),
                end_at=datetime(2026, 9, 7, 16, 0, tzinfo=UTC),
                event_type="class",
                course_id=course.id,
            )
        )
        db.commit()
        minutes, source = free_capacity_between(db, start, end)
        assert (minutes, source) == (510, "calendar")

        db.execute(select(CalendarEvent))
        for row in db.scalars(select(CalendarEvent)):
            db.delete(row)
        course.commute_minutes = 0
        db.add_all(
            [
                CalendarEvent(
                    summary="A",
                    start_at=datetime(2026, 9, 7, 15, 0, tzinfo=UTC),
                    end_at=datetime(2026, 9, 7, 15, 50, tzinfo=UTC),
                    event_type="class",
                    course_id=course.id,
                ),
                CalendarEvent(
                    summary="B",
                    start_at=datetime(2026, 9, 7, 16, 10, tzinfo=UTC),
                    end_at=datetime(2026, 9, 7, 17, 0, tzinfo=UTC),
                    event_type="class",
                    course_id=course.id,
                ),
            ]
        )
        db.commit()
        minutes, _ = free_capacity_between(db, start, end)
        assert minutes == 480
        assert monday.weekday() == 0


def test_calendar_api_normalizes_offset_datetimes_before_sqlite_persistence() -> None:
    with memory_db() as db:
        course = add_course(db)
        course.commute_minutes = 15
        db.commit()
        event = create_calendar_event(
            CalendarEventIn(
                summary="Offset class",
                start_at=datetime.fromisoformat("2026-09-02T12:00:00-05:00"),
                end_at=datetime.fromisoformat("2026-09-02T13:00:00-05:00"),
                timezone="America/Chicago",
                event_type="class",
                course_id=course.id,
            ),
            db,
        )
        assert event.start_at.hour == 17
        assert event.end_at.hour == 18
        db.add(
            StudyAvailabilityRule(
                weekday=2,
                start_local_time="08:00",
                end_local_time="22:00",
            )
        )
        db.commit()
        assert free_capacity_between(
            db,
            datetime.fromisoformat("2026-09-02T08:00:00-05:00"),
            datetime.fromisoformat("2026-09-02T22:00:00-05:00"),
        ) == (750, "calendar")


def test_availability_override_replaces_weekday_and_can_block_day() -> None:
    start = datetime(2026, 9, 7, 13, 0, tzinfo=UTC)
    end = datetime(2026, 9, 7, 23, 0, tzinfo=UTC)
    with memory_db() as db:
        db.add(StudyAvailabilityRule(weekday=0, start_local_time="08:00", end_local_time="18:00"))
        db.add(StudyAvailabilityOverride(date=date(2026, 9, 7), available_intervals=[]))
        db.commit()
        assert free_capacity_between(db, start, end) == (0, "availability")


def test_weekly_recurrence_keeps_chicago_wall_time_across_dst() -> None:
    with memory_db() as db:
        event = CalendarEvent(
            summary="Monday class",
            start_at=datetime(2026, 10, 26, 14, 0, tzinfo=UTC),  # 09:00 CDT
            end_at=datetime(2026, 10, 26, 15, 0, tzinfo=UTC),
            timezone="America/Chicago",
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20261110",
            event_type="class",
        )
        db.add(event)
        db.flush()
        rows = expand_event(
            event,
            datetime(2026, 10, 25, tzinfo=UTC),
            datetime(2026, 11, 10, tzinfo=UTC),
        )
        assert [row.start.hour for row in rows] == [14, 15, 15]


def test_source_connections_are_empty_fresh_then_create_without_duplicates(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        courses_config=tmp_path / "missing.yaml",
        scheduler_enabled=False,
    )
    with memory_db() as db:
        assert ensure_existing_connections(db, settings) == 0
        assert list(db.scalars(select(SourceConnection))) == []
        canvas = create_canvas_connection(db, "https://canvas.example.edu")
        assert create_canvas_connection(db, "https://canvas.example.edu").id == canvas.id
        website = create_website_connection(
            db,
            {
                "course_name": "Independent Physics",
                "course_code": "PHYS 999",
                "term": "Fall 2026",
                "base_url": "https://physics.example.edu/course/",
                "authentication_method": "ntlm",
                "protected_path_prefix": "/course/secure/",
                "probe_url": "https://physics.example.edu/course/secure/probe",
            },
        )
        assert website.course_id is not None
        assert website.credential_id
        assert db.scalar(select(func.count()).select_from(CourseSource)) == 1
        assert create_website_connection(db, website.config_json | {
            "course_name": "Independent Physics",
            "base_url": "https://physics.example.edu/course",
            "authentication_method": "ntlm",
        }).id == website.id


def test_upgrade_bootstrap_preserves_existing_source_ids(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        courses_config=tmp_path / "missing.yaml",
        scheduler_enabled=False,
    )
    with memory_db() as db:
        course = add_course(db)
        source = CourseSource(
            course_id=course.id,
            name="Existing Website",
            source_type="website",
            url="https://existing.example.edu",
            enabled=True,
            state="HEALTHY",
        )
        db.add(source)
        db.commit()
        assert ensure_existing_connections(db, settings) == 1
        connection = db.scalar(select(SourceConnection))
        assert connection.config_json["course_source_id"] == source.id
        assert ensure_existing_connections(db, settings) == 0


@pytest.mark.asyncio
async def test_ai_probe_lists_and_tests_models_without_persisting_secret(monkeypatch) -> None:
    ai_probe_secrets.clear()
    with memory_db() as db:
        settings = set_ai_probe_settings(db, "https://api.deepseek.com/v1", "model-a")
        ai_probe_secrets.set("super-secret", "https://api.deepseek.com/v1")

        async def models(_self):
            return ["model-a", "model-b"]

        async def probe(_self, model=None):
            return {"success": True, "latency_ms": 5, "model_id": model or "model-a"}

        monkeypatch.setattr(OpenAICompatibleProvider, "list_models", models)
        monkeypatch.setattr(OpenAICompatibleProvider, "probe", probe)
        assert (await list_probe_models(db))["models"] == ["model-a", "model-b"]
        set_ai_probe_settings(db, "https://api.deepseek.com/v1", "model-b")
        assert (await probe_selected_model(db, "model-b"))["success"] is True
        stored = db.get(AppSetting, "ai_probe").value_json
        assert "super-secret" not in repr(stored)
        assert settings["connected_to_live_ai"] is False
        live = provider_from_settings(Settings(llm_provider="disabled"))
        assert live.__class__.__name__ == "DisabledProvider"
        ai_probe_secrets.clear()
        assert get_ai_probe_settings(db)["credential_loaded"] is False


@pytest.mark.asyncio
async def test_ai_probe_sanitizes_auth_failure(monkeypatch) -> None:
    ai_probe_secrets.clear()
    with memory_db() as db:
        set_ai_probe_settings(db, "https://api.deepseek.com/v1", "bad-model")
        ai_probe_secrets.set("secret", "https://api.deepseek.com/v1")

        async def rejected(_self):
            request = httpx.Request("GET", "https://ai.example.test/v1/models")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("secret raw body", request=request, response=response)

        monkeypatch.setattr(OpenAICompatibleProvider, "list_models", rejected)
        result = await list_probe_models(db)
        assert result == {
            "success": False,
            "models": [],
            "message": "The provider rejected the credential. Check its provider and region.",
        }
        assert "secret" not in repr(result)
        ai_probe_secrets.clear()
