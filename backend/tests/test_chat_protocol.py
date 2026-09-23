import asyncio
import json
from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import select
from test_v060 import FakeProvider, memory_db, request, settings

from app.ai.chat_protocol import InvalidChatResponse, normalize_chat_response, parse_fallback
from app.ai.provider import AnthropicProvider, OpenAICompatibleProvider
from app.db import ChatMessage
from app.services.ai_chat import _error, _save_message, chat, history


def test_today_chat_roundtrip_dates_persist_as_json():
    provider = FakeProvider([
        {"tool_calls": [{"id": "today", "function": {"name": "get_today", "arguments": {}}}]},
        {"content": "Today: **ready**. $x^2$"},
    ])
    with memory_db() as db:
        result = asyncio.run(chat(db, request("What should I do today?"), settings(), provider=provider))
        assert result["error"] is None
        assert result["message"] == "Today: **ready**. $x^2$"
        json.dumps(result)  # No date/enum/datetime may reach persistence or API unencoded.
        saved = history(db, result["conversation_id"])
        assert any(row["kind"] == "tool_result" for row in saved)
        assert provider.calls[1]["messages"][-1]["role"] == "tool"
        assert json.loads(provider.calls[1]["messages"][-1]["content"])
        _save_message(db, result["conversation_id"], "tool", "dates", payload={
            "nested": [date(2026, 9, 20), {"at": datetime(2026, 9, 20, 12)}]})
        db.commit()
        row = db.scalars(select(ChatMessage).where(ChatMessage.content == "dates")).one()
        assert row.payload_json["nested"] == ["2026-09-20", {"at": "2026-09-20T12:00:00"}]


@pytest.mark.parametrize("value", [None, [], {"content": 4}, {"content": [{}]},
    {"tool_calls": {}}, {"tool_calls": [None]}, {"tool_calls": [{"function": []}]},
    {"tool_calls": [{"function": {"name": "get_today", "arguments": []}}]},
    {"tool_calls": [{"function": {"name": "get_today", "arguments": "null"}}]},
    {"tool_calls": [{"function": {"name": "get_today", "arguments": "{"}}]},
])
def test_invalid_native_shapes_are_normalized_errors(value):
    with memory_db() as db:
        result = asyncio.run(chat(db, request("Hello"), settings(), provider=FakeProvider([value])))
        assert result["error"]["code"] == "INVALID_TOOL_OUTPUT"
        assert result["tool_results"] == []


@pytest.mark.parametrize("text", ['[]', 'null', 'true', '4', '{"type":"final","content":{}}',
    '{"type":"tool_calls","calls":{}}', '{"type":"tool_calls","calls":[null]}'])
def test_fallback_shape_guards(text):
    with pytest.raises(InvalidChatResponse):
        parse_fallback(text)


def test_text_blocks_object_arguments_and_reasoning_are_preserved():
    value = normalize_chat_response({"content": [{"type": "text", "text": "Hello"}],
        "reasoning_content": "opaque fixture",
        "tool_calls": [{"function": {"name": "get_today", "arguments": {}}}]})
    assert value["content"] == "Hello"
    assert value["tool_calls"][0]["function"]["arguments"] == "{}"
    assert value["reasoning_content"] == "opaque fixture"


def test_two_round_deepseek_replay_keeps_required_reasoning_but_drops_output_metadata(monkeypatch):
    original = httpx.AsyncClient
    calls = []
    def handler(req):
        payload = json.loads(req.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None, "reasoning_content": "opaque fixture",
                "annotations": [], "provider_trace": "output-only",
                "tool_calls": [{"id": "today", "function": {"name": "get_today", "arguments": "{}"}}],
            }}]})
        replay = payload["messages"][-2]
        assert replay["reasoning_content"] == "opaque fixture"
        assert "annotations" not in replay and "provider_trace" not in replay
        assert payload["messages"][-1]["tool_call_id"] == "today"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Today ready."}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with memory_db() as db:
        result = asyncio.run(chat(db, request("Today?"), settings(),
            provider=OpenAICompatibleProvider("https://api.deepseek.com/v1", "fixture", "deepseek-flash")))
        assert result["error"] is None
        assert result["provider_mode"] == "native"
        assert len(calls) == 2


def test_fallback_markdown_and_latex_survive_json_decoding():
    content = "**Result**\n\n$$\n\\frac{1}{2}\n$$"
    assert parse_fallback(json.dumps({"type": "final", "content": content}))["content"] == content


def test_internal_errors_never_expose_sql_or_secret_text():
    for error in [TypeError("fixture-secret SQL INSERT"), RuntimeError("fixture-secret")]:
        assert "fixture-secret" not in json.dumps(_error(error))
        assert "SQL" not in json.dumps(_error(error))


@pytest.mark.parametrize("factory,body,path", [
    (OpenAICompatibleProvider, {"choices": [{"message": {"role": "assistant", "content": []}}]}, "/v1/chat/completions"),
    (AnthropicProvider, {"role": "assistant", "content": [None]}, "/v1/messages"),
])
def test_provider_transport_and_malformed_blocks(monkeypatch, factory, body, path):
    original = httpx.AsyncClient
    def handler(request):
        assert request.url.path == path
        return httpx.Response(200, json=body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    provider = factory("https://fixture.test/v1", "fixture", "model")
    if factory is AnthropicProvider:
        with pytest.raises(InvalidChatResponse):
            asyncio.run(provider.chat(messages=[{"role": "user", "content": "Hello"}]))
    else:
        assert asyncio.run(provider.chat(messages=[{"role": "user", "content": "Hello"}]))["content"] == ""
