from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.ai.presets import PROVIDERS, provider_choices, provider_for_url
from app.ai.provider import AnthropicProvider, OpenAICompatibleProvider
from app.db import AppSetting

_lock = RLock()


class AISettingsError(ValueError):
    """Only locally authored, secret-free messages may reach the UI."""


class AIProbeSecretStore:
    def __init__(self) -> None:
        self._api_key: str | None = None
        self.base_url = ""
        self.generation = 0
        self.verified: tuple | None = None

    def set(self, value: str, base_url: str = "") -> None:
        candidate = value.strip()
        if not candidate or any(c.isspace() for c in candidate) or not candidate.isascii():
            raise AISettingsError("Enter an API key without spaces, newlines or a Bearer prefix.")
        with _lock:
            self._api_key = candidate
            self.base_url = base_url
            self.generation += 1
            self.verified = None

    def get(self, base_url: str | None = None) -> str | None:
        if base_url is not None and self.base_url != base_url:
            return None
        return self._api_key

    def clear(self) -> None:
        with _lock:
            self._api_key = None
            self.base_url = ""
            self.generation += 1
            self.verified = None


ai_probe_secrets = AIProbeSecretStore()


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AISettingsError("Choose a supported AI provider.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AISettingsError("AI base URL cannot contain credentials, query, or fragment")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _value(db: Session) -> dict:
    row = db.get(AppSetting, "ai_probe", populate_existing=True)
    return (row.value_json or {}) if row else {}


def _identity(value: dict) -> tuple:
    return (value.get("base_url"), value.get("model_id"), value.get("revision"), ai_probe_secrets.generation)


def get_ai_probe_settings(db: Session) -> dict:
    with _lock:
        value = _value(db)
        base = value.get("base_url", "")
        provider = provider_for_url(base)
        local = bool(provider and PROVIDERS[provider][2] == "local")
        loaded = bool(ai_probe_secrets.get(base))
        ready = bool(provider and value.get("model_id") and (local or loaded)
                     and ai_probe_secrets.verified == _identity(value))
        return {
            "provider": provider, "providers": provider_choices(),
            "base_url": base if provider else "",
            "needs_provider_selection": bool(base and not provider),
            "model_id": value.get("model_id", ""),
            "credential_loaded": loaded if provider else False,
            "connected_to_live_ai": ready, "calendar_chat_ready": ready, "chat_ready": ready,
        }


def set_ai_probe_settings(db: Session, base_url: str, model_id: str, provider_id: str | None = None) -> dict:
    if provider_id is not None:
        if provider_id not in PROVIDERS:
            raise AISettingsError("Choose a supported AI provider.")
        normalized = PROVIDERS[provider_id][1]
        if base_url and normalize_base_url(base_url) != normalized:
            raise AISettingsError("Base URL does not match the selected provider.")
    else:
        normalized = normalize_base_url(base_url)
        if not provider_for_url(normalized):
            raise AISettingsError("Custom Base URLs are no longer supported. Choose an AI provider.")
    with _lock:
        row = db.get(AppSetting, "ai_probe", populate_existing=True)
        previous = (row.value_json or {}) if row else {}
        if row is None:
            row = AppSetting(key="ai_probe", value_json={})
            db.add(row)
        selected = model_id.strip()
        unchanged = previous.get("base_url") == normalized and previous.get("model_id") == selected
        row.value_json = previous if unchanged else {
            "base_url": normalized, "model_id": selected, "revision": str(uuid4()),
            "callability_verified": False,
        }
        db.commit()
        if previous.get("base_url") != normalized:
            ai_probe_secrets.clear()
        elif not unchanged:
            ai_probe_secrets.verified = None
        return get_ai_probe_settings(db)


def set_ai_credential(db: Session, value: str, provider_id: str) -> dict:
    with _lock:
        current = get_ai_probe_settings(db)
        if not provider_id or current["provider"] != provider_id:
            raise AISettingsError("Provider changed. Save the selected provider before importing its key.")
        if PROVIDERS[provider_id][2] == "local":
            raise AISettingsError("Local Ollama does not require an API key.")
        ai_probe_secrets.set(value, current["base_url"])
        return {"credential_loaded": True}


def _provider(db: Session, model_id: str | None = None) -> OpenAICompatibleProvider:
    settings = get_ai_probe_settings(db)
    provider_id = settings["provider"]
    if not provider_id:
        raise AISettingsError("Choose and save a supported provider in AI Settings first.")
    base = settings["base_url"]
    key = ai_probe_secrets.get(base)
    protocol = PROVIDERS[provider_id][2]
    if protocol == "local":
        key = "ollama"
    if not key:
        raise AISettingsError("Load an API key for this provider in this backend session first.")
    selected = (model_id or settings["model_id"]).strip()
    factory = AnthropicProvider if protocol == "anthropic" else OpenAICompatibleProvider
    return factory(base, key, selected)


def calendar_chat_provider(db: Session) -> OpenAICompatibleProvider:
    return chat_provider(db)


def chat_provider(db: Session) -> OpenAICompatibleProvider:
    with _lock:
        if not get_ai_probe_settings(db)["chat_ready"]:
            raise AISettingsError("AI Chat is not configured. Open Settings -> AI API.")
        return _provider(db)


def sanitized_error(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "The provider timed out."
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code in {401, 403}:
            return "The provider rejected the credential. Check its provider and region."
        if code == 404:
            return "The endpoint or selected model was not found. You can enter a model ID if listing is unsupported."
        if code == 429:
            return "The provider rate limit or quota was reached."
        return f"The provider returned HTTP {code}."
    if isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
        return "The provider endpoint could not be reached. For Ollama, check the selected host and port 11434."
    if isinstance(error, AISettingsError):
        return str(error)
    return "The provider returned an invalid response or the call failed."


async def list_probe_models(db: Session) -> dict:
    try:
        with _lock:
            provider = _provider(db)
            identity = _identity(_value(db))
        models = await provider.list_models()
        with _lock:
            if identity != _identity(_value(db)):
                raise AISettingsError("Connection changed during model discovery. Please retry.")
        return {"success": True, "models": models, "message": None}
    except Exception as error:
        return {"success": False, "models": [], "message": sanitized_error(error)}


async def probe_selected_model(db: Session, model_id: str | None = None) -> dict:
    try:
        with _lock:
            value = _value(db)
            provider = _provider(db, model_id)
            if not provider.model or provider.model != value.get("model_id"):
                raise AISettingsError("Save the selected model ID before testing it.")
            ai_probe_secrets.verified = None
            ai_probe_secrets.generation += 1
            identity = _identity(value)
        result = await provider.probe()
        if not result.get("success"):
            raise AISettingsError("The selected model did not pass its connection test.")
        with _lock:
            if identity != _identity(_value(db)):
                raise AISettingsError("Connection changed during the test. Please test again.")
            row = db.get(AppSetting, "ai_probe")
            row.value_json = {**row.value_json, "callability_verified": True,
                              "verified_model_id": provider.model, "verified_at": datetime.now(UTC).isoformat()}
            db.commit()
            ai_probe_secrets.verified = identity
        return {**result, "message": None}
    except Exception as error:
        return {"success": False, "latency_ms": None, "model_id": model_id or get_ai_probe_settings(db)["model_id"],
                "message": sanitized_error(error)}
