import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import broker, provider_sources
from app.auth.broker_challenges import ChallengeStore
from app.auth.provider_sessions import ProviderSession, ProviderSessions
from app.config import Settings, get_settings
from app.db import Base, CredentialProfile, get_db
from app.sources.provider_web import ProviderError, ProviderWeb, authenticated_page, provider_base


@pytest.mark.parametrize("provider,base", [("gradescope", "https://www.gradescope.com"),
    ("prairielearn", "https://us.prairielearn.com/pl")])
async def test_provider_broker_cookie_isolation_and_removal_race(tmp_path, monkeypatch, provider, base):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    store = ProviderSessions()
    monkeypatch.setattr(broker, "provider_sessions", store)
    monkeypatch.setattr(provider_sources, "provider_sessions", store)
    monkeypatch.setattr(broker, "broker_challenges", ChallengeStore())
    settings = Settings(canvas_base_url="", courses_config=tmp_path / "absent", scheduler_enabled=False)
    ui, host = {"Origin": "http://localhost:8080", "X-AW-Broker": "1"}, {"X-AW-Broker": "1"}
    with Session(engine) as db:
        app = FastAPI()
        app.include_router(broker.router)
        app.include_router(provider_sources.router)
        def database():
            yield db
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_settings] = lambda: settings
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/provider-sources", headers=ui, json={"provider": provider, "base_url": base})
            assert response.status_code == 200
            key = response.json()["credential_id"]
            async def probe(self):
                assert self.session.cookies == {"session": "test-secret"}
                assert self.base == base
                return "verified-test-page"
            monkeypatch.setattr(ProviderWeb, "probe", probe)
            for removed in (False, True):
                ticket = (await client.post("/api/auth/broker/challenges", headers=ui,
                    json={"provider": provider, "credential_id": key})).json()
                identity = {name: ticket[name] for name in ("provider", "instance_id", "challenge_id")}
                claimed = (await client.post("/api/auth/broker/claim", headers=host,
                    json={**identity, "one_time_token": ticket["one_time_token"]})).json()
                if removed:
                    store.remove(key)
                response = await client.post("/api/auth/broker/complete", headers=host,
                    json={**identity, "exchange_token": claimed["exchange_token"], "cookies": {"session": "test-secret"}})
                assert response.status_code == (409 if removed else 200)
                assert (store.get(key) is None) is removed
                profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == key))
                assert "test-secret" not in response.text + json.dumps(profile.metadata_json)


async def test_read_only_transport_does_not_follow_redirects_or_leak_cookie():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.example/"})
    session = ProviderSession("gradescope", "https://www.gradescope.com", {"session": "secret-fixture"})
    client = ProviderWeb(session, httpx.MockTransport(handle))
    with pytest.raises(ProviderError, match="AUTH_REQUIRED"):
        await client.get("/")
    assert len(seen) == 1 and seen[0].method == "GET"
    for path in ("//attacker.example", "/../private", "/path?token=secret"):
        with pytest.raises(ProviderError, match="PROVIDER_PATH_INVALID"):
            await client.get(path)


def test_configurable_prairielearn_path_and_strict_auth_markers():
    assert provider_base("prairielearn", "https://institution.example/pl/") == "https://institution.example/pl"
    for value in ("http://school.example/pl", "https://school.example/pl?token=x", "https://user:password@school.example/pl"):
        with pytest.raises(ProviderError):
            provider_base("prairielearn", value)
    assert authenticated_page("prairielearn", '<h1>PrairieLearn Homepage</h1><a href="/pl/logout">Log out</a>')
    assert not authenticated_page("gradescope", '<h1>Course Dashboard</h1>')
    assert not authenticated_page("gradescope", '<h1>Course Dashboard</h1><a href="/logout">Log out</a><input type="password">')
