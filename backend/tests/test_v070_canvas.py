import asyncio
from time import monotonic
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.auth.models import CanvasCredential, CanvasOAuthCredential
from app.auth.store import CredentialStore
from app.config import Settings
from app.services.canvas_oauth import CanvasOAuth, OAuthError, configured
from app.services.canvas_web_session import session_url
from app.sources.canvas_client import CanvasAPIError
from app.sources.canvas_routing import RoutedCanvasClient


def populate(modes):
    store = CredentialStore()
    if "pat" in modes:
        store.set_canvas("canvas", "https://canvas.example", "pat-fixture")
    if "browser_session" in modes:
        store.set_canvas_session("canvas", "https://canvas.example", {"session": "browser-fixture"})
    if "oauth" in modes:
        store.set_canvas_oauth("canvas", CanvasOAuthCredential("https://canvas.example", "oauth-fixture", "refresh-fixture", monotonic() + 3600))
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", [("pat",), ("oauth",), ("browser_session",), ("pat", "browser_session"),
    ("oauth", "browser_session"), ("oauth", "pat"), ("oauth", "pat", "browser_session")])
async def test_all_credential_combinations_and_bounded_fallback(modes):
    store = populate(modes)
    ordered = [mode for mode in ("oauth", "pat", "browser_session") if mode in modes]
    seen = []
    async def handle(request):
        token = request.headers.get("authorization", "")
        mode = "oauth" if "oauth-fixture" in token else "pat" if token else "browser_session"
        assert not (request.headers.get("cookie") and token)
        seen.append((mode, request.url.path))
        return httpx.Response(200, json={"id": 42}) if mode == ordered[-1] else httpx.Response(401, json={})
    client = RoutedCanvasClient(store, "canvas", 42, transport=httpx.MockTransport(handle))
    assert await client.get_json("/api/v1/courses") == {"id": 42}
    assert [mode for mode, path in seen if path.endswith("courses")] == ordered
    assert store.get_canvas("canvas").auth_mode == ordered[-1]


@pytest.mark.asyncio
async def test_three_expired_credentials_stop_once_each():
    store = populate(("oauth", "pat", "browser_session"))
    seen = []
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(401, json={})
    client = RoutedCanvasClient(store, "canvas", 42, transport=httpx.MockTransport(handle))
    with pytest.raises(CanvasAPIError):
        await client.get_json("/api/v1/courses")
    assert len(seen) == 6
    assert store.get_canvas("canvas") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_even_profile_permission_failure_does_not_switch(status):
    store = populate(("oauth", "pat", "browser_session"))
    seen = []
    def handle(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(status, json={}, headers={"retry-after": "0"})
    async def no_sleep(_): pass
    client = RoutedCanvasClient(store, "canvas", 42, transport=httpx.MockTransport(handle), sleep=no_sleep)
    with pytest.raises(CanvasAPIError):
        await client.probe()
    assert all(value == "Bearer oauth-fixture" for value in seen)
    assert store.get_canvas("canvas").auth_mode == "oauth"


def oauth_settings():
    return Settings(canvas_base_url="https://canvas.example", canvas_oauth_client_id="client-fixture",
                    canvas_oauth_client_secret="secret-fixture")


def test_oauth_requires_configuration_and_state_is_scoped_single_use():
    settings = oauth_settings()
    flow = CanvasOAuth(CredentialStore())
    assert not configured(Settings())
    with pytest.raises(OAuthError, match="NOT_CONFIGURED"):
        flow.begin(Settings(), "canvas")
    url = flow.begin(settings, "canvas")
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [settings.canvas_oauth_redirect_uri]
    assert "secret-fixture" not in url
    with pytest.raises(OAuthError):
        flow.consume(settings, "wrong-state")
    assert flow.consume(settings, query["state"][0])[1] == "canvas"
    with pytest.raises(OAuthError):
        flow.consume(settings, query["state"][0])


@pytest.mark.asyncio
async def test_oauth_exchange_probe_is_isolated_and_refresh_single_flight():
    settings = oauth_settings()
    store = populate(("pat", "browser_session"))
    flow = CanvasOAuth(store)
    calls = []
    async def handle(request):
        calls.append(request)
        if request.url.path.endswith("/token"):
            await asyncio.sleep(.01)
            return httpx.Response(200, json={"access_token": "new-oauth", "refresh_token": "new-refresh", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new-oauth"
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"id": 42, "name": "Fixture"})
    transport = httpx.MockTransport(handle)
    credential, account = await flow.exchange(settings, {"grant_type": "authorization_code", "code": "fixture-code"}, transport=transport)
    assert account["id"] == 42 and credential.auth_mode == "oauth"
    credential.expires_clock = 0
    store.set_canvas_oauth("canvas", credential)
    await asyncio.gather(*(flow.refresh(settings, "canvas", 42, transport=transport) for _ in range(8)))
    assert len([row for row in calls if row.url.path.endswith("/token")]) == 2
    assert store.get_canvas("canvas").token == "new-oauth"
    assert "new-refresh" not in repr(store.get_canvas("canvas"))
    store.clear_all()
    assert store.get_canvas("canvas") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,removed", [(400, {"error": "invalid_grant"}, True), (503, {}, False), (429, {}, False)])
async def test_refresh_revocation_falls_back_but_transients_do_not(status, body, removed):
    settings = oauth_settings()
    store = populate(("oauth", "pat"))
    store.canvas_slot("canvas", "oauth").expires_clock = 0
    flow = CanvasOAuth(store)
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    if removed:
        await flow.refresh(settings, "canvas", 42, transport=transport)
    else:
        with pytest.raises(CanvasAPIError):
            await flow.refresh(settings, "canvas", 42, transport=transport)
    assert (store.canvas_slot("canvas", "oauth") is None) == removed
    assert store.canvas_slot("canvas", "pat") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,url", [(200, "https://canvas.example/session?token=fixture"),
    (200, "https://evil.example/session?token=fixture"), (403, None), (404, None), (401, None)])
async def test_web_bridge_is_optional_and_never_invalidates_api_token(status, url):
    credential = CanvasCredential("https://canvas.example", "fixture-pat")
    def handle(request):
        assert request.url.path == "/login/session_token"
        assert request.headers["authorization"] == "Bearer fixture-pat"
        assert "cookie" not in request.headers
        return httpx.Response(status, json={"session_url": url})
    result = await session_url(credential, transport=httpx.MockTransport(handle))
    assert result == (url if status == 200 and url.startswith("https://canvas.example/") else None)
    assert credential.token == "fixture-pat"
