import hashlib
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


class CredentialStore:
    """Process-memory-only credentials. This class never serializes secrets."""

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

    def set_ntlm(self, credential_id: str, username: str, password: str) -> None:
        with self._lock:
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
            if candidate is None:
                self._credentials.pop(credential_id, None)
            else:
                self._credentials[credential_id] = candidate
            return True

    def set_canvas(self, credential_id: str, base_url: str, token: str) -> None:
        with self._lock:
            self._credentials[credential_id] = CanvasCredential(base_url, token)
            self._install_slot(credential_id, self._credentials[credential_id])
            self._activate_best(credential_id)
            self._blocked_canvas.discard(credential_id)

    def set_canvas_session(
        self, credential_id: str, base_url: str, cookies: dict[str, str]
    ) -> None:
        with self._lock:
            self._credentials[credential_id] = CanvasBrowserSessionCredential(
                base_url, dict(cookies)
            )
            self._install_slot(credential_id, self._credentials[credential_id])
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
            self._canvas_modes[credential_id] = CanvasAuthMode(mode)
            self._preferred_modes[credential_id] = CanvasAuthMode(mode)
            selected = self.canvas_slot(credential_id, mode)
            if selected:
                self._credentials[credential_id] = selected

    def canvas_mode(self, credential_id: str) -> CanvasAuthMode | None:
        with self._lock:
            return self._canvas_modes.get(credential_id)

    def block_canvas(self, credential_id: str) -> None:
        with self._lock:
            self._blocked_canvas.add(credential_id)
            self.clear(credential_id)

    def canvas_blocked(self, credential_id: str) -> bool:
        with self._lock:
            return credential_id in self._blocked_canvas

    def clear(self, credential_id: str) -> None:
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
