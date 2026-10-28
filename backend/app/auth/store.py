import hashlib
import math
import secrets
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Lock, RLock
from time import monotonic

from app.auth.models import (
    CanvasAuthMode,
    CanvasBrowserSessionCredential,
    CanvasCredential,
    CanvasCredentialValue,
    CanvasOAuthCredential,
    NtlmCredential,
)
from app.auth.vault import VaultError, valid_cookies, valid_text


class CredentialStore:
    """Memory store with optional server-encrypted durable publication."""

    def __init__(self) -> None:
        self._credentials: dict[str, NtlmCredential | CanvasCredentialValue] = {}
        self._canvas_modes: dict[str, CanvasAuthMode] = {}
        self._preferred_modes: dict[str, CanvasAuthMode] = {}
        self._blocked_canvas: set[str] = set()
        self._canvas_slots: dict[str, dict[CanvasAuthMode, CanvasCredentialValue]] = {}
        self._generations: dict[tuple[str, CanvasAuthMode], int] = {}
        self._slot_status: dict[tuple[str, CanvasAuthMode], dict] = {}
        self._routing_locks: dict = {}
        self._fallback: dict[str, dict] = {}
        self._lock = RLock()
        self._vault = None

    @property
    def persistence_enabled(self):
        with self._lock:
            return self._vault is not None

    def _snapshot(self):
        keys = set(self._credentials) | set(self._canvas_slots) | set(self._preferred_modes) | self._blocked_canvas
        entries = {}
        for key in keys:
            value = self._credentials.get(key)
            slots = {mode.value: self._encode(credential) for mode, credential in self._canvas_slots.get(key, {}).items()}
            entries[key] = {'ntlm': {'username': value.username, 'password': value.password}
                            if isinstance(value, NtlmCredential) else None,
                            'slots': slots, 'preferred': self.preferred_mode(key).value,
                            'blocked': key in self._blocked_canvas}
        return {'entries': entries}

    @staticmethod
    def _encode(value):
        result = {'base_url': value.base_url}
        if isinstance(value, CanvasBrowserSessionCredential):
            return {**result, 'cookies': dict(value.cookies)}
        result['token'] = value.token
        if isinstance(value, CanvasOAuthCredential):
            result.update(refresh_token=value.refresh_token,
                          expires_at=time.time() + value.expires_clock - monotonic())
        return result

    def _persist_change(self, key, change):
        if self._vault is None:
            return
        state = self._snapshot()
        entry = state['entries'].setdefault(key, {'ntlm': None, 'slots': {}, 'preferred': 'oauth', 'blocked': False})
        change(entry)
        self._vault.write('source_credentials', state)

    @contextmanager
    def _without_persistence(self):
        original, self._vault = self._vault, None
        try:
            yield
        finally:
            self._vault = original

    def configure_persistence(self, vault):
        with self._lock:
            try:
                state = vault.read('source_credentials') if vault else {}
                if state and set(state) != {'entries'}:
                    raise VaultError()
                entries = state.get('entries', {})
                if not isinstance(entries, dict):
                    raise VaultError()
                loaded = {}
                for key, entry in entries.items():
                    valid_text(key)
                    if not isinstance(entry, dict) or set(entry) != {'ntlm', 'slots', 'preferred', 'blocked'}:
                        raise VaultError()
                    if not isinstance(entry['blocked'], bool) or not isinstance(entry['slots'], dict):
                        raise VaultError()
                    preferred = CanvasAuthMode(entry['preferred'])
                    ntlm = entry['ntlm']
                    if ntlm is not None:
                        if not isinstance(ntlm, dict) or set(ntlm) != {'username', 'password'}:
                            raise VaultError()
                        ntlm = NtlmCredential(valid_text(ntlm['username']), valid_text(ntlm['password']))
                    slots = {}
                    for mode, raw in entry['slots'].items():
                        mode = CanvasAuthMode(mode)
                        if not isinstance(raw, dict):
                            raise VaultError()
                        base = valid_text(raw['base_url'])
                        if mode is CanvasAuthMode.BROWSER_SESSION:
                            if set(raw) != {'base_url', 'cookies'}:
                                raise VaultError()
                            slots[mode] = CanvasBrowserSessionCredential(base, valid_cookies(raw['cookies']))
                        elif mode is CanvasAuthMode.PAT:
                            if set(raw) != {'base_url', 'token'}:
                                raise VaultError()
                            slots[mode] = CanvasCredential(base, valid_text(raw['token']))
                        else:
                            if (set(raw) != {'base_url', 'token', 'refresh_token', 'expires_at'}
                                    or not isinstance(raw['expires_at'], (int, float)) or not math.isfinite(raw['expires_at'])):
                                raise VaultError()
                            slots[mode] = CanvasOAuthCredential(base, valid_text(raw['token']),
                                valid_text(raw['refresh_token']), monotonic() + raw['expires_at'] - time.time())
                    loaded[key] = (ntlm, slots, preferred, entry['blocked'])
            except Exception:
                raise VaultError() from None
            with self._without_persistence():
                self.clear_all()
            self._vault = vault
            for key, (ntlm, slots, preferred, blocked) in loaded.items():
                self._preferred_modes[key] = preferred
                if ntlm:
                    self._credentials[key] = ntlm
                if slots:
                    self._canvas_slots[key] = slots
                    self._activate_best(key)
                    for mode in slots:
                        self._generations[(key, mode)] = secrets.randbits(48) + 1
                        # Restored presence is not a fresh remote authentication probe.
                        self._slot_status[(key, mode)] = {'state': 'RESTORED'}
                if blocked:
                    self._blocked_canvas.add(key)

    def set_ntlm(self, credential_id: str, username: str, password: str) -> None:
        with self._lock:
            self._persist_change(credential_id, lambda entry: entry.update(ntlm={'username': username, 'password': password}))
            self._credentials[credential_id] = NtlmCredential(username, password)

    def atomic(self):
        """Short synchronous credential publication boundary; never await inside."""
        return self._lock

    def get_ntlm(self, credential_id: str) -> NtlmCredential | None:
        with self._lock:
            value = self._credentials.get(credential_id)
            return value if isinstance(value, NtlmCredential) else None

    def replace_ntlm(self, credential_id: str, expected: NtlmCredential | None,
                     candidate: NtlmCredential | None) -> bool:
        """Install a verified candidate only if the observed slot is unchanged."""
        with self._lock:
            if self._credentials.get(credential_id) is not expected:
                return False
            self._persist_change(credential_id, lambda entry: entry.update(ntlm=
                {'username': candidate.username, 'password': candidate.password} if candidate else None))
            if candidate is None:
                self._credentials.pop(credential_id, None)
            else:
                self._credentials[credential_id] = candidate
            return True

    def set_canvas(self, credential_id: str, base_url: str, token: str) -> None:
        with self._lock:
            self._install_slot(credential_id, CanvasCredential(base_url, token))
            self._activate_best(credential_id)
            self._blocked_canvas.discard(credential_id)

    def set_canvas_session(
        self, credential_id: str, base_url: str, cookies: dict[str, str]
    ) -> None:
        with self._lock:
            candidate = CanvasBrowserSessionCredential(
                base_url, dict(cookies)
            )
            self._install_slot(credential_id, candidate)
            self._activate_best(credential_id)

    def set_canvas_oauth(self, credential_id: str, credential: CanvasOAuthCredential) -> None:
        with self._lock:
            self._install_slot(credential_id, credential)
            self._activate_best(credential_id)

    def _activate_best(self, credential_id: str):
        slots = self._canvas_slots.get(credential_id, {})
        priority = [self._preferred_modes.get(credential_id), *CanvasAuthMode]
        selected = next((slots[mode] for mode in priority if mode in slots), None)
        if selected:
            self._credentials[credential_id] = selected
            self._canvas_modes[credential_id] = selected.auth_mode
        else:
            self._credentials.pop(credential_id, None)
        return selected

    def preferred_mode(self, credential_id: str) -> CanvasAuthMode:
        return self._preferred_modes.get(credential_id, CanvasAuthMode.OAUTH)

    def _install_slot(self, credential_id: str, credential: CanvasCredentialValue) -> None:
        def change(entry):
            entry['slots'][credential.auth_mode.value] = self._encode(credential)
            entry['ntlm'] = None
            if credential.auth_mode is CanvasAuthMode.PAT:
                entry['blocked'] = False
        self._persist_change(credential_id, change)
        self._canvas_slots.setdefault(credential_id, {})[credential.auth_mode] = credential
        key = (credential_id, credential.auth_mode)
        self._generations[key] = self._generations.get(key, 0) + 1
        self.mark_verified(credential_id, credential.auth_mode)

    def mark_verified(self, credential_id: str, mode: CanvasAuthMode | str) -> None:
        with self._lock:
            self._slot_status[(credential_id, CanvasAuthMode(mode))] = {
                **self._slot_status.get((credential_id, CanvasAuthMode(mode)), {}),
                "state": "VALID", "last_verified_at": datetime.now(UTC).isoformat(),
                "last_success_at": datetime.now(UTC).isoformat(),
                "verified_clock": monotonic(),
            }

    def recently_verified(self, credential_id: str, mode: CanvasAuthMode | str, ttl: float = 900) -> bool:
        with self._lock:
            info = self._slot_status.get((credential_id, CanvasAuthMode(mode)), {})
            return info.get("state") == "VALID" and monotonic() - info.get("verified_clock", 0) < ttl

    def bind_identity(self, credential_id: str, mode: CanvasAuthMode | str, account_id: str):
        with self._lock:
            credential = self.canvas_slot(credential_id, mode)
            if credential is not None:
                info = self._slot_status.setdefault((credential_id, CanvasAuthMode(mode)), {})
                info["account_identity_fingerprint"] = hashlib.sha256(
                    f"{credential.base_url}|{account_id}".encode()).hexdigest()

    def slot_status(self, credential_id: str, mode: CanvasAuthMode | str) -> dict:
        with self._lock:
            info = self._slot_status.get((credential_id, CanvasAuthMode(mode)), {})
            credential = self.canvas_slot(credential_id, mode)
            return {"present": self.canvas_slot(credential_id, mode) is not None,
                    "generation": self.generation(credential_id, mode),
                    "credential_type": CanvasAuthMode(mode).value,
                    "state": info.get("state", "MISSING"), "last_verified_at": info.get("last_verified_at"),
                    "last_success_at": info.get("last_success_at"), "last_failure_at": info.get("last_failure_at"),
                    "last_failure_reason": info.get("last_failure_reason"),
                    "account_identity_fingerprint": info.get("account_identity_fingerprint"),
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=max(0, credential.expires_clock - monotonic()))).isoformat()
                        if isinstance(credential, CanvasOAuthCredential) else None}

    def fallback_status(self, credential_id: str) -> dict | None:
        return self._fallback.get(credential_id)

    def routing_lock(self, credential_id: str):
        with self._lock:
            return self._routing_locks.setdefault(credential_id, Lock())

    def canvas_slot(self, credential_id: str, mode: CanvasAuthMode | str):
        with self._lock:
            return self._canvas_slots.get(credential_id, {}).get(CanvasAuthMode(mode))

    def generation(self, credential_id: str, mode: CanvasAuthMode | str) -> int:
        with self._lock:
            return self._generations.get((credential_id, CanvasAuthMode(mode)), 0)

    def remove_canvas_slot(self, credential_id: str, mode: CanvasAuthMode | str,
                           generation: int | None = None, *, state: str = "INVALID") -> bool:
        with self._lock:
            mode = CanvasAuthMode(mode)
            if generation is not None and self.generation(credential_id, mode) != generation:
                return False
            def change(entry):
                entry['slots'].pop(mode.value, None)
                if mode is CanvasAuthMode.PAT:
                    entry['blocked'] = True
            self._persist_change(credential_id, change)
            removed = self._canvas_slots.get(credential_id, {}).pop(mode, None)
            key = (credential_id, mode)
            self._generations[key] = self._generations.get(key, 0) + 1
            self._slot_status[(credential_id, mode)] = {
                **self._slot_status.get((credential_id, mode), {}), "state": state,
                "last_failure_at": datetime.now(UTC).isoformat(), "last_failure_reason": state,
            }
            if mode is CanvasAuthMode.PAT:
                self._blocked_canvas.add(credential_id)
            if removed is None:
                return False
            if isinstance(removed, CanvasCredential):
                removed.token = ""
                if isinstance(removed, CanvasOAuthCredential):
                    removed.refresh_token = ""
            else:
                removed.cookies.clear()
            effective = self._activate_best(credential_id)
            if effective:
                self._credentials[credential_id] = effective
                self._canvas_modes[credential_id] = effective.auth_mode
                self._fallback[credential_id] = {"from": mode.value, "to": effective.auth_mode.value,
                    "reason_code": f"{mode.value.upper()}_{state}", "at": datetime.now(UTC).isoformat()}
            else:
                self._credentials.pop(credential_id, None)
            return True

    def get_canvas(self, credential_id: str) -> CanvasCredentialValue | None:
        with self._lock:
            value = self._credentials.get(credential_id)
            return (
                value
                if isinstance(value, (CanvasCredential, CanvasBrowserSessionCredential))
                else None
            )

    def select_canvas_mode(
        self, credential_id: str, mode: CanvasAuthMode | str
    ) -> None:
        with self._lock:
            mode = CanvasAuthMode(mode)
            self._persist_change(credential_id, lambda entry: entry.update(preferred=mode.value))
            self._canvas_modes[credential_id] = mode
            self._preferred_modes[credential_id] = mode
            selected = self.canvas_slot(credential_id, mode)
            if selected:
                self._credentials[credential_id] = selected

    def canvas_mode(self, credential_id: str) -> CanvasAuthMode | None:
        with self._lock:
            return self._canvas_modes.get(credential_id)

    def block_canvas(self, credential_id: str) -> None:
        with self._lock:
            self._persist_change(credential_id, lambda entry: entry.update(ntlm=None, slots={}, blocked=True))
            self._blocked_canvas.add(credential_id)
            with self._without_persistence():
                self.clear(credential_id)

    def canvas_blocked(self, credential_id: str) -> bool:
        with self._lock:
            return credential_id in self._blocked_canvas

    def clear(self, credential_id: str) -> None:
        with self._lock:
            self._persist_change(credential_id, lambda entry: entry.update(ntlm=None, slots={},
                blocked=entry['blocked'] or 'pat' in entry['slots']))
            with self._without_persistence():
                self._clear_memory(credential_id)

    def _clear_memory(self, credential_id: str) -> None:
        with self._lock:
            for mode in list(self._canvas_slots.get(credential_id, {})):
                self.remove_canvas_slot(credential_id, mode)
            credential = self._credentials.pop(credential_id, None)
            if isinstance(credential, NtlmCredential):
                credential.password = ""
            elif isinstance(credential, CanvasCredential):
                credential.token = ""
            elif isinstance(credential, CanvasBrowserSessionCredential):
                for name in list(credential.cookies):
                    credential.cookies[name] = ""
                credential.cookies.clear()

    def clear_all(self) -> None:
        with self._lock:
            if self._vault:
                self._vault.write('source_credentials', {})
            with self._without_persistence():
                self._clear_all_memory()

    def _clear_all_memory(self) -> None:
        with self._lock:
            for credential_id in list(self._canvas_slots):
                self.clear(credential_id)
            self._canvas_slots.clear()
            self._generations.clear()
            self._slot_status.clear()
            self._routing_locks.clear()
            self._fallback.clear()
            for credential in self._credentials.values():
                if isinstance(credential, NtlmCredential):
                    credential.password = ""
                elif isinstance(credential, CanvasCredential):
                    credential.token = ""
                else:
                    for name in list(credential.cookies):
                        credential.cookies[name] = ""
                    credential.cookies.clear()
            self._credentials.clear()
            self._canvas_modes.clear()
            self._preferred_modes.clear()
            self._blocked_canvas.clear()


credential_store = CredentialStore()
