from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import CalendarEvent, Course, StudyAvailabilityOverride, StudyAvailabilityRule
from app.services.calendar_recurrence import recurrence
from app.timezone import UIUC_TIMEZONE, as_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    is_class: bool = False


class CalendarProvider(Protocol):
    def list_calendars(self) -> list[dict]: ...
    def list_events(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]: ...
    def sync_events(self) -> int: ...
    def health(self) -> dict: ...


class ManualCalendarProvider:
    def __init__(self, db: Session):
        self.db = db

    def list_calendars(self) -> list[dict]:
        return [{"id": "manual", "name": "Manual Calendar", "read_only": False}]

    def list_events(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        # Recurring masters may begin before the query window, so keep all active
        # recurring rows and constrain ordinary rows in SQL.
        return list(
            self.db.scalars(
                select(CalendarEvent).where(
                    CalendarEvent.status != "cancelled",
                    (CalendarEvent.recurrence_rule.is_not(None))
                    | (
                        (CalendarEvent.start_at < as_utc(time_max))
                        & (CalendarEvent.end_at > as_utc(time_min))
                    ),
                )
            )
        )

    def sync_events(self) -> int:
        return 0

    def health(self) -> dict:
        return {"provider": "manual", "status": "ACTIVE", "read_only": False}


class GoogleCalendarProvider:
    """Legacy provider interface; live OAuth orchestration uses GoogleCalendarService."""

    def list_calendars(self) -> list[dict]:
        raise NotImplementedError("Use GoogleCalendarService with a database session")

    def list_events(self, time_min: datetime, time_max: datetime) -> list[CalendarEvent]:
        del time_min, time_max
        raise NotImplementedError("Use GoogleCalendarService with a database session")

    def sync_events(self) -> int:
        raise NotImplementedError("Use GoogleCalendarService with a database session")

    def health(self) -> dict:
        return {"provider": "google", "status": "NOT_CONNECTED", "read_only": True}


def expand_event(
    event: CalendarEvent, time_min: datetime, time_max: datetime, *, strict: bool = False
) -> list[Interval]:
    minimum = as_utc(time_min)
    maximum = as_utc(time_max)
    try:
        timezone = ZoneInfo(event.timezone or UIUC_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        if strict:
            raise ValueError("unsupported_timezone") from error
        logger.warning("Calendar event %s has an unsupported stored timezone; capacity is conservative", event.id)
        return [Interval(minimum, maximum)]
    original_start = as_utc(event.start_at).astimezone(timezone)
    original_end = as_utc(event.end_at).astimezone(timezone)
    duration = original_end - original_start
    if not event.recurrence_rule:
        return [
            Interval(as_utc(event.start_at), as_utc(event.end_at), event.event_type == "class")
        ] if as_utc(event.start_at) < maximum and as_utc(event.end_at) > minimum else []

    try:
        rule = recurrence(event.recurrence_rule, original_start)
    except ValueError:
        if strict:
            raise
        # Do not turn an unknown recurring commitment into falsely free study time.
        # The graphical view reports the exact editable master separately.
        logger.warning("Calendar event %s has an unsupported stored recurrence; capacity is conservative", event.id)
        return [Interval(minimum, maximum)]
    # Include masters starting before the window whose multi-day duration overlaps it.
    # One extra local day covers offset changes across the overlap boundary.
    lower = minimum.astimezone(timezone) - duration - timedelta(days=1)
    upper = maximum.astimezone(timezone)
    output: list[Interval] = []
    for local_start in rule.between(lower, upper, inc=True):
        local_end = local_start + duration
        start_utc, end_utc = local_start.astimezone(UTC), local_end.astimezone(UTC)
        if start_utc < maximum and end_utc > minimum:
            output.append(Interval(start_utc, end_utc, event.event_type == "class"))
    return output


def merge_busy(intervals: Iterable[Interval], class_gap_minutes: int = 30) -> list[Interval]:
    rows = sorted(intervals, key=lambda row: (row.start, row.end))
    merged: list[Interval] = []
    for current in rows:
        if not merged:
            merged.append(current)
            continue
        previous = merged[-1]
        gap = (current.start - previous.end).total_seconds() / 60
        bridge_class_gap = previous.is_class and current.is_class and gap < class_gap_minutes
        if current.start <= previous.end or bridge_class_gap:
            merged[-1] = Interval(
                previous.start,
                max(previous.end, current.end),
                previous.is_class and current.is_class,
            )
        else:
            merged.append(current)
    return merged


def subtract_intervals(available: Iterable[Interval], busy: Iterable[Interval]) -> list[Interval]:
    remaining = list(available)
    for block in merge_busy(busy):
        next_rows: list[Interval] = []
        for slot in remaining:
            if block.end <= slot.start or block.start >= slot.end:
                next_rows.append(slot)
                continue
            if block.start > slot.start:
                next_rows.append(Interval(slot.start, min(block.start, slot.end)))
            if block.end < slot.end:
                next_rows.append(Interval(max(block.end, slot.start), slot.end))
        remaining = next_rows
    return [row for row in remaining if row.end > row.start]


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _availability_for_day(db: Session, day: date, timezone: ZoneInfo) -> list[Interval] | None:
    override = db.scalar(
        select(StudyAvailabilityOverride).where(StudyAvailabilityOverride.date == day)
    )
    if override is not None:
        values = override.available_intervals or []
    else:
        rules = list(
            db.scalars(
                select(StudyAvailabilityRule).where(
                    StudyAvailabilityRule.weekday == day.weekday(),
                    StudyAvailabilityRule.enabled.is_(True),
                )
            )
        )
        if not rules:
            any_rules = db.scalar(select(StudyAvailabilityRule.id).limit(1)) is not None
            return [] if any_rules else None
        values = [
            {"start": row.start_local_time, "end": row.end_local_time} for row in rules
        ]
    return _availability_intervals(values, day, timezone)


def _availability_intervals(values: list[dict], day: date, timezone: ZoneInfo) -> list[Interval]:
    return [
        Interval(
            datetime.combine(day, _clock(row["start"]), timezone).astimezone(UTC),
            datetime.combine(day, _clock(row["end"]), timezone).astimezone(UTC),
        )
        for row in values
        if row.get("start") and row.get("end") and _clock(row["end"]) > _clock(row["start"])
    ]


def free_capacity_between(
    db: Session,
    time_min: datetime,
    time_max: datetime,
    timezone_name: str = UIUC_TIMEZONE,
) -> tuple[int | None, str]:
    minimum, maximum = as_utc(time_min), as_utc(time_max)
    if maximum <= minimum:
        return 0, "calendar"
    return CapacityReader(db, minimum, maximum, timezone_name).between(minimum, maximum)


class CapacityReader:
    """A single request's calendar inputs, shared across its deadline windows.

    Keep expansion/clipping specific to each window: selecting an ordinary event
    outside that window and applying its commute would change existing capacity.
    No object is retained across requests or a write/commit.
    """

    def __init__(self, db: Session, time_min: datetime, time_max: datetime,
                 timezone_name: str = UIUC_TIMEZONE):
        self.minimum, self.maximum = as_utc(time_min), as_utc(time_max)
        self.timezone = ZoneInfo(timezone_name)
        self.events = ManualCalendarProvider(db).list_events(self.minimum, self.maximum)
        self.commutes = {row.id: max(0, row.commute_minutes or 0)
                         for row in db.execute(select(Course.id, Course.commute_minutes))}
        self.rules = list(db.scalars(select(StudyAvailabilityRule)))
        self.overrides = {row.date: row.available_intervals or [] for row in db.scalars(
            select(StudyAvailabilityOverride).where(
                StudyAvailabilityOverride.date >= self.minimum.astimezone(self.timezone).date(),
                StudyAvailabilityOverride.date <= self.maximum.astimezone(self.timezone).date(),
            ))}
        self._days: dict[date, list[Interval] | None] = {}
        self._windows: dict[tuple[datetime, datetime], tuple[int | None, str]] = {}

    def _day(self, day: date) -> list[Interval] | None:
        if day not in self._days:
            if day in self.overrides:
                values = self.overrides[day]
            else:
                values = [{"start": row.start_local_time, "end": row.end_local_time}
                          for row in self.rules if row.enabled and row.weekday == day.weekday()]
                if not values and not self.rules:
                    self._days[day] = None
                    return None
            self._days[day] = _availability_intervals(values, day, self.timezone)
        return self._days[day]

    def between(self, time_min: datetime, time_max: datetime) -> tuple[int | None, str]:
        minimum, maximum = as_utc(time_min), as_utc(time_max)
        if maximum <= minimum:
            return 0, "calendar"
        if minimum < self.minimum or maximum > self.maximum:
            raise ValueError("Capacity window exceeds this request's loaded range")
        key = (minimum, maximum)
        if key in self._windows:
            return self._windows[key]
        events = [event for event in self.events if event.recurrence_rule is not None or (
            as_utc(event.start_at) < maximum and as_utc(event.end_at) > minimum)]
        busy: list[Interval] = []
        for event in events:
            if (event.transparency or "opaque").lower() == "transparent":
                continue
            for item in expand_event(event, minimum, maximum):
                minutes = self.commutes.get(event.course_id or -1, 0) if item.is_class else 0
                busy.append(Interval(item.start - timedelta(minutes=minutes),
                                     item.end + timedelta(minutes=minutes), item.is_class))
        day = minimum.astimezone(self.timezone).date()
        last = maximum.astimezone(self.timezone).date()
        total, configured = 0.0, False
        while day <= last:
            availability = self._day(day)
            if availability is not None:
                configured = True
                clipped = [Interval(max(slot.start, minimum), min(slot.end, maximum))
                           for slot in availability
                           if min(slot.end, maximum) > max(slot.start, minimum)]
                total += sum((row.end - row.start).total_seconds() / 60
                             for row in subtract_intervals(clipped, busy))
            day += timedelta(days=1)
        source = "calendar" if events and configured else "availability" if configured else "fallback"
        result = (max(0, round(total)), source) if configured else (None, source)
        self._windows[key] = result
        return result
