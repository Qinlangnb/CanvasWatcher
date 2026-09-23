from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.routes import notifications
from app.config import Settings
from app.db import (
    Base,
    CalendarEvent,
    Course,
    GoogleCalendarConnection,
    GoogleCalendarSelection,
    ICSCalendarImportSource,
    Notification,
    StudyAvailabilityRule,
    Task,
    TaskProgress,
)
from app.schemas import CalendarMutationExecuteIn, CalendarMutationPlan
from app.services.calendar_ai import execute_calendar_mutation
from app.services.calendar_capacity import free_capacity_between
from app.services.change_policy import should_emit_change
from app.services.google_calendar import GOOGLE_SCOPES, GoogleCalendarError, GoogleCalendarService
from app.services.ics_calendar import (
    ICSRemovalConfirmationRequired,
    ICSValidationError,
    import_ics,
    parse_ics,
    preview_ics,
    remove_ics_source,
)
from app.services.planner import Planner
from app.services.source_connections import create_website_connection, update_source_connection
from app.services.today import TodayEngine
from app.services.work_tracking import record_work_heartbeat, start_task_work


@contextmanager
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def add_course(db: Session, code: str = "TEST101") -> Course:
    row = Course(
        source="manual",
        external_id=f"manual:{code}",
        course_code=code,
        name=code,
        active=True,
        lifecycle_state="ACTIVE",
    )
    db.add(row)
    db.flush()
    return row


def google_settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        scheduler_enabled=False,
        google_calendar_client_id="client-id",
        google_calendar_client_secret="client-secret",
        google_calendar_redirect_uri="http://localhost/callback",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )


def test_google_oauth_uses_read_only_offline_scopes_and_encrypts_refresh_token() -> None:
    pages = {"calendar": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "access-secret", "refresh_token": "refresh-secret"})
        if request.url.path.endswith("/calendarList"):
            pages["calendar"] += 1
            if not request.url.params.get("pageToken"):
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": "primary@example.edu", "summary": "Primary", "primary": True, "timeZone": "America/Chicago", "accessRole": "owner"}],
                        "nextPageToken": "page-2",
                    },
                )
            return httpx.Response(
                200,
                json={"items": [{"id": "classes", "summary": "Classes", "timeZone": "America/Chicago", "accessRole": "reader"}]},
            )
        raise AssertionError(request.url)

    settings = google_settings()
    with memory_db() as db:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = GoogleCalendarService(
            settings,
            client=client,
            token_url="https://mock.local/token",
            api_root="https://mock.local/calendar/v3",
        )
        authorization_url = service.begin_oauth(db)
        query = parse_qs(urlparse(authorization_url).query)
        assert query["access_type"] == ["offline"]
        assert set(query["scope"][0].split()) == set(GOOGLE_SCOPES)
        import asyncio

        status = asyncio.run(service.finish_oauth(db, code="code", state=query["state"][0]))
        connection = db.scalar(select(GoogleCalendarConnection))
        assert connection.refresh_token_ciphertext != "refresh-secret"
        assert "refresh" not in repr(status).lower()
        assert len(status["calendars"]) == 2
        assert pages["calendar"] == 2
        asyncio.run(client.aclose())


def test_google_oauth_rejects_state_mismatch() -> None:
    import asyncio

    with memory_db() as db:
        service = GoogleCalendarService(google_settings())
        service.begin_oauth(db)
        with pytest.raises(GoogleCalendarError, match="state"):
            asyncio.run(service.finish_oauth(db, code="x", state="wrong"))


def test_google_initial_incremental_cancel_and_410_recovery() -> None:
    import asyncio

    calls = {"full": 0, "incremental": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "access"})
        if request.url.path.endswith("/events"):
            token = request.url.params.get("syncToken")
            if token == "expired":
                calls["incremental"] += 1
                return httpx.Response(410, json={"error": "gone"})
            if token == "sync-1":
                calls["incremental"] += 1
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": "event-1", "status": "cancelled"}],
                        "nextSyncToken": "sync-2",
                    },
                )
            calls["full"] += 1
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "event-1", "summary": "Class", "start": {"dateTime": "2026-09-03T10:00:00-05:00"}, "end": {"dateTime": "2026-09-03T11:00:00-05:00"}, "recurringEventId": "series", "originalStartTime": {"dateTime": "2026-09-03T10:00:00-05:00"}}],
                    "nextSyncToken": "sync-1",
                },
            )
        raise AssertionError(request.url)

    settings = google_settings()
    with memory_db() as db:
        connection = GoogleCalendarConnection(
            state="ACTIVE",
            refresh_token_ciphertext=__import__("app.services.google_calendar", fromlist=["TokenCipher"]).TokenCipher(settings.google_token_encryption_key.get_secret_value()).encrypt("refresh"),
        )
        db.add(connection)
        db.flush()
        selection = GoogleCalendarSelection(
            connection_id=connection.id,
            calendar_id="classes@example.edu",
            summary="Classes",
            selected=True,
            timezone="America/Chicago",
        )
        db.add(selection)
        db.commit()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = GoogleCalendarService(settings, client=client, token_url="https://mock.local/token", api_root="https://mock.local/calendar/v3")
        asyncio.run(service.sync(db))
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 1
        assert selection.sync_token == "sync-1"
        asyncio.run(service.sync(db))
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 0
        selection.sync_token = "expired"
        db.commit()
        asyncio.run(service.sync(db))
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 1
        assert selection.sync_token == "sync-1"
        assert calls == {"full": 2, "incremental": 2}
        service.disconnect(db)
        assert db.scalar(select(GoogleCalendarConnection)).refresh_token_ciphertext is None
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 0
        asyncio.run(client.aclose())


ICS_RECURRING = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Academic Watcher Test//EN\r
X-WR-CALNAME:UIUC Fall 2026\r
X-WR-TIMEZONE:America/Chicago\r
BEGIN:VEVENT\r
UID:series-1\r
DTSTART;TZID=America/Chicago:20260907T100000\r
DTEND;TZID=America/Chicago:20260907T110000\r
RRULE:FREQ=WEEKLY;COUNT=3\r
EXDATE;TZID=America/Chicago:20260914T100000\r
SUMMARY:Lecture\r
LOCATION:Room 100\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:transparent-1\r
DTSTART;TZID=America/Chicago:20260908T120000\r
DTEND;TZID=America/Chicago:20260908T130000\r
SUMMARY:Office hours\r
TRANSP:TRANSPARENT\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_ics_recognition_recurrence_dedup_and_owned_removal() -> None:
    settings = Settings(database_url="sqlite:///:memory:", scheduler_enabled=False)
    with memory_db() as db:
        parsed = parse_ics(ICS_RECURRING, "fall.ics", settings)
        assert parsed.name == "UIUC Fall 2026"
        assert parsed.timezone == "America/Chicago"
        assert parsed.recurring_series_count == 1
        assert parsed.event_count == 3  # two recurrence instances after EXDATE + transparent event
        result = import_ics(db, ICS_RECURRING, "fall.ics", settings)
        assert result["reconciliation"]["new"] == 3
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 3
        same = import_ics(db, ICS_RECURRING, "fall.ics", settings, source_id=result["source_id"])
        assert same["no_op"] is True
        preview = preview_ics(db, ICS_RECURRING, "fall.ics", settings, source_id=result["source_id"])
        assert preview["reconciliation"] == {
            "new": 0,
            "updated": 0,
            "removed": 0,
            "unchanged": 3,
        }
        source_id = result["source_id"]
        assert remove_ics_source(db, source_id) is True
        assert db.scalar(select(func.count()).select_from(CalendarEvent)) == 0
        assert db.get(ICSCalendarImportSource, source_id) is None


def test_ics_all_day_and_transparent_event_capacity() -> None:
    data = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-TIMEZONE:America/Chicago\r
BEGIN:VEVENT\r
UID:all-day\r
DTSTART;VALUE=DATE:20260908\r
DTEND;VALUE=DATE:20260909\r
SUMMARY:Holiday\r
TRANSP:TRANSPARENT\r
END:VEVENT\r
END:VCALENDAR\r
"""
    settings = Settings(database_url="sqlite:///:memory:", scheduler_enabled=False)
    with memory_db() as db:
        result = import_ics(db, data, "holiday.ics", settings)
        event = db.scalar(select(CalendarEvent))
        assert event.all_day is True
        assert event.start_date.isoformat() == "2026-09-08"
        assert event.read_only is True
        db.add(StudyAvailabilityRule(weekday=1, start_local_time="08:00", end_local_time="18:00"))
        db.commit()
        assert free_capacity_between(
            db,
            datetime.fromisoformat("2026-09-08T08:00:00-05:00"),
            datetime.fromisoformat("2026-09-08T18:00:00-05:00"),
        ) == (600, "calendar")
        assert result["event_count"] == 1


def test_ics_reimport_requires_confirmation_before_removing() -> None:
    settings = Settings(database_url="sqlite:///:memory:", scheduler_enabled=False)
    with memory_db() as db:
        first = import_ics(db, ICS_RECURRING, "fall.ics", settings)
        changed = ICS_RECURRING.replace(b"BEGIN:VEVENT\r\nUID:transparent-1", b"BEGIN:VEVENT\r\nUID:replacement").replace(b"Office hours", b"Advising")
        with pytest.raises(ICSRemovalConfirmationRequired):
            import_ics(db, changed, "fall.ics", settings, source_id=first["source_id"])
        result = import_ics(db, changed, "fall.ics", settings, source_id=first["source_id"], confirm_removals=True)
        assert result["reconciliation"]["removed"] == 1
        assert result["reconciliation"]["new"] == 1


def test_ics_rejects_malformed_and_floating_requires_confirmation() -> None:
    settings = Settings(database_url="sqlite:///:memory:", scheduler_enabled=False)
    with pytest.raises(ICSValidationError):
        parse_ics(b"not a calendar", "bad.ics", settings)
    floating = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260908T100000\r\nDTEND:20260908T110000\r\nSUMMARY:X\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    parsed = parse_ics(floating, "floating.ics", settings)
    assert parsed.timezone_confirmation_required is True
    with memory_db() as db, pytest.raises(ICSValidationError, match="Confirm"):
        import_ics(db, floating, "floating.ics", settings)


def test_change_visibility_and_file_semantics() -> None:
    assert should_emit_change("UPDATED", {"title": "old"}, None) is False
    assert should_emit_change("UPDATED", {"hidden": False}, {"hidden": True}) is False
    assert should_emit_change("NEW_FILE", {"hidden": True}, {"hidden": False}) is True
    assert should_emit_change("FILE_CHANGE", {"sha256": "a"}, {"sha256": "b"}) is True
    assert should_emit_change("CANVAS_FILE_METADATA_UPDATED", {"url": "a"}, {"url": "b"}) is False


@pytest.mark.asyncio
async def test_source_edit_preserves_identity_and_failed_probe_preserves_config() -> None:
    settings = Settings(database_url="sqlite:///:memory:", scheduler_enabled=False)
    del settings
    with memory_db() as db:
        connection = create_website_connection(db, {"course_name": "Old", "course_code": "OLD 1", "term": "Fall 2026", "base_url": "https://old.example.edu", "authentication_method": "none"})
        offline = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request))
            )
        )
        renamed = await update_source_connection(
            db,
            connection.id,
            {
                "name": "Renamed Offline",
                "base_url": "https://old.example.edu",
                "course_name": "Old",
                "course_code": "OLD 1",
                "term": "Fall 2026",
                "authentication_method": "none",
                "protected_path_prefix": None,
                "probe_url": None,
                "default_auth_method": None,
                "discovery_path_limit": 100,
            },
            client=offline,
        )
        assert renamed.name == "Renamed Offline"
        await offline.aclose()
        original = (connection.id, connection.base_url, dict(connection.config_json))
        failed = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
        with pytest.raises(ValueError):
            await update_source_connection(db, connection.id, {"name": "Broken", "base_url": "https://broken.example.edu", "course_name": "Broken", "course_code": "BAD", "term": "Fall 2026", "authentication_method": "none", "protected_path_prefix": None, "probe_url": None, "default_auth_method": None, "discovery_path_limit": 100}, client=failed)
        db.rollback()
        db.refresh(connection)
        assert (connection.id, connection.base_url, connection.config_json) == original
        await failed.aclose()
        ok = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
        updated = await update_source_connection(db, connection.id, {"name": "New Site", "base_url": "https://new.example.edu", "course_name": "New Course", "course_code": "NEW 2", "term": "Spring 2027", "authentication_method": "none", "protected_path_prefix": None, "probe_url": None, "default_auth_method": None, "discovery_path_limit": 200}, client=ok)
        assert updated.id == original[0]
        assert updated.base_url == "https://new.example.edu"
        await ok.aclose()


def test_task_ignore_excludes_today_and_planner_then_undo_restores(tmp_path) -> None:
    now = datetime(2026, 9, 3, 14, tzinfo=UTC)
    availability = tmp_path / "availability.yaml"
    availability.write_text("availability:\n  thursday:\n    - ['09:00', '17:00']\n", encoding="utf-8")
    with memory_db() as db:
        course = add_course(db)
        task = Task(course_id=course.id, title="Ignore me", status="NOT_STARTED", due_at=now + timedelta(hours=4))
        db.add(task)
        db.flush()
        db.add(TaskProgress(task_id=task.id, status="NOT_STARTED", progress_percent=0))
        db.commit()
        assert TodayEngine().build(db, now)["work"]
        task.ignored_at = now
        db.commit()
        assert TodayEngine().build(db, now)["work"] == []
        plan = Planner(availability).rebuild(db, now)
        assert plan.id and not plan.status == "missing"
        task.ignored_at = None
        db.commit()
        assert TodayEngine().build(db, now)["work"]


def test_work_heartbeat_persists_progress_without_trusting_elapsed_snapshot() -> None:
    now = datetime(2026, 9, 3, 14, tzinfo=UTC)
    with memory_db() as db:
        course = add_course(db)
        task = Task(course_id=course.id, title="Work", status="NOT_STARTED")
        db.add(task)
        db.commit()
        start_task_work(db, task.id, now)
        summary = record_work_heartbeat(db, task.id, progress_percent=25, manual_adjustment_minutes=5, now=now + timedelta(seconds=5))
        assert summary["manual_adjustment_minutes"] == 5
        assert db.scalar(select(TaskProgress).where(TaskProgress.task_id == task.id)).progress_percent == 25


def test_calendar_ai_execution_rejects_imported_event_and_applies_manual_plan() -> None:
    with memory_db() as db:
        course = add_course(db)
        imported = CalendarEvent(calendar_id="ics:1", external_id="x", source="ics", summary="Imported", start_at=datetime(2026, 9, 8, 15, tzinfo=UTC), end_at=datetime(2026, 9, 8, 16, tzinfo=UTC), read_only=True)
        db.add(imported)
        db.commit()
        forbidden = CalendarMutationPlan.model_validate({"type": "calendar_mutation_plan", "summary": "Delete imported", "operations": [{"operation": "delete_manual_event", "event_id": imported.id}], "requires_confirmation": True})
        with pytest.raises(ValueError, match="manual"):
            execute_calendar_mutation(db, forbidden)
        db.rollback()
        allowed = CalendarMutationExecuteIn.model_validate({"plan": {"type": "calendar_mutation_plan", "summary": "Add dentist and commute", "operations": [{"operation": "create_manual_event", "event": {"summary": "Dentist", "start": "2026-09-08T10:00:00-05:00", "end": "2026-09-08T11:00:00-05:00", "timezone": "America/Chicago", "event_type": "personal"}}, {"operation": "set_course_commute", "course_id": course.id, "minutes": 20}], "requires_confirmation": True}})
        result = execute_calendar_mutation(db, allowed.plan)
        assert len(result["applied"]) == 2
        assert course.commute_minutes == 20
        assert db.scalar(select(CalendarEvent).where(CalendarEvent.source == "manual")).summary == "Dentist"


def test_notification_dismissal_field_is_persistent() -> None:
    with memory_db() as db:
        row = Notification(level="info", title="Notice", body="Body", dedupe_key="notice-1")
        db.add(row)
        db.commit()
        row.dismissed_at = datetime.now(UTC)
        db.commit()
        assert db.get(Notification, row.id).dismissed_at is not None


def test_notification_banners_are_high_signal_limited_and_match_frontend_contract() -> None:
    with memory_db() as db:
        db.add(Notification(level="minor", title="Metadata noise", body="Changed canvadoc_session_url", dedupe_key="noise"))
        for index in range(5):
            db.add(Notification(level="important", title=f"Action {index}", body=f"Body {index}", dedupe_key=f"action-{index}"))
        dismissed = Notification(level="critical", title="Dismissed", body="Hidden", dedupe_key="dismissed", dismissed_at=datetime.now(UTC))
        db.add(dismissed)
        db.commit()

        result = notifications(db)

        assert len(result) == 3
        assert [row["title"] for row in result] == ["Action 4", "Action 3", "Action 2"]
        assert result[0]["body"] == "Body 4"
        assert "message" not in result[0]


def test_notification_banner_keeps_older_critical_ahead_of_newer_important() -> None:
    with memory_db() as db:
        db.add(Notification(level="critical", title="Critical", body="Action", dedupe_key="older-critical"))
        for index in range(4):
            db.add(Notification(level="important", title=f"New {index}", body="Action", dedupe_key=f"new-{index}"))
        db.commit()
        assert [row["title"] for row in notifications(db)] == ["Critical", "New 3", "New 2"]
