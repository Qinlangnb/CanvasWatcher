from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base, CalendarEvent, Course, StudyAvailabilityRule
from app.services.calendar_capacity import free_capacity_between
from app.services.calendar_view import calendar_view


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def event(db, start, end, **kwargs):
    row = CalendarEvent(calendar_id="manual", source="manual", summary="Class",
                        start_at=start, end_at=end, timezone="America/Chicago", **kwargs)
    db.add(row)
    db.flush()
    return row


def test_bounded_projection_preserves_dst_and_stable_instance_ids(db):
    start = datetime(2026, 3, 2, 15, tzinfo=UTC)  # 09:00 CST
    event(db, start, start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;BYDAY=MO")
    window = (datetime(2026, 3, 8, tzinfo=UTC), datetime(2026, 3, 16, tzinfo=UTC))
    rows = calendar_view(db, *window)["instances"]
    assert len(rows) == 1
    assert rows[0]["start"] == "2026-03-09T14:00:00+00:00"  # 09:00 CDT
    assert calendar_view(db, *window)["instances"][0]["key"] == rows[0]["key"]
    with pytest.raises(ValueError):
        calendar_view(db, window[0], window[0] + timedelta(days=63))
    with pytest.raises(ValueError):
        calendar_view(db, window[0].replace(tzinfo=None), window[1])


def test_projection_draws_commute_gaps_and_availability_without_writes(db):
    course = Course(source="manual", external_id="fixture", course_code="TEST", name="Test", commute_minutes=5)
    db.add(course)
    db.flush()
    start = datetime(2026, 9, 14, 14, tzinfo=UTC)
    event(db, start, start + timedelta(hours=1), event_type="class", course_id=course.id)
    event(db, start + timedelta(minutes=80), start + timedelta(minutes=140), event_type="class", course_id=course.id)
    db.add(StudyAvailabilityRule(weekday=0, start_local_time="08:00", end_local_time="17:00", enabled=True))
    db.commit()
    end = start + timedelta(hours=9)
    before = free_capacity_between(db, start, end)
    view = calendar_view(db, start, end)
    assert {row["kind"] for row in view["layers"]} == {"availability", "commute", "short_gap"}
    assert len(view["instances"]) == 2
    assert free_capacity_between(db, start, end) == before
    assert not db.dirty and not db.new


def test_all_day_ics_is_read_only_and_dates_not_shifted(db):
    start = datetime(2026, 9, 14, 5, tzinfo=UTC)
    row = event(db, start, start + timedelta(days=1), all_day=True, read_only=True)
    row.source = "ics"
    db.commit()
    instance = calendar_view(db, start, start + timedelta(days=2))["instances"][0]
    assert instance["start_date"] == "2026-09-14"
    assert instance["end_date"] == "2026-09-15"
    assert instance["event"]["read_only"]


def test_recurrence_count_is_from_master_not_query_window(db):
    start = datetime(2026, 9, 1, 14, tzinfo=UTC)
    event(db, start, start + timedelta(hours=1), recurrence_rule="FREQ=DAILY;COUNT=3")
    assert len(calendar_view(db, start + timedelta(days=1), start + timedelta(days=10))["instances"]) == 2
    assert calendar_view(db, start + timedelta(days=3), start + timedelta(days=10))["instances"] == []


def test_monthly_recurrence_skips_invalid_month_days(db):
    start = datetime(2026, 1, 31, 15, tzinfo=UTC)
    event(db, start, start + timedelta(hours=1), recurrence_rule="FREQ=MONTHLY;COUNT=3")
    rows = calendar_view(db, datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC))["instances"]
    assert [row["start"] for row in rows] == ["2026-03-31T14:00:00+00:00"]


def test_biweekly_uses_calendar_week_not_seven_days_from_master(db):
    start = datetime(2026, 9, 9, 14, tzinfo=UTC)  # Wednesday
    event(db, start, start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=3")
    rows = calendar_view(db, start, start + timedelta(days=20))["instances"]
    assert [row["start"][:10] for row in rows] == ["2026-09-09", "2026-09-21", "2026-09-23"]


def test_recurring_multiday_overlap_and_date_until(db):
    start = datetime(2026, 9, 1, 14, tzinfo=UTC)
    event(db, start, start + timedelta(days=4), recurrence_rule="FREQ=WEEKLY;UNTIL=20260901")
    assert len(calendar_view(db, start + timedelta(days=3), start + timedelta(days=6))["instances"]) == 1
    assert calendar_view(db, start + timedelta(days=7), start + timedelta(days=10))["instances"] == []


@pytest.mark.parametrize("rule", ["FREQ=BOGUS", "FREQ=DAILY;COUNT=0", "FREQ=DAILY;INTERVAL=0",
    "FREQ=DAILY;COUNT=2;UNTIL=20260910", "FREQ=WEEKLY;BYDAY=XX", "FREQ=HOURLY",
    "FREQ=DAILY;BYSECOND=1,2", "FREQ=DAILY;FREQ=WEEKLY"])
def test_invalid_recurrence_rejected_before_persistence(rule):
    from app.schemas import CalendarEventIn

    with pytest.raises(ValueError):
        CalendarEventIn(summary="Invalid", start_at=datetime(2026, 9, 1, 14, tzinfo=UTC),
                        end_at=datetime(2026, 9, 1, 15, tzinfo=UTC), recurrence_rule=rule)


def test_legacy_invalid_rule_does_not_break_other_events_or_overstate_capacity(db):
    start = datetime(2026, 9, 14, 14, tzinfo=UTC)
    broken = event(db, start, start + timedelta(hours=1), recurrence_rule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=9")
    valid = event(db, start, start + timedelta(hours=1))
    db.add(StudyAvailabilityRule(weekday=0, start_local_time="08:00", end_local_time="17:00", enabled=True))
    db.commit()
    view = calendar_view(db, start, start + timedelta(hours=3))
    assert [row["event"]["id"] for row in view["instances"]] == [valid.id]
    assert view["warnings"][0]["event"]["id"] == broken.id
    assert free_capacity_between(db, start, start + timedelta(hours=3)) == (0, "calendar")
    assert broken.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9"


@pytest.mark.parametrize("kind", ["calendar", "settings", "ai"])
def test_unknown_timezone_is_validation_error(kind):
    from pydantic import ValidationError

    from app.schemas import CalendarEventIn, CalendarMutationEvent, GeneralSettingsIn

    start, end = datetime(2026, 9, 14, 14, tzinfo=UTC), datetime(2026, 9, 14, 15, tzinfo=UTC)
    with pytest.raises(ValidationError, match="Unknown timezone"):
        if kind == "calendar":
            CalendarEventIn(summary="x", start_at=start, end_at=end, timezone="Not/AZone")
        elif kind == "settings":
            GeneralSettingsIn(academic_timezone="Not/AZone")
        else:
            CalendarMutationEvent(summary="x", start=start, end=end, timezone="Not/AZone")


def test_invalid_stored_timezone_is_isolated_and_empty_zone_uses_default(db):
    start = datetime(2026, 9, 14, 14, tzinfo=UTC)
    bad = event(db, start, start + timedelta(hours=1))
    bad.timezone = "Not/AZone"
    valid = event(db, start, start + timedelta(hours=1))
    valid.timezone = ""
    db.add(StudyAvailabilityRule(weekday=0, start_local_time="08:00", end_local_time="17:00", enabled=True))
    db.commit()
    view = calendar_view(db, start, start + timedelta(hours=3))
    assert [row["event"]["id"] for row in view["instances"]] == [valid.id]
    assert view["warnings"][0]["code"] == "unsupported_timezone"
    assert view["warnings"][0]["event"]["id"] == bad.id
    assert free_capacity_between(db, start, start + timedelta(hours=3)) == (0, "calendar")
