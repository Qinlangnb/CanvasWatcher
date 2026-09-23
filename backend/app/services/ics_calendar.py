from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import recurring_ical_events
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import CalendarEvent, ICSCalendarImportSource, utcnow
from app.timezone import UIUC_TIMEZONE, as_utc


class ICSValidationError(ValueError):
    pass


class ICSRemovalConfirmationRequired(ICSValidationError):
    def __init__(self, reconciliation: dict[str, int]):
        super().__init__("Re-import removes existing events and requires confirmation")
        self.reconciliation = reconciliation


@dataclass(frozen=True)
class ParsedEvent:
    external_id: str
    summary: str
    description: str
    location: str | None
    start_at: datetime
    end_at: datetime
    all_day: bool
    start_date: date | None
    end_date: date | None
    timezone: str
    recurring_event_id: str | None
    original_start_at: datetime | None
    status: str
    transparency: str


@dataclass(frozen=True)
class ParsedCalendar:
    filename: str
    sha256: str
    name: str
    timezone: str | None
    timezone_confirmation_required: bool
    event_count: int
    recurring_series_count: int
    range_start: date | None
    range_end: date | None
    events: tuple[ParsedEvent, ...]


def _text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()[:maximum]


def _valid_timezone(value: str | None) -> str | None:
    if not value:
        return None
    candidate = str(value).strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return None
    return candidate


def _calendar_timezone(calendar: Calendar) -> str | None:
    direct = _valid_timezone(_text(calendar.get("X-WR-TIMEZONE"), 80))
    if direct:
        return direct
    for component in calendar.walk("VTIMEZONE"):
        candidate = _valid_timezone(_text(component.get("TZID"), 80))
        if candidate:
            return candidate
    return None


def _decoded(component: Any, name: str) -> Any:
    try:
        return component.decoded(name)
    except (KeyError, ValueError, TypeError):
        return None


def _event_timezone(component: Any, calendar_timezone: str | None) -> tuple[str, bool]:
    for key in ("DTSTART", "DTEND"):
        prop = component.get(key)
        tzid = _valid_timezone(str(prop.params.get("TZID"))) if prop is not None else None
        if tzid:
            return tzid, False
        value = _decoded(component, key)
        if isinstance(value, datetime) and value.tzinfo is not None:
            name = getattr(value.tzinfo, "key", None)
            if name and _valid_timezone(name):
                return name, False
            if value.utcoffset() == timedelta(0):
                return "UTC", False
    if calendar_timezone:
        return calendar_timezone, False
    return UIUC_TIMEZONE, True


def _normalize_datetime(value: date | datetime, timezone: ZoneInfo) -> tuple[datetime, date | None]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone)
        return as_utc(value), None
    return datetime.combine(value, time.min, timezone).astimezone(UTC), value


def _parse_component(component: Any, calendar_timezone: str | None) -> ParsedEvent | None:
    uid = _text(component.get("UID"), 512)
    start_value = _decoded(component, "DTSTART")
    if not uid or not isinstance(start_value, (date, datetime)):
        return None
    timezone_name, _ambiguous = _event_timezone(component, calendar_timezone)
    timezone = ZoneInfo(timezone_name)
    start_at, start_day = _normalize_datetime(start_value, timezone)
    end_value = _decoded(component, "DTEND")
    duration = _decoded(component, "DURATION")
    if isinstance(end_value, (date, datetime)):
        end_at, end_day = _normalize_datetime(end_value, timezone)
    elif isinstance(duration, timedelta):
        end_at, end_day = start_at + duration, (
            start_day + duration if start_day is not None else None
        )
    else:
        end_at = start_at + (timedelta(days=1) if start_day else timedelta(hours=1))
        end_day = start_day + timedelta(days=1) if start_day else None
    if end_at <= start_at:
        return None
    recurrence_value = _decoded(component, "RECURRENCE-ID")
    recurring = isinstance(recurrence_value, (date, datetime))
    original_start_at = (
        _normalize_datetime(recurrence_value, timezone)[0] if recurring else None
    )
    identity = (
        f"{uid}::{recurrence_value.isoformat()}"
        if recurring
        else uid
    )
    return ParsedEvent(
        external_id=identity[:1024],
        summary=_text(component.get("SUMMARY"), 512) or "Busy",
        description=_text(component.get("DESCRIPTION"), 5000),
        location=_text(component.get("LOCATION"), 512) or None,
        start_at=start_at,
        end_at=end_at,
        all_day=start_day is not None,
        start_date=start_day,
        end_date=end_day,
        timezone=timezone_name,
        recurring_event_id=uid if recurring else None,
        original_start_at=original_start_at,
        status=(_text(component.get("STATUS"), 24) or "CONFIRMED").lower(),
        transparency=(_text(component.get("TRANSP"), 16) or "OPAQUE").lower(),
    )


def parse_ics(
    data: bytes,
    filename: str,
    settings: Settings,
    *,
    timezone_override: str | None = None,
) -> ParsedCalendar:
    if not data:
        raise ICSValidationError("Calendar file is empty")
    if len(data) > settings.ics_max_file_bytes:
        raise ICSValidationError(
            f"Calendar file exceeds the {settings.ics_max_file_bytes} byte limit"
        )
    if not filename.lower().endswith(".ics") and b"BEGIN:VCALENDAR" not in data[:4096].upper():
        raise ICSValidationError("Upload an .ics iCalendar file")
    try:
        calendar = Calendar.from_ical(data)
    except Exception as error:
        raise ICSValidationError("The iCalendar file is malformed") from error
    if _text(calendar.name, 32).upper() != "VCALENDAR":
        raise ICSValidationError("The file does not contain VCALENDAR data")
    raw_events = list(calendar.walk("VEVENT"))
    if not raw_events:
        raise ICSValidationError("The calendar does not contain any events")
    if len(raw_events) > settings.ics_max_events:
        raise ICSValidationError("The calendar contains too many events")
    detected_timezone = _calendar_timezone(calendar)
    override = _valid_timezone(timezone_override)
    if timezone_override and not override:
        raise ICSValidationError("Timezone override must be a valid IANA timezone")
    effective_timezone = override or detected_timezone
    floating = any(_event_timezone(component, effective_timezone)[1] for component in raw_events)
    now = utcnow()
    range_start = now - timedelta(days=settings.ics_expansion_past_days)
    range_end = now + timedelta(days=settings.ics_expansion_future_days)
    try:
        expanded = recurring_ical_events.of(
            calendar,
            keep_recurrence_attributes=True,
            skip_bad_series=False,
        ).between(range_start, range_end)
    except Exception as error:
        raise ICSValidationError("The calendar recurrence data is invalid") from error
    if len(expanded) > settings.ics_max_events:
        raise ICSValidationError("Expanded recurrence exceeds the event limit")
    parsed = tuple(
        event
        for component in expanded
        if (event := _parse_component(component, effective_timezone)) is not None
        and event.status != "cancelled"
    )
    if not parsed:
        raise ICSValidationError("No usable events fall within the bounded import range")
    days = [event.start_date or event.start_at.astimezone(ZoneInfo(event.timezone)).date() for event in parsed]
    recurring_count = sum(
        1 for component in raw_events if component.get("RRULE") or component.get("RDATE")
    )
    return ParsedCalendar(
        filename=filename[:512],
        sha256=hashlib.sha256(data).hexdigest(),
        name=_text(calendar.get("X-WR-CALNAME"), 512) or filename.rsplit(".", 1)[0][:512],
        timezone=effective_timezone,
        timezone_confirmation_required=bool(floating and not override and not detected_timezone),
        event_count=len(parsed),
        recurring_series_count=recurring_count,
        range_start=min(days) if days else None,
        range_end=max(days) if days else None,
        events=parsed,
    )


def _event_values(event: ParsedEvent) -> dict[str, Any]:
    return {
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "start_at": event.start_at,
        "end_at": event.end_at,
        "all_day": event.all_day,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "timezone": event.timezone,
        "recurring_event_id": event.recurring_event_id,
        "original_start_at": event.original_start_at,
        "status": event.status,
        "transparency": event.transparency,
    }


def _reconciliation(
    existing: dict[str, CalendarEvent], incoming: dict[str, ParsedEvent]
) -> dict[str, int]:
    added = set(incoming) - set(existing)
    removed = set(existing) - set(incoming)
    updated = {
        key
        for key in set(existing) & set(incoming)
        if any(
            (
                as_utc(getattr(existing[key], field))
                if isinstance(getattr(existing[key], field), datetime)
                else getattr(existing[key], field)
            )
            != (as_utc(value) if isinstance(value, datetime) else value)
            for field, value in _event_values(incoming[key]).items()
        )
    }
    unchanged = set(existing) & set(incoming) - updated
    return {
        "new": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "unchanged": len(unchanged),
    }


def preview_payload(
    parsed: ParsedCalendar,
    *,
    reconciliation: dict[str, int] | None = None,
    no_op: bool = False,
) -> dict[str, Any]:
    reconciliation = reconciliation or {
        "new": parsed.event_count,
        "updated": 0,
        "removed": 0,
        "unchanged": 0,
    }
    return {
        "status": "ready",
        "events_found": parsed.event_count,
        "warnings": ["Timezone confirmation is required for floating times"] if parsed.timezone_confirmation_required else [],
        "original_filename": parsed.filename,
        "file_sha256": parsed.sha256,
        "calendar_name": parsed.name,
        "calendar_timezone": parsed.timezone,
        "timezone_confirmation_required": parsed.timezone_confirmation_required,
        "event_count": parsed.event_count,
        "recurring_series_count": parsed.recurring_series_count,
        "date_range_start": parsed.range_start,
        "date_range_end": parsed.range_end,
        "reconciliation": reconciliation,
        "no_op": no_op,
    }


def preview_ics(
    db: Session,
    data: bytes,
    filename: str,
    settings: Settings,
    *,
    source_id: int | None = None,
    timezone_override: str | None = None,
) -> dict[str, Any]:
    parsed = parse_ics(data, filename, settings, timezone_override=timezone_override)
    if source_id is None:
        return preview_payload(parsed)
    source = db.get(ICSCalendarImportSource, source_id)
    if source is None:
        raise ICSValidationError("Calendar import source was not found")
    existing = {
        row.external_id: row
        for row in db.scalars(
            select(CalendarEvent).where(CalendarEvent.import_source_id == source.id)
        )
        if row.external_id
    }
    incoming = {row.external_id: row for row in parsed.events}
    return preview_payload(
        parsed,
        reconciliation=_reconciliation(existing, incoming),
        no_op=source.file_sha256 == parsed.sha256,
    )


def import_ics(
    db: Session,
    data: bytes,
    filename: str,
    settings: Settings,
    *,
    source_id: int | None = None,
    timezone_override: str | None = None,
    confirm_removals: bool = False,
) -> dict[str, Any]:
    parsed = parse_ics(data, filename, settings, timezone_override=timezone_override)
    if parsed.timezone_confirmation_required:
        raise ICSValidationError("Confirm a timezone for floating calendar times")
    source = db.get(ICSCalendarImportSource, source_id) if source_id else None
    if source_id and source is None:
        raise ICSValidationError("Calendar import source was not found")
    if source and source.file_sha256 == parsed.sha256:
        return {
            "source_id": source.id,
            **preview_payload(parsed, reconciliation={"new": 0, "updated": 0, "removed": 0, "unchanged": source.event_count}, no_op=True),
        }
    if source is None:
        source = ICSCalendarImportSource(
            original_filename=parsed.filename,
            file_sha256=parsed.sha256,
            calendar_name=parsed.name,
            calendar_timezone=parsed.timezone,
            event_count=0,
            recurring_series_count=parsed.recurring_series_count,
            date_range_start=parsed.range_start,
            date_range_end=parsed.range_end,
        )
        db.add(source)
        db.flush()
    existing = {
        row.external_id: row
        for row in db.scalars(
            select(CalendarEvent).where(CalendarEvent.import_source_id == source.id)
        )
        if row.external_id
    }
    incoming = {row.external_id: row for row in parsed.events}
    reconciliation = _reconciliation(existing, incoming)
    if reconciliation["removed"] and source_id and not confirm_removals:
        raise ICSRemovalConfirmationRequired(reconciliation)
    for external_id, event in incoming.items():
        row = existing.get(external_id)
        if row is None:
            row = CalendarEvent(
                calendar_id=f"ics:{source.id}",
                external_id=external_id,
                source="ics",
                import_source_id=source.id,
                event_type="busy",
                read_only=True,
                **_event_values(event),
            )
            db.add(row)
        else:
            for field, value in _event_values(event).items():
                setattr(row, field, value)
            row.read_only = True
    for external_id in set(existing) - set(incoming):
        db.delete(existing[external_id])
    source.original_filename = parsed.filename
    source.file_sha256 = parsed.sha256
    source.calendar_name = parsed.name
    source.calendar_timezone = parsed.timezone
    source.event_count = len(incoming)
    source.recurring_series_count = parsed.recurring_series_count
    source.date_range_start = parsed.range_start
    source.date_range_end = parsed.range_end
    source.last_reimported_at = utcnow() if source_id else None
    source.state = "IMPORTED"
    db.commit()
    return {
        "source_id": source.id,
        **preview_payload(parsed, reconciliation=reconciliation),
    }


def list_ics_sources(db: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "original_filename": row.original_filename,
            "file_sha256": row.file_sha256,
            "calendar_name": row.calendar_name,
            "calendar_timezone": row.calendar_timezone,
            "imported_at": row.imported_at,
            "last_reimported_at": row.last_reimported_at,
            "event_count": row.event_count,
            "recurring_series_count": row.recurring_series_count,
            "date_range_start": row.date_range_start,
            "date_range_end": row.date_range_end,
            "state": row.state,
            "read_only": True,
        }
        for row in db.scalars(
            select(ICSCalendarImportSource).order_by(ICSCalendarImportSource.calendar_name)
        )
    ]


def remove_ics_source(db: Session, source_id: int) -> bool:
    source = db.get(ICSCalendarImportSource, source_id)
    if source is None:
        return False
    for event in db.scalars(
        select(CalendarEvent).where(CalendarEvent.import_source_id == source.id)
    ):
        db.delete(event)
    db.delete(source)
    db.commit()
    return True
