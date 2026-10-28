"""Generation-guarded browser credentials with optional encrypted persistence."""

import secrets
import threading
from dataclasses import dataclass, field

from app.auth.vault import VaultError, valid_cookies, valid_text


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
        self._vault = None

    @property
    def persistence_enabled(self):
        with self.lock:
            return self._vault is not None

    def _snapshot(self):
        return {key: {'provider': value.provider, 'base_url': value.base_url, 'cookies': dict(value.cookies)}
                for key, value in self.values.items()}

    def configure_persistence(self, vault):
        with self.lock:
            try:
                saved = vault.read('provider_sessions') if vault else {}
                loaded = {}
                for key, entry in saved.items():
                    valid_text(key)
                    if (not isinstance(entry, dict) or set(entry) != {'provider', 'base_url', 'cookies'}
                            or entry['provider'] not in {'gradescope', 'prairielearn'}):
                        raise VaultError()
                    loaded[key] = ProviderSession(entry['provider'], valid_text(entry['base_url']), valid_cookies(entry['cookies']))
            except Exception:
                raise VaultError() from None
            for value in self.values.values():
                value.cookies.clear()
            self._vault = vault
            self.values = loaded
            self.generations = {key: secrets.randbits(48) + 1 for key in loaded}

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
            if self._vault:
                saved = self._snapshot()
                saved[key] = {'provider': value.provider, 'base_url': value.base_url, 'cookies': dict(value.cookies)}
                self._vault.write('provider_sessions', saved)
            self._remove_memory(key)
            self.values[key] = ProviderSession(value.provider, value.base_url, dict(value.cookies))

    def remove(self, key):
        with self.lock:
            if self._vault:
                saved = self._snapshot()
                saved.pop(key, None)
                self._vault.write('provider_sessions', saved)
            self._remove_memory(key)

    def _remove_memory(self, key):
        with self.lock:
            old = self.values.pop(key, None)
            if old:
                old.cookies.clear()
            self.generations[key] = self.generation(key) + 1


provider_sessions = ProviderSessions()
