import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.timezone import UIUC_TZ, as_utc


class ExpirationSource(StrEnum):
    CANVAS_API = "CANVAS_API"
    USER_ENTERED = "USER_ENTERED"
    UIUC_30_DAY_POLICY = "UIUC_30_DAY_POLICY"
    UNKNOWN = "UNKNOWN"


class ExpirationState(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    URGENT = "urgent"
    CRITICAL = "critical"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CanvasExpiration:
    expires_at: datetime | None
    days_remaining: int | None
    state: ExpirationState


def parse_expiration(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UIUC_TZ)
    return as_utc(parsed)


def expiration_status(
    expires_at: datetime | str | None,
    now: datetime | None = None,
) -> CanvasExpiration:
    expiry = parse_expiration(expires_at)
    if expiry is None:
        return CanvasExpiration(None, None, ExpirationState.UNKNOWN)
    current = as_utc(now or datetime.now(UTC))
    seconds = (expiry - current).total_seconds()
    if seconds <= 0:
        return CanvasExpiration(expiry, 0, ExpirationState.EXPIRED)
    days = max(1, math.ceil(seconds / 86_400))
    if days <= 1:
        state = ExpirationState.CRITICAL
    elif days <= 3:
        state = ExpirationState.URGENT
    elif days <= 7:
        state = ExpirationState.WARNING
    else:
        state = ExpirationState.NORMAL
    return CanvasExpiration(expiry, days, state)


def new_pat_metadata(
    existing: dict[str, Any] | None,
    *,
    expiration_date: date | None = None,
    now: datetime | None = None,
    expiration_source: ExpirationSource | None = None,
    exact_expires_at: datetime | None = None,
) -> dict[str, Any]:
    activated = as_utc(now or datetime.now(UTC))
    if exact_expires_at is not None:
        expires = as_utc(exact_expires_at)
        source = expiration_source or ExpirationSource.CANVAS_API
    elif expiration_date is not None:
        expires = as_utc(datetime.combine(expiration_date, time.max, UIUC_TZ))
        source = ExpirationSource.USER_ENTERED
    else:
        # Verification proves neither issuance time nor a 30-day lifetime.
        expires = None
        source = ExpirationSource.UNKNOWN
    return {
        **(existing or {}),
        "issued_at": None,
        "activated_at": activated.isoformat(),
        "expires_at": expires.isoformat() if expires else None,
        "expiration_source": source.value,
        "expiration_lifecycle_id": uuid4().hex,
    }


def lifecycle_payload(metadata: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    values = metadata or {}
    status = expiration_status(values.get("expires_at"), now)
    return {
        "issued_at": values.get("issued_at"),
        "activated_at": values.get("activated_at"),
        "expires_at": status.expires_at,
        "expiration_source": values.get("expiration_source", ExpirationSource.UNKNOWN.value),
        "days_remaining": status.days_remaining,
        "expiration_state": status.state.value,
    }
