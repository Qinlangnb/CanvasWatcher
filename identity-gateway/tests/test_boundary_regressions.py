"""Public host and account mutation boundaries stay independent of proxy headers."""

import asyncio

import httpx

from aw_gateway.config import GatewayConfig
from aw_gateway.main import create_app
from aw_gateway.security import csrf_token, issue_token
from aw_gateway.store import IdentityStore

_LEGACY_FIXTURE = (
    "$6$awfixture$hwuRg1k3n0txaihHtr0ZuVtbkTDZ6uqB8dqZc7MrD3Ggb8KfiXQ5xjuBdgSuwvPz"
    "Kl1yGCvB4Ffte03X.DcQg."
)


def test_public_host_and_csrf_boundaries(tmp_path):
    config = GatewayConfig(
        database_path=tmp_path / "identity.db", static_root=tmp_path / "frontend",
        socket_root=tmp_path / "sockets", token_root=tmp_path / "tokens",
        csrf_key_file=tmp_path / "csrf.key",
    )
    key = b"k" * 48
    store = IdentityStore(config.database_path)
    app = create_app(config, csrf_key=key, store=store)
    code = issue_token()
    owner = store.create_owner("owner", "owner@example.com", _LEGACY_FIXTURE, code)
    bootstrap = issue_token()
    assert store.claim_owner(owner, code, bootstrap)
    session = issue_token()
    store.finish_owner_password_change(bootstrap, "new secure owner password", session)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            wrong_host = await client.get("/watcher/health", headers={"Host": "evil.example"})
            assert wrong_host.status_code == 421
            client.cookies.set("aw_session", session, path="/watcher")
            valid_csrf = csrf_token(key, session)
            client.cookies.set("aw_csrf", valid_csrf, path="/watcher")
            no_origin = await client.post("/watcher/logout", data={"csrf": valid_csrf})
            assert no_origin.status_code == 403
            wrong_csrf = await client.post(
                "/watcher/logout", headers={"Origin": "https://watcher.example.com"},
                data={"csrf": "forged"},
            )
            assert wrong_csrf.status_code == 403
            assert store.session(session) is not None

    asyncio.run(run())
