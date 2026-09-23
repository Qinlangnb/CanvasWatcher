from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from dateutil.parser import isoparse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import (
    CalendarEvent,
    GoogleCalendarConnection,
    GoogleCalendarSelection,
    utcnow,
)
from app.timezone import UIUC_TIMEZONE, as_utc

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_ROOT = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)


class GoogleCalendarError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class SyncTokenExpired(GoogleCalendarError):
    pass


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise GoogleCalendarError("Google token encryption key is not configured", status_code=503)
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as error:
            raise GoogleCalendarError(
                "Google token encryption key must be a valid Fernet key", status_code=503
            ) from error

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as error:
            raise GoogleCalendarError(
                "Stored Google authorization cannot be decrypted; reconnect the account",
                status_code=401,
            ) from error


def _connection(db: Session) -> GoogleCalendarConnection:
    value = db.scalar(select(GoogleCalendarConnection).order_by(GoogleCalendarConnection.id).limit(1))
    if value is None:
        value = GoogleCalendarConnection(state="DISCONNECTED")
        db.add(value)
        db.flush()
    return value


def _state_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class GoogleCalendarService:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        auth_url: str = GOOGLE_AUTH_URL,
        token_url: str = GOOGLE_TOKEN_URL,
        api_root: str = GOOGLE_API_ROOT,
    ) -> None:
        self.settings = settings
        self.client = client
        self.auth_url = auth_url
        self.token_url = token_url
        self.api_root = api_root.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.google_calendar_client_id
            and self.settings.google_calendar_client_secret.get_secret_value()
            and self.settings.google_token_encryption_key.get_secret_value()
            and self.settings.google_calendar_redirect_uri
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.client is not None:
            return await self.client.request(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            return await client.request(method, url, **kwargs)

    def _cipher(self) -> TokenCipher:
        return TokenCipher(self.settings.google_token_encryption_key.get_secret_value())

    def status(self, db: Session) -> dict[str, Any]:
        value = db.scalar(
            select(GoogleCalendarConnection).order_by(GoogleCalendarConnection.id).limit(1)
        )
        selections = (
            list(
                db.scalars(
                    select(GoogleCalendarSelection)
                    .where(GoogleCalendarSelection.connection_id == value.id)
                    .order_by(
                        GoogleCalendarSelection.primary.desc(),
                        GoogleCalendarSelection.summary,
                    )
                )
            )
            if value
            else []
        )
        return {
            "configured": self.configured,
            "state": value.state if value else "DISCONNECTED",
            "connected_account": value.connected_account if value else None,
            "connected_at": _aware(value.connected_at) if value else None,
            "last_successful_sync": _aware(value.last_successful_sync) if value else None,
            "last_error": value.last_error if value else None,
            "read_only": True,
            "scopes": list(GOOGLE_SCOPES),
            "calendars": [
                {
                    "id": row.calendar_id,
                    "summary": row.summary,
                    "primary": row.primary,
                    "selected": row.selected,
                    "timezone": row.timezone,
                    "access_role": row.access_role,
                    "state": row.state,
                    "last_sync_at": _aware(row.last_sync_at),
                }
                for row in selections
            ],
        }

    def begin_oauth(self, db: Session) -> str:
        if not self.configured:
            raise GoogleCalendarError(
                "Google Calendar OAuth deployment settings are incomplete", status_code=503
            )
        state = secrets.token_urlsafe(32)
        value = _connection(db)
        value.state = "CONNECTING"
        value.oauth_state_hash = _state_digest(state)
        value.oauth_state_expires_at = utcnow() + timedelta(minutes=10)
        value.last_error = None
        db.commit()
        return f"{self.auth_url}?{urlencode({
            'client_id': self.settings.google_calendar_client_id,
            'redirect_uri': self.settings.google_calendar_redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(GOOGLE_SCOPES),
            'access_type': 'offline',
            'include_granted_scopes': 'true',
            'prompt': 'consent',
            'state': state,
        })}"

    def _validate_state(self, db: Session, state: str) -> GoogleCalendarConnection:
        value = _connection(db)
        expires = _aware(value.oauth_state_expires_at)
        valid = bool(
            value.oauth_state_hash
            and state
            and hmac.compare_digest(value.oauth_state_hash, _state_digest(state))
            and expires
            and expires >= utcnow()
        )
        value.oauth_state_hash = None
        value.oauth_state_expires_at = None
        if not valid:
            value.state = "ERROR"
            value.last_error = "OAuth state validation failed"
            db.commit()
            raise GoogleCalendarError("OAuth state validation failed", status_code=400)
        return value

    async def finish_oauth(
        self, db: Session, *, code: str | None, state: str, denied_error: str | None = None
    ) -> dict[str, Any]:
        value = self._validate_state(db, state)
        if denied_error:
            value.state = "DISCONNECTED"
            value.last_error = "Google authorization was denied"
            db.commit()
            raise GoogleCalendarError("Google authorization was denied", status_code=400)
        if not code:
            raise GoogleCalendarError("Google authorization code is missing")
        response = await self._request(
            "POST",
            self.token_url,
            data={
                "client_id": self.settings.google_calendar_client_id,
                "client_secret": self.settings.google_calendar_client_secret.get_secret_value(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.google_calendar_redirect_uri,
            },
        )
        if response.status_code >= 400:
            value.state = "ERROR"
            value.last_error = f"OAuth token exchange failed (HTTP {response.status_code})"
            db.commit()
            raise GoogleCalendarError("Google token exchange failed", status_code=502)
        token_data = response.json()
        refresh_token = token_data.get("refresh_token")
        if refresh_token:
            value.refresh_token_ciphertext = self._cipher().encrypt(str(refresh_token))
        if not value.refresh_token_ciphertext:
            value.state = "AUTH_REQUIRED"
            value.last_error = "Google did not return an offline refresh token"
            db.commit()
            raise GoogleCalendarError(
                "Google did not return an offline refresh token; reconnect and grant consent",
                status_code=400,
            )
        access_token = str(token_data.get("access_token") or "")
        if not access_token:
            raise GoogleCalendarError("Google token response did not include an access token", status_code=502)
        value.connected_at = utcnow()
        value.state = "ACTIVE"
        value.last_error = None
        await self._refresh_calendar_list(db, value, access_token)
        db.commit()
        return self.status(db)

    async def _access_token(self, db: Session, value: GoogleCalendarConnection) -> str:
        if not value.refresh_token_ciphertext:
            value.state = "AUTH_REQUIRED"
            db.commit()
            raise GoogleCalendarError("Google Calendar requires authorization", status_code=401)
        refresh_token = self._cipher().decrypt(value.refresh_token_ciphertext)
        response = await self._request(
            "POST",
            self.token_url,
            data={
                "client_id": self.settings.google_calendar_client_id,
                "client_secret": self.settings.google_calendar_client_secret.get_secret_value(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            value.state = "AUTH_REQUIRED" if response.status_code in {400, 401} else "DEGRADED"
            value.last_error = f"Google access refresh failed (HTTP {response.status_code})"
            db.commit()
            raise GoogleCalendarError("Google access refresh failed", status_code=401)
        access_token = str(response.json().get("access_token") or "")
        if not access_token:
            raise GoogleCalendarError("Google refresh response was incomplete", status_code=502)
        return access_token

    async def _get_pages(
        self, path: str, access_token: str, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        next_sync_token: str | None = None
        while True:
            request_params = {**params}
            if page_token:
                request_params["pageToken"] = page_token
            response = await self._request(
                "GET",
                f"{self.api_root}/{path.lstrip('/')}",
                params=request_params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 410:
                raise SyncTokenExpired("Google sync token expired", status_code=410)
            if response.status_code >= 400:
                raise GoogleCalendarError(
                    f"Google Calendar API request failed (HTTP {response.status_code})",
                    status_code=502,
                )
            payload = response.json()
            rows.extend(row for row in payload.get("items", []) if isinstance(row, dict))
            page_token = payload.get("nextPageToken")
            if not page_token:
                next_sync_token = payload.get("nextSyncToken")
                break
        return rows, next_sync_token

    async def _refresh_calendar_list(
        self,
        db: Session,
        value: GoogleCalendarConnection,
        access_token: str,
    ) -> list[GoogleCalendarSelection]:
        rows, _ = await self._get_pages("users/me/calendarList", access_token, {"maxResults": 250})
        existing = {
            row.calendar_id: row
            for row in db.scalars(
                select(GoogleCalendarSelection).where(
                    GoogleCalendarSelection.connection_id == value.id
                )
            )
        }
        seen: set[str] = set()
        for item in rows:
            calendar_id = str(item.get("id") or "")
            if not calendar_id:
                continue
            seen.add(calendar_id)
            row = existing.get(calendar_id)
            if row is None:
                row = GoogleCalendarSelection(
                    connection_id=value.id,
                    calendar_id=calendar_id,
                    summary=str(item.get("summary") or calendar_id),
                    selected=False,
                )
                db.add(row)
            row.summary = str(item.get("summary") or calendar_id)[:512]
            row.primary = bool(item.get("primary"))
            row.timezone = item.get("timeZone")
            row.access_role = item.get("accessRole")
            row.state = "READY"
            if row.primary:
                value.connected_account = calendar_id
        for calendar_id, row in existing.items():
            if calendar_id not in seen:
                db.delete(row)
        db.flush()
        return list(
            db.scalars(
                select(GoogleCalendarSelection).where(
                    GoogleCalendarSelection.connection_id == value.id
                )
            )
        )

    async def refresh_calendars(self, db: Session) -> dict[str, Any]:
        value = _connection(db)
        token = await self._access_token(db, value)
        await self._refresh_calendar_list(db, value, token)
        value.state = "ACTIVE"
        value.last_error = None
        db.commit()
        return self.status(db)

    async def select_calendars(self, db: Session, calendar_ids: list[str]) -> dict[str, Any]:
        value = _connection(db)
        token = await self._access_token(db, value)
        await self._refresh_calendar_list(db, value, token)
        rows = list(
            db.scalars(
                select(GoogleCalendarSelection).where(
                    GoogleCalendarSelection.connection_id == value.id
                )
            )
        )
        known = {row.calendar_id for row in rows}
        unknown = set(calendar_ids) - known
        if unknown:
            raise GoogleCalendarError("One or more selected calendars no longer exist")
        requested = set(calendar_ids)
        for row in rows:
            changed = row.selected != (row.calendar_id in requested)
            row.selected = row.calendar_id in requested
            if changed:
                row.sync_token = None
                row.state = "READY"
        db.commit()
        await self.sync(db, access_token=token)
        return self.status(db)

    @staticmethod
    def _event_times(
        item: dict[str, Any], default_timezone: str
    ) -> tuple[datetime, datetime, bool, date | None, date | None, str]:
        start = item.get("start") or {}
        end = item.get("end") or {}
        timezone_name = str(start.get("timeZone") or end.get("timeZone") or default_timezone)
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception:
            timezone_name = UIUC_TIMEZONE
            timezone = ZoneInfo(timezone_name)
        if start.get("date"):
            start_day = date.fromisoformat(start["date"])
            end_day = date.fromisoformat(end.get("date") or start["date"])
            if end_day <= start_day:
                end_day = start_day + timedelta(days=1)
            return (
                datetime.combine(start_day, time.min, timezone).astimezone(UTC),
                datetime.combine(end_day, time.min, timezone).astimezone(UTC),
                True,
                start_day,
                end_day,
                timezone_name,
            )
        start_at = isoparse(str(start.get("dateTime")))
        end_at = isoparse(str(end.get("dateTime")))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone)
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone)
        return as_utc(start_at), as_utc(end_at), False, None, None, timezone_name

    def _upsert_google_event(
        self,
        db: Session,
        calendar: GoogleCalendarSelection,
        item: dict[str, Any],
    ) -> str:
        external_id = str(item.get("id") or "")
        if not external_id:
            return "unchanged"
        existing = db.scalar(
            select(CalendarEvent).where(
                CalendarEvent.source == "google",
                CalendarEvent.calendar_id == calendar.calendar_id,
                CalendarEvent.external_id == external_id,
            )
        )
        if item.get("status") == "cancelled":
            if existing:
                db.delete(existing)
                return "removed"
            return "unchanged"
        try:
            start_at, end_at, all_day, start_day, end_day, timezone_name = self._event_times(
                item, calendar.timezone or UIUC_TIMEZONE
            )
        except (TypeError, ValueError):
            return "unchanged"
        values = {
            "summary": str(item.get("summary") or "Busy")[:512],
            "description": str(item.get("description") or "")[:5000],
            "location": str(item.get("location"))[:512] if item.get("location") else None,
            "start_at": start_at,
            "end_at": end_at,
            "all_day": all_day,
            "start_date": start_day,
            "end_date": end_day,
            "timezone": timezone_name,
            "status": str(item.get("status") or "confirmed")[:24],
            "recurring_event_id": item.get("recurringEventId"),
            "original_start_at": None,
            "transparency": str(item.get("transparency") or "opaque")[:16],
            "source_updated_at": isoparse(item["updated"]) if item.get("updated") else None,
        }
        original = item.get("originalStartTime") or {}
        if original.get("dateTime"):
            values["original_start_at"] = as_utc(isoparse(original["dateTime"]))
        if existing is None:
            existing = CalendarEvent(
                calendar_id=calendar.calendar_id,
                external_id=external_id,
                source="google",
                event_type="busy",
                read_only=True,
                **values,
            )
            db.add(existing)
            return "added"
        changed = any(getattr(existing, key) != value for key, value in values.items())
        for key, value in values.items():
            setattr(existing, key, value)
        existing.read_only = True
        return "updated" if changed else "unchanged"

    async def _sync_calendar(
        self,
        db: Session,
        calendar: GoogleCalendarSelection,
        access_token: str,
        *,
        force_full: bool = False,
    ) -> dict[str, int]:
        incremental = bool(calendar.sync_token and not force_full)
        params: dict[str, Any] = {
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": 2500,
        }
        if incremental:
            params["syncToken"] = calendar.sync_token
        else:
            now = utcnow()
            params["timeMin"] = (now - timedelta(days=366)).isoformat().replace("+00:00", "Z")
            params["timeMax"] = (now + timedelta(days=730)).isoformat().replace("+00:00", "Z")
        path = f"calendars/{quote(calendar.calendar_id, safe='')}/events"
        try:
            items, next_sync = await self._get_pages(path, access_token, params)
        except SyncTokenExpired:
            calendar.sync_token = None
            return await self._sync_calendar(
                db, calendar, access_token, force_full=True
            )
        counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
        seen: set[str] = set()
        for item in items:
            event_id = str(item.get("id") or "")
            if event_id and item.get("status") != "cancelled":
                seen.add(event_id)
            outcome = self._upsert_google_event(db, calendar, item)
            counts[outcome] += 1
        if not incremental:
            for existing in list(
                db.scalars(
                    select(CalendarEvent).where(
                        CalendarEvent.source == "google",
                        CalendarEvent.calendar_id == calendar.calendar_id,
                    )
                )
            ):
                if existing.external_id not in seen:
                    db.delete(existing)
                    counts["removed"] += 1
        if next_sync:
            calendar.sync_token = next_sync
        calendar.last_sync_at = utcnow()
        calendar.state = "ACTIVE"
        return counts

    async def sync(
        self, db: Session, *, access_token: str | None = None
    ) -> dict[str, Any]:
        value = _connection(db)
        if value.state == "DISCONNECTED" or not value.refresh_token_ciphertext:
            return {"synced": 0, "counts": {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}}
        token = access_token or await self._access_token(db, value)
        calendars = list(
            db.scalars(
                select(GoogleCalendarSelection).where(
                    GoogleCalendarSelection.connection_id == value.id,
                    GoogleCalendarSelection.selected.is_(True),
                )
            )
        )
        totals = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
        try:
            for calendar in calendars:
                counts = await self._sync_calendar(db, calendar, token)
                for key, number in counts.items():
                    totals[key] += number
            value.state = "ACTIVE"
            value.last_successful_sync = utcnow()
            value.last_error = None
            db.commit()
        except GoogleCalendarError as error:
            db.rollback()
            value = _connection(db)
            value.state = "DEGRADED" if error.status_code != 401 else "AUTH_REQUIRED"
            value.last_error = str(error)
            db.commit()
            raise
        return {"synced": len(calendars), "counts": totals}

    def disconnect(self, db: Session) -> None:
        value = db.scalar(
            select(GoogleCalendarConnection).order_by(GoogleCalendarConnection.id).limit(1)
        )
        if value:
            db.execute(delete(CalendarEvent).where(CalendarEvent.source == "google"))
            db.execute(
                delete(GoogleCalendarSelection).where(
                    GoogleCalendarSelection.connection_id == value.id
                )
            )
            value.refresh_token_ciphertext = None
            value.connected_account = None
            value.connected_at = None
            value.last_successful_sync = None
            value.last_error = None
            value.oauth_state_hash = None
            value.oauth_state_expires_at = None
            value.state = "DISCONNECTED"
            db.commit()
