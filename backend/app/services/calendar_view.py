"""Bounded graphical projection of the existing event/capacity data, never a store."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Course
from app.schemas import CalendarEventOut
from app.services.calendar_capacity import (
    Interval,
    ManualCalendarProvider,
    _availability_for_day,
    expand_event,
    merge_busy,
    subtract_intervals,
)
from app.timezone import UIUC_TIMEZONE, as_utc


def calendar_view(db: Session, start: datetime, end: datetime) -> dict:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Calendar range requires timezone offsets")
    start, end = as_utc(start), as_utc(end)
    if end <= start or end - start > timedelta(days=62):
        raise ValueError("Calendar range must be positive and at most 62 days")
    zone = ZoneInfo(UIUC_TIMEZONE)
    events = ManualCalendarProvider(db).list_events(start, end)
    commutes = {row.id: max(0, row.commute_minutes or 0) for row in db.scalars(select(Course))}
    instances, layers, classes = [], [], []
    warnings = []
    for event in events:
        master = CalendarEventOut.model_validate(event).model_dump(mode="json")
        try:
            intervals = expand_event(event, start, end, strict=True)
        except ValueError as error:
            is_timezone = str(error) == "unsupported_timezone"
            warnings.append({"event": master, "code": "unsupported_timezone" if is_timezone else "unsupported_recurrence",
                             "message": f"Stored {'timezone' if is_timezone else 'recurrence'} needs repair. Study capacity is conservatively unavailable until corrected."})
            continue
        for interval in intervals:
            key = f"{event.source}:{event.id}:{interval.start.isoformat()}"
            instances.append({
                "key": key, "event": master,
                "start": interval.start.isoformat(), "end": interval.end.isoformat(),
                "start_date": interval.start.astimezone(ZoneInfo(event.timezone or UIUC_TIMEZONE)).date().isoformat(),
                "end_date": interval.end.astimezone(ZoneInfo(event.timezone or UIUC_TIMEZONE)).date().isoformat(),
            })
            if interval.is_class and event.transparency != "transparent":
                minutes = commutes.get(event.course_id, 0)
                extended = Interval(interval.start - timedelta(minutes=minutes),
                                    interval.end + timedelta(minutes=minutes), True)
                classes.append(extended)
                for fragment in subtract_intervals([extended], [interval]):
                    layers.append({"kind": "commute", "start": fragment.start.isoformat(),
                                   "end": fragment.end.isoformat()})
    # Draw only the gaps added by the same merge rule used by capacity.
    unbridged = [Interval(row.start, row.end) for row in classes]
    for fragment in subtract_intervals(merge_busy(classes), unbridged):
        layers.append({"kind": "short_gap", "start": fragment.start.isoformat(),
                       "end": fragment.end.isoformat()})
    day, last = start.astimezone(zone).date(), end.astimezone(zone).date()
    while day <= last:
        for interval in _availability_for_day(db, day, zone) or []:
            if interval.start < end and interval.end > start:
                layers.append({"kind": "availability", "start": interval.start.isoformat(),
                               "end": interval.end.isoformat()})
        day += timedelta(days=1)
    return {"instances": instances, "layers": layers, "timezone": UIUC_TIMEZONE, "warnings": warnings}
