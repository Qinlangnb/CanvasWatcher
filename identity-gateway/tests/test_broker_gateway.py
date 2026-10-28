import asyncio
import json

import httpx
import pytest
from fastapi.responses import Response
from passlib.hash import sha512_crypt

from aw_gateway.broker_map import BrokerMap, BrokerMappingError
from aw_gateway.config import GatewayConfig
from aw_gateway.main import create_app
from aw_gateway.security import issue_token
from aw_gateway.store import IdentityStore


def _backend_ticket():
    return {
        "challenge_id": "inner-challenge-123456789", "instance_id": "inner-instance-123456789",
        "provider": "gradescope", "one_time_token": "inner-ticket-1234567890123456789",
        "control_token": "inner-control-1234567890123456789", "expires_in": 240,
        "protocol_version": 1,
    }


def test_outer_broker_capabilities_are_user_bound_and_single_use():
    broker = BrokerMap()
    outer = broker.create(_backend_ticket(), user_id="owner", slot=1)
    assert outer["challenge_id"] != _backend_ticket()["challenge_id"]
    assert outer["one_time_token"] != _backend_ticket()["one_time_token"]
    with pytest.raises(BrokerMappingError):
        broker.browser_control({**outer}, user_id="other")
    slot, inner_control = broker.browser_control(outer, user_id="owner")
    assert slot == 1 and inner_control["control_token"] == _backend_ticket()["control_token"]
    with pytest.raises(BrokerMappingError):
        broker.broker_claim({**outer, "one_time_token": "wrong"})
    slot, inner_claim = broker.broker_claim(outer)
    assert slot == 1 and inner_claim["one_time_token"] == _backend_ticket()["one_time_token"]
    exchange = broker.exchange(outer, "inner-exchange-1234567890123456")
    slot, inner_completion = broker.broker_completion({**outer, "exchange_token": exchange, "cookies": {"session": "fixture"}})
    assert slot == 1 and inner_completion["exchange_token"] == "inner-exchange-1234567890123456"
    broker.finish(outer)
    with pytest.raises(BrokerMappingError):
        broker.broker_completion({**outer, "exchange_token": exchange})


def test_cookie_less_broker_return_is_routed_by_outer_capability(tmp_path, monkeypatch):
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
    seen = []

    async def fake_tenant(request, *, config, slot, body_override=None, max_body=32768, query=None):
        payload = json.loads(body_override)
        seen.append((slot, request.url.path, payload))
        if request.url.path.endswith("/challenges"):
            return Response(content=json.dumps(_backend_ticket()), media_type="application/json")
        if request.url.path.endswith("/claim"):
            assert payload["one_time_token"] == _backend_ticket()["one_time_token"]
            return Response(content=json.dumps({"exchange_token": "inner-exchange-1234567890123456", "base_url": "https://gradescope.com", "operation": "login"}), media_type="application/json")
        assert payload["exchange_token"] == "inner-exchange-1234567890123456"
        return Response(content=json.dumps({"state": "AUTHENTICATED"}), media_type="application/json")

    monkeypatch.setattr("aw_gateway.main.buffered_tenant_request", fake_tenant)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as browser:
            await browser.post(
                "/watcher/login", headers={"Origin": "https://watcher.example.com"},
                data={"username": "example_admin", "password": "new owner password"},
            )
            ticket_response = await browser.post(
                "/watcher/api/auth/broker/challenges",
                headers={"Origin": "https://watcher.example.com", "X-AW-Broker": "1", "X-AW-CSRF": browser.cookies.get("aw_csrf")},
                json={"provider": "gradescope", "credential_id": "gradescope"},
            )
            assert ticket_response.status_code == 200
            ticket = ticket_response.json()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://watcher.example.com"
        ) as broker:
            probe = await broker.get("/watcher/api/health")
            assert probe.status_code == 200 and probe.json() == {"status": "ok"}
            wrong = await broker.post(
                "/watcher/api/auth/broker/claim", headers={"X-AW-Broker": "1"},
                json={**ticket, "one_time_token": "wrong"},
            )
            assert wrong.status_code == 409
            claimed = await broker.post(
                "/watcher/api/auth/broker/claim", headers={"X-AW-Broker": "1"}, json=ticket,
            )
            assert claimed.status_code == 200
            exchange = claimed.json()["exchange_token"]
            assert exchange != "inner-exchange-1234567890123456"
            completed = await broker.post(
                "/watcher/api/auth/broker/complete", headers={"X-AW-Broker": "1"},
                json={"challenge_id": ticket["challenge_id"], "instance_id": ticket["instance_id"],
                      "provider": ticket["provider"], "exchange_token": exchange, "cookies": {"session": "fixture"}},
            )
            assert completed.status_code == 200
            replay = await broker.post(
                "/watcher/api/auth/broker/complete", headers={"X-AW-Broker": "1"},
                json={"challenge_id": ticket["challenge_id"], "instance_id": ticket["instance_id"],
                      "provider": ticket["provider"], "exchange_token": exchange, "cookies": {"session": "fixture"}},
            )
            assert replay.status_code == 409
        assert [row[0] for row in seen] == [1, 1, 1]

    asyncio.run(run())
