import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from academic_watcher_auth_broker.browser import Profiles, failure_category
from academic_watcher_auth_broker.config import Config, loopback_origin, provider_origin
from academic_watcher_auth_broker.server import create_app

from app.auth.broker_challenges import ChallengeError, ChallengeStore
from app.auth.store import CredentialStore


def ticket(store, provider="canvas"):
    return store.create(provider, {"base_url": "https://canvas.example.edu", "credential_id": "canvas",
                                    "operation": "login"})


def ids(value):
    return value["challenge_id"], value["instance_id"], value["provider"]


@pytest.mark.parametrize("message, expected", [
    ("Executable doesn't exist at C:/private/token-secret", "browser_missing"),
    ("ProcessSingleton cookie-secret", "profile_busy"),
    ("Access is denied private-secret", "permission_denied"),
    ("net::ERR_CERT_AUTHORITY_INVALID https://private/?token=secret", "certificate_failed"),
    ("AUTH_PROFILE_PATH_INVALID", "profile_path_rejected"),
    ("arbitrary cookie-secret", "unknown"),
])
def test_browser_failure_categories_never_echo_error_text(message, expected):
    assert failure_category(RuntimeError(message)) == expected


async def test_explicit_https_frontend_and_private_network_preflight(tmp_path):
    config = Config(frontend_origins=("https://aw.example.com",), profiles=tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config)), base_url="http://127.0.0.1:8765") as client:
        response = await client.options("/login", headers={"Origin": "https://aw.example.com",
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-AW-Broker,Content-Type",
            "Access-Control-Request-Private-Network": "true"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://aw.example.com"
        assert response.headers["access-control-allow-private-network"] == "true"
        assert (await client.get("/health", headers={"Origin": "https://other.example"})).status_code == 403
    for origin in ("*", "https://*.example.com", "https://aw.example.com/path", "https://user:pass@aw.example.com"):
        with pytest.raises(ValueError):
            Config(frontend_origins=(origin,), profiles=tmp_path)


def test_capabilities_expire_reject_replay_and_separate_control():
    now = [10]
    store = ChallengeStore(clock=lambda: now[0])
    value = ticket(store)
    with pytest.raises(ChallengeError):
        store.claim(*ids(value), "wrong")
    with pytest.raises(ChallengeError):
        store.claim(value["challenge_id"], "other-instance", "canvas", value["one_time_token"])
    with pytest.raises(ChallengeError):
        store.claim(value["challenge_id"], value["instance_id"], "gradescope", value["one_time_token"])
    claimed = store.claim(*ids(value), value["one_time_token"])
    with pytest.raises(ChallengeError):
        store.claim(*ids(value), value["one_time_token"])
    with pytest.raises(ChallengeError):
        store.begin_completion(*ids(value), value["one_time_token"])
    row = store.begin_completion(*ids(value), claimed["exchange_token"])
    with pytest.raises(ChallengeError):
        store.begin_completion(*ids(value), claimed["exchange_token"])
    published = []
    store.finish(row, lambda: published.append(True))
    assert published == [True]
    assert store.control(*ids(value), value["control_token"])["state"] == "AUTHENTICATED"
    assert value["one_time_token"] not in repr(store.rows)
    value = ticket(store)
    now[0] += 241
    with pytest.raises(ChallengeError, match="EXPIRED"):
        store.claim(*ids(value), value["one_time_token"])


@pytest.mark.parametrize("action", ["cancel", "expire", "replace"])
def test_cancel_or_expiry_during_probe_never_publishes(action):
    now = [0]
    store = ChallengeStore(clock=lambda: now[0])
    value = ticket(store)
    claimed = store.claim(*ids(value), value["one_time_token"])
    row = store.begin_completion(*ids(value), claimed["exchange_token"])
    if action == "cancel":
        store.control(*ids(value), value["control_token"], cancel=True)
    elif action == "expire":
        now[0] = 241
    else:
        ticket(store)
    published = []
    with pytest.raises(ChallengeError):
        store.finish(row, lambda: published.append(True))
    assert published == []


def test_concurrent_claim_is_single_use():
    store = ChallengeStore()
    value = ticket(store)
    def claim(_):
        try:
            store.claim(*ids(value), value["one_time_token"])
            return True
        except ChallengeError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(16))) == 1


def test_removal_even_of_empty_slot_invalidates_pending_completion():
    store = CredentialStore()
    generation = store.generation("canvas", "browser_session")
    store.remove_canvas_slot("canvas", "browser_session")
    assert store.generation("canvas", "browser_session") > generation


def test_structured_logs_redact_capabilities_and_oauth():
    from app.logging import redact_secrets
    result = redact_secrets(None, "info", {"one_time_token": "secret1", "nested": {
        "cookies": {"session": "secret2"}, "refresh_token": "secret3", "exchange_token": "secret4"}})
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("url", ["http://0.0.0.0:8000", "http://evil.test:8000", "http://localhost:8000/path",
                                  "http://user@localhost:8000", "http://localhost:8000?token=secret"])
def test_backend_origin_cannot_be_redirected(url):
    with pytest.raises(ValueError):
        loopback_origin(url)


@pytest.mark.parametrize("url", ["http://canvas.test", "https://user@canvas.test", "https://canvas.test/path",
                                  "https://canvas.test?secret=1", "https://canvas.test\\evil"])
def test_provider_origin_is_not_an_arbitrary_launch_url(url):
    with pytest.raises(ValueError):
        provider_origin(url)


def test_profile_namespace_and_clear_are_bounded(tmp_path):
    profiles = Profiles(tmp_path / "appdata", "http://127.0.0.1:8000")
    target = {"provider": "canvas", "base_url": "https://canvas.test", "credential_id": "../../outside"}
    path = profiles.path(target)
    path.mkdir(parents=True)
    other = tmp_path / "preserved"
    other.mkdir()
    assert path.parent == profiles.namespace()
    profiles.clear(target)
    assert not path.exists() and other.exists()
    assert Profiles(tmp_path / "appdata", "http://127.0.0.1:9000").path(target) != path
    path.mkdir(parents=True)
    other_instance = Profiles(tmp_path / "appdata", "http://127.0.0.1:9000").path(target)
    other_instance.mkdir(parents=True)
    profiles.clear_all()
    assert not path.exists() and other_instance.exists()


@pytest.mark.asyncio
async def test_broker_origin_host_and_validation_redaction(tmp_path):
    app = create_app(Config(profiles=tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        assert (await client.get("/health")).status_code == 403
        assert (await client.get("/health", headers={"Origin": "https://evil.test"})).status_code == 403
        headers = {"Origin": "http://localhost:8080", "X-AW-Broker": "1"}
        assert (await client.get("/health", headers={**headers, "Host": "evil.test:8765"})).status_code == 403
        assert (await client.get("/health", headers=headers)).json()["protocol_version"] == 1
        response = await client.post("/login", headers=headers, json={"one_time_token": "fixture-secret"})
        assert response.status_code == 422 and "fixture-secret" not in response.text
        assert "*" != response.headers.get("access-control-allow-origin")


@pytest.mark.asyncio
async def test_provider_cookie_goes_only_to_pinned_backend(tmp_path, caplog):
    store = ChallengeStore()
    value = ticket(store)
    received = []

    async def backend(request):
        assert str(request.url).startswith("http://127.0.0.1:8000/api/auth/broker/")
        body = json.loads(request.content)
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=store.claim(body["challenge_id"], body["instance_id"], body["provider"], body["one_time_token"]))
        received.append(body)
        return httpx.Response(200, json={"state": "AUTHENTICATED"})

    class FakeBrowser:
        async def run(self, target, cancelled):
            return {"canvas_session": "fixture-cookie-secret"}

    app = create_app(Config(profiles=tmp_path), login=FakeBrowser(), transport=httpx.MockTransport(backend))
    headers = {"Origin": "http://localhost:8080", "X-AW-Broker": "1"}
    body = {key: value[key] for key in ("challenge_id", "instance_id", "provider", "one_time_token")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        response = await client.post("/login", headers=headers, json=body)
        assert response.json() == {"state": "LOGIN_OPENED"}
        await app.state.jobs[value["challenge_id"]].task
        assert received[0]["cookies"] == {"canvas_session": "fixture-cookie-secret"}
        assert "fixture-cookie-secret" not in response.text + caplog.text
        assert (await client.post("/login", headers=headers, json=body)).status_code == 403


@pytest.mark.asyncio
async def test_concurrent_browser_start_and_cancel(tmp_path):
    claimed = asyncio.Event()
    release = asyncio.Event()
    async def backend(request):
        if request.url.path.endswith("claim"):
            claimed.set()
            await release.wait()
            return httpx.Response(200, json={"protocol_version": 1, "provider": "canvas",
                "exchange_token": "fixture", "expires_in": 240, "operation": "login"})
        return httpx.Response(200, json={"state": "FAILED"})
    class FakeBrowser:
        async def run(self, target, cancelled):
            await asyncio.Event().wait()
    app = create_app(Config(profiles=tmp_path), login=FakeBrowser(), transport=httpx.MockTransport(backend))
    value = ticket(ChallengeStore())
    body = {key: value[key] for key in ("challenge_id", "instance_id", "provider", "one_time_token")}
    headers = {"Origin": "http://localhost:8080", "X-AW-Broker": "1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        first = asyncio.create_task(client.post("/login", headers=headers, json=body))
        await claimed.wait()
        assert (await client.post("/login", headers=headers, json=body)).status_code == 409
        release.set()
        assert (await first).status_code == 200
        await asyncio.sleep(0)
        assert (await client.post("/cancel", headers=headers, json=body)).status_code == 200
        await app.state.jobs[value["challenge_id"]].task
        assert app.state.jobs[value["challenge_id"]].state == "CANCELLED"


@pytest.mark.asyncio
async def test_login_failure_logs_stage_without_secret_exception_text(tmp_path, caplog):
    async def backend(request):
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json={"protocol_version": 1, "provider": "canvas",
                "exchange_token": "fixture-exchange-secret", "expires_in": 240, "operation": "login"})
        assert json.loads(request.content)["reason"] == "AUTH_LOGIN_FAILED"
        return httpx.Response(200, json={"state": "FAILED"})

    class BrokenBrowser:
        async def run(self, target, cancelled):
            raise RuntimeError("https://example.test/?token=fixture-login-secret")

    app = create_app(Config(profiles=tmp_path), login=BrokenBrowser(), transport=httpx.MockTransport(backend))
    value = ticket(ChallengeStore())
    body = {key: value[key] for key in ("challenge_id", "instance_id", "provider", "one_time_token")}
    headers = {"Origin": "http://localhost:8080", "X-AW-Broker": "1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        assert (await client.post("/login", headers=headers, json=body)).status_code == 200
        job = app.state.jobs[value["challenge_id"]]
        await job.task
        assert job.state == "FAILED"
    assert "phase=browser_login error_type=RuntimeError" in caplog.text
    assert "fixture-login-secret" not in caplog.text
    assert "fixture-exchange-secret" not in caplog.text
    assert value["one_time_token"] not in caplog.text
