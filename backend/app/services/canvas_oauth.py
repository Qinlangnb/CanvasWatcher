"""Institution-scoped OAuth; access/refresh tokens live in memory only."""

import asyncio
import secrets
import threading
from time import monotonic
from urllib.parse import urlencode, urlsplit

import httpx

from app.auth.models import CanvasOAuthCredential
from app.auth.store import credential_store
from app.sources.canvas_client import CanvasAPIError, CanvasClient, CanvasErrorCode


class OAuthError(ValueError):
    pass


def configured(settings) -> bool:
    try:
        base, callback = urlsplit(settings.canvas_base_url), urlsplit(settings.canvas_oauth_redirect_uri)
        return bool(settings.canvas_oauth_client_id and settings.canvas_oauth_client_secret.get_secret_value()
            and base.scheme == "https" and base.hostname and not base.username and not base.password
            and callback.scheme == "http" and callback.hostname in {"localhost", "127.0.0.1"}
            and callback.port and callback.path == "/api/auth/canvas/oauth/callback"
            and not callback.query and not callback.fragment and not callback.username)
    except ValueError:
        return False


class CanvasOAuth:
    def __init__(self, store=credential_store):
        self.store = store
        self.states = {}
        self.lock = threading.RLock()
        self.refresh_locks = {}

    def begin(self, settings, credential_id: str) -> str:
        if not configured(settings):
            raise OAuthError("CANVAS_OAUTH_NOT_CONFIGURED")
        with self.lock:
            self.states = {key: val for key, val in self.states.items() if val[0] > monotonic() and val[1] != credential_id}
            if len(self.states) >= 32:
                raise OAuthError("CANVAS_OAUTH_BUSY")
            state = secrets.token_urlsafe(32)
            self.states[state] = (monotonic() + 240, credential_id,
                self.store.generation(credential_id, "oauth"), settings.canvas_base_url.rstrip("/"), settings.canvas_oauth_redirect_uri)
        return settings.canvas_base_url.rstrip("/") + "/login/oauth2/auth?" + urlencode({
            "client_id": settings.canvas_oauth_client_id, "redirect_uri": settings.canvas_oauth_redirect_uri,
            "response_type": "code", "state": state})

    def consume(self, settings, state: str):
        with self.lock:
            attempt = self.states.pop(state, None)
        if (not attempt or attempt[0] <= monotonic() or attempt[3] != settings.canvas_base_url.rstrip("/")
                or attempt[4] != settings.canvas_oauth_redirect_uri or not configured(settings)):
            raise OAuthError("CANVAS_OAUTH_STATE_INVALID")
        return attempt

    async def exchange(self, settings, grant: dict, *, transport=None):
        try:
            async with httpx.AsyncClient(transport=transport, timeout=25, trust_env=False, follow_redirects=False) as client:
                response = await client.post(settings.canvas_base_url.rstrip("/") + "/login/oauth2/token", data={
                    "client_id": settings.canvas_oauth_client_id,
                    "client_secret": settings.canvas_oauth_client_secret.get_secret_value(),
                    "redirect_uri": settings.canvas_oauth_redirect_uri, **grant})
            body = response.json()
            if response.status_code == 400 and body.get("error") == "invalid_grant":
                raise OAuthError("CANVAS_OAUTH_REVOKED")
            if response.status_code != 200 or not isinstance(body, dict):
                raise OAuthError("CANVAS_OAUTH_FAILED")
            token, ttl = body.get("access_token"), body.get("expires_in")
            if not isinstance(token, str) or not token or not isinstance(ttl, (int, float)) or not 0 < ttl <= 86400 * 365:
                raise OAuthError("CANVAS_OAUTH_FAILED")
            refresh = body.get("refresh_token") or grant.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise OAuthError("CANVAS_OAUTH_FAILED")
            credential = CanvasOAuthCredential(settings.canvas_base_url.rstrip("/"), token, refresh, monotonic() + ttl)
            account = await CanvasClient(credential.base_url, credential=credential, transport=transport).probe()
            if not account.get("id"):
                raise OAuthError("CANVAS_OAUTH_FAILED")
            return credential, account
        except OAuthError:
            raise
        except Exception:
            raise OAuthError("CANVAS_OAUTH_FAILED") from None

    async def refresh(self, settings, credential_id, account_id, *, transport=None):
        current = self.store.canvas_slot(credential_id, "oauth")
        if not current or current.expires_clock > monotonic() + 30:
            return
        with self.lock:
            lock = self.refresh_locks.setdefault(credential_id, threading.Lock())
        while not lock.acquire(blocking=False):
            await asyncio.sleep(.02)
        try:
            current = self.store.canvas_slot(credential_id, "oauth")
            if not current or current.expires_clock > monotonic() + 30:
                return
            generation = self.store.generation(credential_id, "oauth")
            try:
                replacement, account = await self.exchange(settings, {"grant_type": "refresh_token",
                    "refresh_token": current.refresh_token}, transport=transport)
            except OAuthError as exc:
                if str(exc) == "CANVAS_OAUTH_REVOKED":
                    self.store.remove_canvas_slot(credential_id, "oauth", generation, state="EXPIRED")
                    return
                raise CanvasAPIError(CanvasErrorCode.NETWORK_ERROR, "Canvas OAuth refresh unavailable; retry later") from None
            with self.store.atomic():
                if self.store.generation(credential_id, "oauth") != generation:
                    raise CanvasAPIError(CanvasErrorCode.INVALID_RESPONSE, "Credential changed; retry")
                if str(account.get("id")) != str(account_id) or replacement.base_url != current.base_url:
                    self.store.remove_canvas_slot(credential_id, "oauth", generation, state="REVOKED")
                    raise CanvasAPIError(CanvasErrorCode.PERMISSION_DENIED, "Canvas account identity mismatch")
                self.store.set_canvas_oauth(credential_id, replacement)
                self.store.bind_identity(credential_id, "oauth", str(account["id"]))
        finally:
            lock.release()


canvas_oauth = CanvasOAuth()
