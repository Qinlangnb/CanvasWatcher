import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ai.presets import PROVIDERS
from app.ai.provider import AnthropicProvider, OpenAICompatibleProvider
from app.api.routes import router
from app.db import AppSetting, Base, get_db
from app.main import safe_ai_validation_error
from app.services.ai_settings import (
    AISettingsError,
    ai_probe_secrets,
    chat_provider,
    get_ai_probe_settings,
    list_probe_models,
    probe_selected_model,
    set_ai_credential,
    set_ai_probe_settings,
)


@pytest.fixture
def db():
    ai_probe_secrets.clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    ai_probe_secrets.clear()
    engine.dispose()


def configure(db, provider="deepseek", model="model"):
    set_ai_probe_settings(db, "", model, provider)
    if PROVIDERS[provider][2] != "local":
        set_ai_credential(db, "fixture-secret", provider)


def mock_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize("provider", PROVIDERS)
def test_presets_are_server_owned(db, provider):
    state = set_ai_probe_settings(db, "", "", provider)
    assert state["base_url"] == PROVIDERS[provider][1]
    assert state["provider"] == provider
    assert not state["chat_ready"]


@pytest.mark.parametrize("base", ["https://attacker.test/v1", "https://api.deepseek.com/anthropic",
                                  "https://api.deepseek.com/v1?key=private", "https://user:private@api.deepseek.com/v1"])
def test_reject_custom_endpoint_before_mutation(db, base):
    configure(db)
    with pytest.raises(ValueError):
        set_ai_probe_settings(db, base, "model")
    assert get_ai_probe_settings(db)["provider"] == "deepseek"
    assert ai_probe_secrets.get(PROVIDERS["deepseek"][1]) == "fixture-secret"


def test_legacy_endpoint_requires_selection_and_never_echoes_url(db):
    db.add(AppSetting(key="ai_probe", value_json={"base_url": "https://example.test/?secret=private", "model_id": "model"}))
    db.commit()
    state = get_ai_probe_settings(db)
    assert state["needs_provider_selection"]
    assert state["base_url"] == ""
    assert "private" not in repr(state)


async def test_missing_key_models_is_actionable_not_500(db):
    set_ai_probe_settings(db, "", "", "deepseek")
    result = await list_probe_models(db)
    assert not result["success"]
    assert "API key" in result["message"]


@pytest.mark.parametrize("candidate", ["", "   ", "Bearer secret", "secret\nother", "密钥"])
def test_invalid_import_retains_existing_key(db, candidate):
    configure(db)
    with pytest.raises(AISettingsError):
        set_ai_credential(db, candidate, "deepseek")
    assert ai_probe_secrets.get(PROVIDERS["deepseek"][1]) == "fixture-secret"


async def test_ready_binds_key_endpoint_model_and_process(db, monkeypatch):
    configure(db)
    async def success(self):
        return {"success": True, "model_id": self.model, "latency_ms": 1}
    monkeypatch.setattr(OpenAICompatibleProvider, "probe", success)
    assert (await probe_selected_model(db))["success"]
    assert get_ai_probe_settings(db)["chat_ready"]
    assert chat_provider(db).model == "model"
    set_ai_credential(db, "replacement", "deepseek")
    assert not get_ai_probe_settings(db)["chat_ready"]
    assert (await probe_selected_model(db))["success"]
    set_ai_probe_settings(db, "", "changed", "deepseek")
    assert not get_ai_probe_settings(db)["chat_ready"]
    assert (await probe_selected_model(db))["success"]
    ai_probe_secrets.clear()
    assert not get_ai_probe_settings(db)["connected_to_live_ai"]
    with pytest.raises(AISettingsError):
        chat_provider(db)


def test_switch_provider_does_not_forward_previous_key(db):
    configure(db)
    set_ai_probe_settings(db, "", "model", "kimi")
    assert not get_ai_probe_settings(db)["credential_loaded"]
    assert ai_probe_secrets.get() is None
    with pytest.raises(AISettingsError):
        set_ai_credential(db, "stale-key", "deepseek")


@pytest.mark.parametrize("change", ["key", "model", "provider", "clear"])
async def test_late_probe_cannot_enable_changed_connection(db, monkeypatch, change):
    configure(db)
    entered, release = asyncio.Event(), asyncio.Event()
    async def pending(self):
        entered.set()
        await release.wait()
        return {"success": True, "model_id": self.model}
    monkeypatch.setattr(OpenAICompatibleProvider, "probe", pending)
    operation = asyncio.create_task(probe_selected_model(db))
    await entered.wait()
    if change == "key":
        set_ai_credential(db, "replacement", "deepseek")
    elif change == "clear":
        ai_probe_secrets.clear()
    elif change == "model":
        set_ai_probe_settings(db, "", "other", "deepseek")
    else:
        set_ai_probe_settings(db, "", "model", "kimi")
    release.set()
    assert not (await operation)["success"]
    assert not get_ai_probe_settings(db)["chat_ready"]


async def test_unsaved_model_probe_does_not_verify(db, monkeypatch):
    configure(db)
    async def unexpected(self):
        pytest.fail("Should not contact provider")
    monkeypatch.setattr(OpenAICompatibleProvider, "probe", unexpected)
    assert not (await probe_selected_model(db, "other"))["success"]


async def test_ollama_works_without_key(db, monkeypatch):
    configure(db, "ollama")
    def handler(request):
        assert request.url.host == "host.docker.internal"
        assert request.headers["authorization"] == "Bearer ollama"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]})
    mock_transport(monkeypatch, handler)
    assert (await list_probe_models(db))["models"] == ["local-model"]
    assert (await probe_selected_model(db))["success"]
    assert get_ai_probe_settings(db)["chat_ready"]
    assert not get_ai_probe_settings(db)["credential_loaded"]


@pytest.mark.parametrize("provider,limit_field", [("gpt", "max_completion_tokens"), ("deepseek", "max_tokens")])
async def test_probe_parameter_and_response_validation(db, monkeypatch, provider, limit_field):
    configure(db, provider)
    bodies = [{"choices": [{"message": {"role": "assistant", "content": "OK"}}]}, {"error": "not a completion"}]
    def handler(request):
        payload = json.loads(request.content)
        assert payload[limit_field] == 1024
        assert ("max_tokens" if limit_field == "max_completion_tokens" else "max_completion_tokens") not in payload
        return httpx.Response(200, json=bodies.pop(0))
    mock_transport(monkeypatch, handler)
    assert (await probe_selected_model(db))["success"]
    assert not (await probe_selected_model(db))["success"]
    assert not get_ai_probe_settings(db)["chat_ready"]


async def test_anthropic_native_tools_and_headers(monkeypatch):
    seen = []
    def handler(request):
        assert request.headers["x-api-key"] == "fixture-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "claude-fixture"}], "has_more": False})
        assert request.url.path == "/v1/messages"
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(200, json={"role": "assistant", "content": [
                {"type": "tool_use", "id": "tool-1", "name": "get_today", "input": {}}]})
        return httpx.Response(200, json={"role": "assistant", "content": [{"type": "text", "text": "Done"}]})
    mock_transport(monkeypatch, handler)
    provider = AnthropicProvider("https://api.anthropic.com/v1", "fixture-secret", "claude-fixture")
    assert await provider.list_models() == ["claude-fixture"]
    messages = [{"role": "system", "content": "System"}, {"role": "user", "content": "Today?"}]
    reply = await provider.chat(messages=messages, tools=[{"function": {"name": "get_today", "parameters": {"type": "object"}}}])
    assert reply["tool_calls"][0]["function"]["arguments"] == "{}"
    assert seen[0]["system"] == "System"
    assert seen[0]["tools"][0]["input_schema"] == {"type": "object"}
    result = await provider.chat(messages=[*messages, reply, {"role": "tool", "tool_call_id": "tool-1", "content": "{}"}])
    assert result["content"] == "Done"
    assert seen[1]["messages"][-1]["content"][0]["tool_use_id"] == "tool-1"
    assert seen[1]["messages"][-2]["content"] == reply["_anthropic_content"]


async def test_http_contract_and_secret_redaction(db, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, safe_ai_validation_error)
    app.dependency_overrides[get_db] = lambda: db
    async def success(self):
        return {"success": True, "latency_ms": 1, "model_id": self.model}
    monkeypatch.setattr(OpenAICompatibleProvider, "probe", success)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.put("/api/settings/ai", json={"provider": "deepseek", "model_id": "model"})).status_code == 200
        for payload in ({"api_key": "hidden-secret"}, {"provider": "deepseek", "api_key": {"bad": "hidden-secret"}}):
            bad = await client.post("/api/settings/ai/credential", json=payload)
            assert bad.status_code == 422
            assert "hidden-secret" not in bad.text
        loaded = await client.post("/api/settings/ai/credential", json={"provider": "deepseek", "api_key": "hidden-secret"})
        assert loaded.status_code == 200
        assert "hidden-secret" not in loaded.text
        assert (await client.post("/api/settings/ai/test")).json()["success"]
        state = (await client.get("/api/settings/ai")).json()
        assert state["chat_ready"]
        assert "hidden-secret" not in repr(db.get(AppSetting, "ai_probe").value_json)
        assert (await client.delete("/api/settings/ai/credential")).status_code == 204
        assert not (await client.get("/api/settings/ai")).json()["chat_ready"]
