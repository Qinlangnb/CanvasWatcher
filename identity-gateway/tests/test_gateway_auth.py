import asyncio
import re

import httpx
from passlib.hash import sha512_crypt

from aw_gateway.config import GatewayConfig
from aw_gateway.main import create_app
from aw_gateway.security import issue_token
from aw_gateway.store import IdentityStore


def _setup(tmp_path):
    config = GatewayConfig(
        database_path=tmp_path / "identity.db",
        static_root=tmp_path / "frontend",
        socket_root=tmp_path / "sockets",
        token_root=tmp_path / "tokens",
        csrf_key_file=tmp_path / "csrf.key",
        public_origin="https://watcher.example.com",
    )
    store = IdentityStore(config.database_path)
    code = issue_token()
    app = create_app(config, csrf_key=b"k" * 48, store=store)
    owner_id = store.create_owner(
        "example_admin", "admin@example.com", sha512_crypt.hash("old local password"), code
    )
    return app, store, owner_id, code


def test_owner_bootstrap_requires_both_secrets_and_csrf(tmp_path):
    app, store, owner_id, code = _setup(tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            forbidden = await client.post(
                "/watcher/login", data={"username": "example_admin", "password": "old local password"}
            )
            assert forbidden.status_code == 403
            missing_code = await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "old local password"},
            )
            assert missing_code.status_code == 403
            claimed = await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "old local password", "bootstrap_code": code},
            )
            assert claimed.status_code == 303
            assert claimed.headers["location"] == "/watcher/change-password"
            assert "HttpOnly" in claimed.headers["set-cookie"]
            replay = await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "old local password", "bootstrap_code": code},
            )
            assert replay.status_code == 409
            blocked_api = await client.get("/watcher/api/courses")
            assert blocked_api.status_code == 403
            change_page = await client.get("/watcher/change-password")
            csrf = re.search(r"name='csrf' value='([^']+)'", change_page.text).group(1)
            wrong_csrf = await client.post(
                "/watcher/change-password", headers={"Origin": "https://watcher.example.com"},
                data={"csrf": "wrong", "new_password": "new very long password", "confirm_password": "new very long password"},
            )
            assert wrong_csrf.status_code == 403
            short_password = await client.post(
                "/watcher/change-password", headers={"Origin": "https://watcher.example.com"},
                data={"csrf": csrf, "new_password": "short", "confirm_password": "short"},
            )
            assert short_password.status_code == 400
            assert store.get_user(owner_id)["must_change_password"] == 1
            assert store.session(client.cookies.get("aw_session"))["session_kind"] == "bootstrap"
            changed = await client.post(
                "/watcher/change-password", headers={"Origin": "https://watcher.example.com"},
                data={"csrf": csrf, "new_password": "new very long password", "confirm_password": "new very long password"},
            )
            assert changed.status_code == 303
            assert store.get_user(owner_id)["legacy_hash"] is None
            assert store.session(client.cookies.get("aw_session"))["session_kind"] == "normal"
            now_api = await client.get("/watcher/api/courses")
            assert now_api.status_code == 503  # slot not provisioned in this isolated test

    asyncio.run(run())


def test_invitation_registration_is_single_use(tmp_path):
    app, store, owner_id, code = _setup(tmp_path)
    bootstrap = issue_token()
    assert store.claim_owner(owner_id, code, bootstrap)
    store.finish_owner_password_change(bootstrap, "admin secure password", issue_token())

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            login = await client.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "admin secure password"},
            )
            assert login.status_code == 303
            page = await client.get("/watcher/admin")
            csrf = re.search(r"name='csrf' value='([^']+)'", page.text).group(1)
            created = await client.post(
                "/watcher/admin/invite", headers={"Origin": "https://watcher.example.com"},
                data={"csrf": csrf, "email": "student@example.com"},
            )
            assert created.status_code == 200
            invite = re.search(r"<code>([^<]+)</code>", created.text).group(1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as guest:
            data = {"invite": invite, "email": "student@example.com", "username": "student", "password": "student secure password"}
            registered = await guest.post(
                "/watcher/register", headers={"Origin": "https://watcher.example.com"}, data=data,
            )
            assert registered.status_code == 202
            repeated = await guest.post(
                "/watcher/register", headers={"Origin": "https://watcher.example.com"}, data=data,
            )
            assert repeated.status_code == 400
            user = store.find_user("student")
            assert user["status"] == "pending"
            assert user["slot"] == 2

        store.activate_user(user["id"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as student:
            signed_in = await student.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "student", "password": "student secure password"},
            )
            assert signed_in.status_code == 303
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
            ) as admin:
                await admin.post(
                    "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                    data={"username": "example_admin", "password": "admin secure password"},
                )
                admin_page = await admin.get("/watcher/admin")
                csrf = re.search(r"name='csrf' value='([^']+)'", admin_page.text).group(1)
                disabled = await admin.post(
                    "/watcher/admin/disable", headers={"Origin": "https://watcher.example.com"},
                    data={"csrf": csrf, "user_id": user["id"]},
                )
                assert disabled.status_code == 303
            assert store.find_user("student")["status"] == "disabled"
            assert store.session(student.cookies.get("aw_session")) is None
            assert (await student.get("/watcher/api/courses")).status_code == 401

    asyncio.run(run())


def test_auth_pages_let_browsers_send_their_origin(tmp_path):
    # Under "no-referrer" browsers send "Origin: null" on form POSTs, so the same-origin
    # check would reject every real login. "same-origin" still sends nothing cross-site.
    app, _, _, _ = _setup(tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as client:
            page = await client.get("/watcher/login")
            assert page.headers["referrer-policy"] == "same-origin"
            health = await client.get("/watcher/health")
            assert health.headers["referrer-policy"] == "same-origin"

    asyncio.run(run())
