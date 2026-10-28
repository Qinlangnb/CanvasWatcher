import asyncio

import httpx

from aw_gateway.config import GatewayConfig
from aw_gateway.main import create_app
from aw_gateway.store import IdentityStore


def test_preview_login_is_static_and_cannot_reach_tenant_api(tmp_path):
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text("<html><head></head><body>app</body></html>")
    config = GatewayConfig(
        database_path=tmp_path / "identity.db", static_root=root,
        socket_root=tmp_path / "sockets", token_root=tmp_path / "tokens",
        csrf_key_file=tmp_path / "csrf.key", public_origin="https://watcher.example.com",
    )
    store = IdentityStore(config.database_path)
    app = create_app(config, csrf_key=b"k" * 48, store=store)
    user_id, slot = store.create_preview_account("preview", "preview@demo.invalid", "Preview1234!")
    assert 2 <= slot <= 5 and store.is_preview(user_id)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            login = await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "preview", "password": "Preview1234!"},
            )
            assert login.status_code == 303
            page = await client.get("/watcher/")
            assert page.status_code == 200
            assert '<meta name="aw-preview" content="true">' in page.text
            assert page.headers["cache-control"] == "no-store"
            for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(method, "/watcher/api/courses")
                assert response.status_code == 403
                assert response.headers["cache-control"] == "no-store"
            account = await client.get("/watcher/account")
            assert account.status_code == 200

    asyncio.run(run())
