"""Shared manual-event RRULE validation and expansion semantics."""
from datetime import UTC, datetime, time

from dateutil.rrule import rrulestr


def recurrence(value: str, start: datetime):
    text = value.strip().upper().removeprefix("RRULE:")
    parts: dict[str, str] = {}
    allowed = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY",
               "BYMONTH", "BYSETPOS", "WKST"}
    for part in text.split(";"):
        key, separator, item = part.partition("=")
        if not separator or key not in allowed or key in parts or not item:
            raise ValueError("Invalid or unsupported recurrence rule field")
        parts[key] = item
    if parts.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise ValueError("Recurrence frequency must be DAILY, WEEKLY, MONTHLY or YEARLY")
    for key in ("INTERVAL", "COUNT"):
        if key in parts and (not parts[key].isdigit() or int(parts[key]) < 1):
            raise ValueError(f"Recurrence {key} must be a positive integer")
    if "COUNT" in parts and "UNTIL" in parts:
        raise ValueError("Recurrence must not combine COUNT and UNTIL")
    # Preserve the existing editor's date-only inclusive UNTIL convention.
    if len(parts.get("UNTIL", "")) == 8:
        end = datetime.combine(datetime.strptime(parts["UNTIL"], "%Y%m%d").date(),
                               time(23, 59, 59), start.tzinfo)
        parts["UNTIL"] = end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    parts.setdefault("WKST", "MO")
    try:
        return rrulestr(";".join(f"{key}={item}" for key, item in parts.items()), dtstart=start)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("Invalid recurrence rule") from error
