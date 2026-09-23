"""Validate untrusted model output before replay, persistence, or tool execution."""

import json


class InvalidChatResponse(ValueError):
    pass


def normalize_chat_response(value: object) -> dict:
    if not isinstance(value, dict):
        raise InvalidChatResponse("Expected an assistant message")
    content = value.get("content")
    if content is None:
        content = ""
    if isinstance(content, list):
        if not all(isinstance(block, dict) and block.get("type") == "text"
                   and isinstance(block.get("text"), str) for block in content):
            raise InvalidChatResponse("Unsupported content blocks")
        content = "".join(block["text"] for block in content)
    if not isinstance(content, str):
        raise InvalidChatResponse("Expected text content")
    calls = value.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list) or len(calls) > 6:
        raise InvalidChatResponse("Invalid tool call list")
    normalized = []
    ids = set()
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise InvalidChatResponse("Invalid tool call")
        function = call["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidChatResponse("Missing tool name")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError as error:
                raise InvalidChatResponse("Malformed tool arguments") from error
        if not isinstance(arguments, dict):
            raise InvalidChatResponse("Tool arguments must be an object")
        call_id = call.get("id", f"call-{index}")
        if not isinstance(call_id, str) or not call_id or call_id in ids:
            raise InvalidChatResponse("Invalid or duplicate tool call ID")
        ids.add(call_id)
        normalized.append({"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
    # DeepSeek thinking tool rounds require reasoning_content to be replayed.
    # Keep only supported extension fields, not arbitrary output annotations.
    extensions = {key: value[key] for key in ("reasoning_content", "_anthropic_content") if key in value}
    if extensions.get("reasoning_content") is not None and not isinstance(extensions["reasoning_content"], str):
        raise InvalidChatResponse("Invalid reasoning content")
    return {**extensions, "role": "assistant", "content": content, "tool_calls": normalized}


def parse_fallback(content: str) -> dict:
    try:
        envelope = json.loads(content)
    except ValueError as error:
        raise InvalidChatResponse("Malformed strict JSON") from error
    if not isinstance(envelope, dict):
        raise InvalidChatResponse("Expected a strict JSON object")
    if envelope.get("type") == "final" and isinstance(envelope.get("content"), str):
        return normalize_chat_response({"content": envelope["content"]})
    calls = envelope.get("calls")
    if envelope.get("type") != "tool_calls" or not isinstance(calls, list) or not calls:
        raise InvalidChatResponse("Invalid strict tool envelope")
    if not all(isinstance(call, dict) for call in calls):
        raise InvalidChatResponse("Invalid strict tool call")
    return normalize_chat_response({"tool_calls": [
        {"function": {"name": call.get("name"), "arguments": call.get("arguments", {})}}
        for call in calls
    ]})
