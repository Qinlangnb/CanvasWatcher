import asyncio
import json
from time import monotonic

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import broker
from app.auth.broker_challenges import ChallengeStore
from app.auth.models import CanvasOAuthCredential
from app.auth.store import CredentialStore
from app.config import Settings, get_settings
from app.db import Base, CredentialProfile, get_db
from app.services.auth import AuthService
from app.services.canvas_oauth import OAuthError


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(canvas_base_url="https://canvas.example", courses_config=tmp_path / "absent",
                            canvas_access_token="", scheduler_enabled=False)
        store = CredentialStore()
        service = AuthService(settings, store=store)
        profile = CredentialProfile(credential_id="canvas", auth_type="canvas_token",
            probe_url="https://canvas.example/api/v1/users/self/profile",
            metadata_json={"base_url": "https://canvas.example", "account": {"id": "42"}})
        db.add(profile)
        db.commit()
        service.profile = lambda *args: profile
        app = FastAPI()
        app.include_router(broker.router)
        def database():
            yield db
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_settings] = lambda: settings
        monkeypatch.setattr(broker, "AuthService", lambda *args: service)
        monkeypatch.setattr(broker, "credential_store", store)
        monkeypatch.setattr(broker, "broker_challenges", ChallengeStore())
        yield app, store, profile


async def new_claim(client):
    ui = {"Origin": "http://localhost:8080", "X-AW-Broker": "1"}
    response = await client.post("/api/auth/broker/challenges", headers=ui,
                                json={"provider": "canvas", "credential_id": "canvas"})
    value = response.json()
    identity = {key: value[key] for key in ("challenge_id", "instance_id", "provider")}
    claim = await client.post("/api/auth/broker/claim", headers={"X-AW-Broker": "1"},
                             json={**identity, "one_time_token": value["one_time_token"]})
    return value, {**identity, "exchange_token": claim.json()["exchange_token"], "cookies": {"session": "fixture-cookie"}}


@pytest.mark.parametrize("revoked", [False, True])
async def test_independent_oauth_verify_refreshes_before_probe(context, monkeypatch, revoked):
    _app, store, profile = context
    service = broker.AuthService(None)
    store.set_canvas_oauth("canvas", CanvasOAuthCredential("https://canvas.example", "old", "refresh", 0))
    async def exchange(settings, grant, **kwargs):
        assert grant["refresh_token"] == "refresh"
        if revoked:
            raise OAuthError("CANVAS_OAUTH_REVOKED")
        return CanvasOAuthCredential("https://canvas.example", "new", "refresh", monotonic() + 3600), {"id": 42}
    async def probe(client):
        assert store.canvas_slot("canvas", "oauth").token == "new"
        return {"id": 42, "name": "Fixture"}
    monkeypatch.setattr(service.oauth, "exchange", exchange)
    monkeypatch.setattr("app.services.auth.CanvasClient.probe", probe)
    from sqlalchemy.orm import object_session
    assert await service._verify_canvas(object_session(profile), profile, "oauth") is not revoked
    assert store.slot_status("canvas", "oauth")["state"] == ("EXPIRED" if revoked else "VALID")


async def test_wrong_account_has_specific_safe_reason(context, monkeypatch):
    app, store, profile = context
    async def probe(client):
        return {"id": 99, "name": "Different"}
    monkeypatch.setattr(broker.CanvasClient, "probe", probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        value, body = await new_claim(client)
        result = await client.post("/api/auth/broker/complete", headers={"X-AW-Broker": "1"}, json=body)
        assert result.status_code == 409 and result.json()["detail"] == "CANVAS_ACCOUNT_MISMATCH"
        state = broker.broker_challenges.control(value["challenge_id"], value["instance_id"], "canvas", value["control_token"])
        assert state["reason"] == "CANVAS_ACCOUNT_MISMATCH"
        assert store.get_canvas("canvas") is None


@pytest.mark.asyncio
async def test_browser_cannot_claim_and_cookie_never_returned(context, monkeypatch):
    app, store, profile = context
    async def probe(self):
        return {"id": 42, "name": "Fixture"}
    monkeypatch.setattr(broker.CanvasClient, "probe", probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/auth/broker/challenges", json={"provider": "canvas", "credential_id": "canvas"})).status_code == 403
        assert (await client.post("/api/auth/broker/claim", headers={"X-AW-Broker": "1", "Sec-Fetch-Site": "same-origin"}, json={})).status_code == 403
        value, body = await new_claim(client)
        response = await client.post("/api/auth/broker/complete", headers={"X-AW-Broker": "1"}, json=body)
        assert response.json() == {"state": "AUTHENTICATED"}
        assert store.canvas_slot("canvas", "browser_session").cookies == {"session": "fixture-cookie"}
        assert "fixture-cookie" not in response.text + json.dumps(profile.metadata_json)
        assert (await client.post("/api/auth/broker/complete", headers={"X-AW-Broker": "1"}, json=body)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "remove", "wrong_account"])
async def test_late_completion_cannot_resurrect_or_replace_account(context, monkeypatch, change):
    app, store, profile = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(self):
        entered.set()
        await release.wait()
        return {"id": 99 if change == "wrong_account" else 42}
    monkeypatch.setattr(broker.CanvasClient, "probe", probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        value, body = await new_claim(client)
        pending = asyncio.create_task(client.post("/api/auth/broker/complete", headers={"X-AW-Broker": "1"}, json=body))
        await entered.wait()
        if change == "cancel":
            response = await client.post("/api/auth/broker/cancel", headers={"Origin": "http://localhost:8080", "X-AW-Broker": "1"},
                json={key: value[key] for key in ("challenge_id", "instance_id", "provider", "control_token")})
            assert response.json()["state"] == "CANCELLED"
        elif change == "remove":
            store.remove_canvas_slot("canvas", "browser_session", state="DISABLED")
        release.set()
        assert (await pending).status_code == 409
        assert store.canvas_slot("canvas", "browser_session") is None


@pytest.mark.asyncio
async def test_independent_health_never_credits_another_slot(context, monkeypatch):
    app, store, profile = context
    from app.sources.canvas_client import CanvasAPIError, CanvasErrorCode
    service = broker.AuthService(None)
    store.set_canvas("canvas", "https://canvas.example", "pat-fixture")
    store.set_canvas_oauth("canvas", CanvasOAuthCredential("https://canvas.example", "oauth-fixture", "refresh-fixture", 999999999))
    seen = []
    async def probe(self):
        seen.append(self.auth_mode.value)
        if self.auth_mode.value == "pat":
            raise CanvasAPIError(CanvasErrorCode.INVALID_TOKEN, "expired")
        return {"id": 42, "name": "Fixture"}
    monkeypatch.setattr(broker.CanvasClient, "probe", probe)
    from sqlalchemy.orm import object_session
    assert not await service._verify_canvas(object_session(profile), profile, "pat")
    assert seen == ["pat"]
    assert store.get_canvas("canvas").auth_mode == "oauth"
    assert store.slot_status("canvas", "pat")["state"] == "INVALID"
