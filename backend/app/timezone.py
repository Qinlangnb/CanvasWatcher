from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

UIUC_TIMEZONE = "America/Chicago"
UIUC_TZ = ZoneInfo(UIUC_TIMEZONE)


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC instant; SQLite-naive values are treated as stored UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_uiuc(value: datetime) -> datetime:
    return as_utc(value).astimezone(UIUC_TZ)


def uiuc_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, UIUC_TZ)
    end = datetime.combine(day, time.max, UIUC_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def planning_cutoff(day: date) -> datetime:
    """Private planner cutoff for a DATE_ONLY task, never a source-authored time."""
    return datetime.combine(day, time(23, 59), UIUC_TZ).astimezone(UTC)
