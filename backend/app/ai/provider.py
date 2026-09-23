import json
from time import perf_counter
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from app.ai.chat_protocol import InvalidChatResponse, normalize_chat_response
from app.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    async def structured_generate(self, *, system_prompt: str, user_prompt: str, response_model: type[T]) -> T: ...


class DisabledProvider:
    async def structured_generate(self, *, system_prompt: str, user_prompt: str, response_model: type[T]) -> T:
        raise RuntimeError("LLM provider is disabled; configure LLM_PROVIDER and credentials")


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def structured_generate(self, *, system_prompt: str, user_prompt: str, response_model: type[T]) -> T:
        schema = response_model.model_json_schema()
        payload = {"model": self.model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "response_format": {"type": "json_schema", "json_schema": {"name": response_model.__name__, "strict": True, "schema": schema}}}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}/chat/completions", json=payload, headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
        return response_model.model_validate_json(response.json()["choices"][0]["message"]["content"])

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        native_tools: bool = True,
    ) -> dict:
        payload: dict = {"model": self.model, "messages": messages}
        if tools and native_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
        try:
            body = response.json()
            message = body["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise InvalidChatResponse("Provider returned a malformed chat response")
            return normalize_chat_response(message)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise InvalidChatResponse("Provider returned a malformed chat response") from error

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("Provider returned a malformed model list")
        return sorted(
            {
                str(row["id"])
                for row in body["data"]
                if isinstance(row, dict) and row.get("id")
            }
        )

    async def probe(self, model: str | None = None) -> dict:
        model_id = model or self.model
        started = perf_counter()
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        }
        # GPT reasoning models reject legacy max_tokens; don't use a two-token
        # budget that can prevent reasoning providers from producing a response.
        payload["max_completion_tokens" if self.base_url == "https://api.openai.com/v1" else "max_tokens"] = 1024
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
        try:
            message = response.json()["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ValueError("Provider returned a malformed probe response") from error
        return {
            "success": True,
            "latency_ms": round((perf_counter() - started) * 1000),
            "model_id": model_id,
        }


class AnthropicProvider(OpenAICompatibleProvider):
    """Translate the internal tool conversation to Anthropic's native API."""

    @property
    def headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    async def structured_generate(self, *, system_prompt: str, user_prompt: str, response_model: type[T]) -> T:
        schema = json.dumps(response_model.model_json_schema())
        reply = await self.chat(messages=[
            {"role": "system", "content": system_prompt + "\nReturn only JSON matching this schema: " + schema},
            {"role": "user", "content": user_prompt},
        ])
        return response_model.model_validate_json(reply["content"])

    async def list_models(self) -> list[str]:
        models: set[str] = set()
        after = None
        async with httpx.AsyncClient(timeout=20) as client:
            for _ in range(10):
                params = {"limit": 100}
                if after:
                    params["after_id"] = after
                response = await client.get(f"{self.base_url}/models", headers=self.headers, params=params)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise ValueError("Provider returned a malformed model list")
                models.update(row["id"] for row in body["data"] if isinstance(row, dict) and isinstance(row.get("id"), str))
                if not body.get("has_more"):
                    return sorted(models)
                cursor = body.get("last_id")
                if not cursor or cursor == after:
                    break
                after = cursor
        raise ValueError("Provider returned an incomplete model list")

    async def chat(self, *, messages: list[dict], tools: list[dict] | None = None,
                   native_tools: bool = True, max_tokens: int = 4096) -> dict:
        system = []
        converted: list[dict] = []
        for row in messages:
            if row["role"] in {"system", "developer"}:
                system.append(row.get("content") or "")
                continue
            role = "user" if row["role"] == "tool" else row["role"]
            if row["role"] == "tool":
                blocks = [{"type": "tool_result", "tool_use_id": row["tool_call_id"], "content": row.get("content") or ""}]
            elif row.get("_anthropic_content") is not None:
                blocks = row["_anthropic_content"]
            else:
                blocks = [{"type": "text", "text": row["content"]}] if row.get("content") else []
                for call in row.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                                   "input": json.loads(call["function"]["arguments"])})
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": list(blocks)})
        payload = {"model": self.model, "max_tokens": max_tokens, "messages": converted}
        if system:
            payload["system"] = "\n".join(system)
        if tools and native_tools:
            payload["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                                 "input_schema": t["function"]["parameters"]} for t in tools]
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}/messages", headers=self.headers, json=payload)
            response.raise_for_status()
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("role") != "assistant" or not isinstance(body.get("content"), list):
                raise InvalidChatResponse("Provider returned a malformed chat response")
            blocks = body["content"]
            if not all(isinstance(block, dict) for block in blocks):
                raise InvalidChatResponse("Provider returned malformed content blocks")
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": b["input"]}}
                     for b in blocks if b.get("type") == "tool_use"]
            return normalize_chat_response({"role": "assistant", "content": "".join(b["text"] for b in blocks if b.get("type") == "text"),
                                            "tool_calls": calls, "_anthropic_content": blocks})
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidChatResponse("Provider returned a malformed chat response") from error

    async def probe(self, model: str | None = None) -> dict:
        started = perf_counter()
        await self.chat(messages=[{"role": "user", "content": "Reply with OK."}], max_tokens=1024)
        return {"success": True, "latency_ms": round((perf_counter() - started) * 1000), "model_id": self.model}


def provider_from_settings(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai_compatible":
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required")
        return OpenAICompatibleProvider(
            settings.llm_base_url, settings.llm_api_key, settings.llm_model
        )
    return DisabledProvider()
