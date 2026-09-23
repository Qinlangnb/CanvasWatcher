"""Generation-guarded memory-only browser credentials for read-only providers."""

import threading
from dataclasses import dataclass, field


@dataclass(repr=False)
class ProviderSession:
    provider: str
    base_url: str
    cookies: dict[str, str] = field(repr=False)

    def __repr__(self):
        return "ProviderSession(<redacted>)"


class ProviderSessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.values = {}
        self.generations = {}

    def generation(self, key):
        with self.lock:
            return self.generations.get(key, 0)

    def get(self, key):
        with self.lock:
            value = self.values.get(key)
            return ProviderSession(value.provider, value.base_url, dict(value.cookies)) if value else None

    def set(self, key, value, expected):
        with self.lock:
            if self.generation(key) != expected:
                raise ValueError("AUTH_CREDENTIAL_CHANGED")
            self.remove(key)
            self.values[key] = ProviderSession(value.provider, value.base_url, dict(value.cookies))

    def remove(self, key):
        with self.lock:
            old = self.values.pop(key, None)
            if old:
                old.cookies.clear()
            self.generations[key] = self.generation(key) + 1


provider_sessions = ProviderSessions()
