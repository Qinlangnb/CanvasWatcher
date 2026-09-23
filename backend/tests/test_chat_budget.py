import asyncio
import json

import httpx
from sqlalchemy import select
from test_v060 import FakeProvider, memory_db, request, settings

from app.db import AIToolConfirmation, ChatMessage
from app.services import ai_chat


def lookups(count=1, round_index=0):
    return {"content": "", "tool_calls": [
        {"id": f"r{round_index}-c{i}", "function": {"name": "list_courses", "arguments": "{}"}}
        for i in range(count)
    ]}


def test_observed_six_round_seventeen_call_shape_can_produce_answer():
    # Actual failed conversation batch sizes; fixture data replaces private course facts.
    batches = [1, 5, 2, 5, 2, 2]
    provider = FakeProvider([*[lookups(n, i) for i, n in enumerate(batches)], {"content": "已查完；没有证据的考试日期标为未知。"}])
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("把几个课程的midterm时间都给我报一下"), settings(), provider=provider))
        assert result["error"] is None
        assert len(result["tool_results"]) == 17
        assert len(provider.calls) == 7
        assert len(result["diagnostic"]["rounds"]) == 7
        assert "20 tool rounds" in provider.calls[0]["messages"][0]["content"]


def test_twenty_rounds_then_one_final_only_request_preserves_all_evidence():
    assert ai_chat.MAX_TOOL_ROUNDS == 20
    provider = FakeProvider([*[lookups(round_index=i) for i in range(20)], {"content": "Summary from the collected evidence."}])
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("Exams?"), settings(), provider=provider))
        assert result["error"] is None
        assert len(result["tool_results"]) == 20
        assert len(provider.calls) == 21
        final = provider.calls[-1]
        assert final["tools"] is None and final["native_tools"] is False
        assert "final answer NOW" in final["messages"][0]["content"]
        assert len([m for m in final["messages"] if m["role"] == "tool"]) == 20
        saved = db.scalars(select(ChatMessage).where(ChatMessage.content == result["message"])).one()
        assert saved.payload_json["diagnostic"]["stage"] == "complete"
        assert saved.payload_json["diagnostic"]["rounds"][-1]["final_only"] is True


def test_model_cannot_extend_budget_or_propose_writes_in_final_only_turn():
    final_call = {"tool_calls": [{"id": "write", "function": {"name": "archive_course", "arguments": {"course_id": 1}}}]}
    provider = FakeProvider([*[lookups(round_index=i) for i in range(20)], final_call])
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("Keep calling forever"), settings(), provider=provider))
        assert result["error"]["code"] == "AI_TOOL_LIMIT_REACHED"
        assert len(provider.calls) == 21
        assert len(result["tool_results"]) == 20
        assert db.scalar(select(AIToolConfirmation)) is None
        assert result["error"]["diagnostic"]["failure_type"] == "ToolBudgetExceeded"


def test_fallback_final_only_retains_page_context_and_evidence():
    failure = httpx.Response(400, request=httpx.Request("POST", "https://fixture.test"))
    fallback_call = {"content": json.dumps({"type": "tool_calls", "calls": [{"name": "list_courses", "arguments": {}}]})}
    provider = FakeProvider([httpx.HTTPStatusError("unsupported tools", request=failure.request, response=failure),
        *[fallback_call for _ in range(20)], {"content": '{"type":"final","content":"Final evidence summary."}'}])
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("Exams?"), settings(), provider=provider))
        assert result["error"] is None
        assert result["provider_mode"] == "structured_fallback"
        assert len(provider.calls) == 22  # One protocol fallback, not another tool round.
        prompt = provider.calls[-1]["messages"][0]["content"]
        assert '"type":"final"' in prompt and "Current page context:" in prompt
        assert provider.calls[-1]["tools"] is None


def test_diagnostics_distinguish_schema_and_format_without_secret_values():
    for response, expected_stage, expected_type in [
        (None, "response_parse", "InvalidChatResponse"),
        ({"tool_calls": [{"function": {"name": "list_courses", "arguments": {"private-field": "fixture-secret"}}}]}, "tool_validation", "ValidationError"),
    ]:
        with memory_db() as db:
            result = asyncio.run(ai_chat.chat(db, request("Exams?"), settings(), provider=FakeProvider([response])))
            diagnostic = result["error"]["diagnostic"]
            assert diagnostic["stage"] == expected_stage
            assert diagnostic["failure_type"] == expected_type
            assert "fixture-secret" not in json.dumps(result)
            assert "private-field" not in json.dumps(result)
    shape = ai_chat._response_shape({"content": "fixture-secret", "reasoning_content": "fixture-secret",
        "tool_calls": [{"function": {"name": "fixture-secret", "arguments": "fixture-secret"}}]})
    assert "fixture-secret" not in json.dumps(shape)


def test_provider_turn_has_total_timeout(monkeypatch):
    class SlowProvider:
        async def chat(self, **kwargs):
            await asyncio.sleep(1)
    monkeypatch.setattr(ai_chat, "PROVIDER_TURN_TIMEOUT_SECONDS", 0.001)
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("Exams?"), settings(), provider=SlowProvider()))
        assert result["error"]["code"] == "PROVIDER_UNAVAILABLE"
        assert result["error"]["diagnostic"]["failure_type"] == "TimeoutError"


def test_missing_file_read_recovers_and_preserves_tool_call_id():
    missing = {"tool_calls": [{"id": "missing-file", "function": {"name": "get_file_text", "arguments": {"file_id": 999999}}}]}
    provider = FakeProvider([missing, lookups(), {"content": "文件缺失，无法确认日期。"}])
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("Exams?"), settings(), provider=provider))
        assert result["error"] is None
        assert result["tool_results"][0]["result"]["error"]["code"] == "FILE_NOT_FOUND"
        replay = next(m for m in provider.calls[1]["messages"] if m.get("tool_call_id") == "missing-file")
        assert json.loads(replay["content"])["found"] is False
        assert db.scalar(select(AIToolConfirmation)) is None


def test_internal_lookup_error_is_not_hidden_as_missing_file(monkeypatch):
    async def broken(*args, **kwargs):
        raise LookupError("private-internal-secret")
    monkeypatch.setattr(ai_chat, "execute_tool", broken)
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("Exams?"), settings(), provider=FakeProvider([lookups()])))
        assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"
        assert "private-internal-secret" not in json.dumps(result)


def test_fallback_missing_file_read_recovers():
    failure = httpx.Response(400, request=httpx.Request("POST", "https://fixture.test"))
    provider = FakeProvider([httpx.HTTPStatusError("unsupported", request=failure.request, response=failure),
        {"content": json.dumps({"type": "tool_calls", "calls": [{"name": "get_file_metadata", "arguments": {"file_id": 999999}}]})},
        {"content": '{"type":"final","content":"File unavailable."}'}])
    with memory_db() as db:
        result = asyncio.run(ai_chat.chat(db, request("File?"), settings(), provider=provider))
        assert result["error"] is None
        assert result["provider_mode"] == "structured_fallback"
        assert "FILE_NOT_FOUND" in provider.calls[-1]["messages"][-1]["content"]


def test_null_file_id_recovers_but_other_invalid_arguments_still_fail():
    for tool_name in ("get_file_text", "get_file_metadata"):
        for arguments, recovers in (({"file_id": None}, True), ({"file_id": "not-an-id"}, False), ({"file_id": None, "extra": "secret"}, False), ({}, False)):
            provider = FakeProvider([{"tool_calls": [{"id": "null-file", "function": {"name": tool_name, "arguments": arguments}}]}, {"content": "No file available."}])
            with memory_db() as db:
                result = asyncio.run(ai_chat.chat(db, request("File?"), settings(), provider=provider))
                if recovers:
                    assert result["error"] is None
                    assert result["tool_results"][0]["result"]["error"]["code"] == "FILE_NOT_FOUND"
                else:
                    assert result["error"]["code"] == "INVALID_TOOL_OUTPUT"
