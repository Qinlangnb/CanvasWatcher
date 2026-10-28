import asyncio
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi.responses import HTMLResponse, Response
from passlib.hash import sha512_crypt

from aw_gateway.config import GatewayConfig
from aw_gateway.main import create_app
from aw_gateway.security import issue_token
from aw_gateway.store import IdentityStore


def test_canvas_callback_routes_without_cookie_and_rejects_replay(tmp_path, monkeypatch):
    config = GatewayConfig(
        database_path=tmp_path / "identity.db", static_root=tmp_path / "frontend",
        socket_root=tmp_path / "sockets", token_root=tmp_path / "tokens",
        csrf_key_file=tmp_path / "csrf.key",
    )
    store = IdentityStore(config.database_path)
    app = create_app(config, csrf_key=b"k" * 48, store=store)
    code = issue_token()
    owner = store.create_owner(
        "example_admin", "admin@example.com", sha512_crypt.hash("legacy password"), code
    )
    bootstrap = issue_token()
    assert store.claim_owner(owner, code, bootstrap)
    store.finish_owner_password_change(bootstrap, "new owner password", issue_token())
    called = []
    callback_uri = "https://watcher.example.com/watcher/api/auth/canvas/oauth/callback"
    auth_url = "https://canvas.example/login/oauth2/auth?" + urlencode(
        {"client_id": "example", "state": "instance-private-state", "redirect_uri": callback_uri}
    )

    async def fake_tenant(request, *, config, slot, query=None):
        called.append((slot, query))
        if query is None:
            return Response(content=json.dumps({"authorization_url": auth_url}), media_type="application/json")
        assert parse_qs(query)["state"] == ["instance-private-state"]
        return HTMLResponse("Canvas connected")

    monkeypatch.setattr("aw_gateway.main.buffered_tenant_request", fake_tenant)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            login = await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "new owner password"},
            )
            assert login.status_code == 303
            csrf = client.cookies.get("aw_csrf")
            started = await client.post(
                "/watcher/api/auth/canvas/oauth/start",
                headers={"Origin": "https://watcher.example.com", "X-AW-CSRF": csrf},
                json={"credential_id": "canvas"},
            )
            assert started.status_code == 200
            outer = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
            assert outer != "instance-private-state"
            assert called == [(1, None)]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as callback_client:
            completed = await callback_client.get(
                "/watcher/api/auth/canvas/oauth/callback", params={"state": outer, "code": "fixture"},
            )
            assert completed.status_code == 200
            assert completed.text == "Canvas connected"
            assert called[1][0] == 1
            repeated = await callback_client.get(
                "/watcher/api/auth/canvas/oauth/callback", params={"state": outer, "code": "fixture"},
            )
            assert repeated.status_code == 400

    asyncio.run(run())


def test_oauth_start_rejects_wrong_redirect(tmp_path, monkeypatch):
    config = GatewayConfig(
        database_path=tmp_path / "identity.db", static_root=tmp_path / "frontend",
        socket_root=tmp_path / "sockets", token_root=tmp_path / "tokens",
        csrf_key_file=tmp_path / "csrf.key",
    )
    store = IdentityStore(config.database_path)
    app = create_app(config, csrf_key=b"k" * 48, store=store)
    code = issue_token()
    owner = store.create_owner(
        "example_admin", "admin@example.com", sha512_crypt.hash("legacy password"), code
    )
    bootstrap = issue_token()
    assert store.claim_owner(owner, code, bootstrap)
    store.finish_owner_password_change(bootstrap, "new owner password", issue_token())

    async def fake_tenant(request, *, config, slot, query=None):
        url = "https://canvas.example/login/oauth2/auth?" + urlencode(
            {"state": "inner", "redirect_uri": "https://evil.example/callback"}
        )
        return Response(content=json.dumps({"authorization_url": url}), media_type="application/json")

    monkeypatch.setattr("aw_gateway.main.buffered_tenant_request", fake_tenant)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "new owner password"},
            )
            response = await client.post(
                "/watcher/api/auth/canvas/oauth/start",
                headers={"Origin": "https://watcher.example.com", "X-AW-CSRF": client.cookies.get("aw_csrf")},
                json={"credential_id": "canvas"},
            )
            assert response.status_code == 503

    asyncio.run(run())
