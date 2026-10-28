"""Opt-in authenticated-encryption vault. Never expose its payload via an API."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from threading import RLock

from cryptography.fernet import Fernet

NAMESPACES = frozenset({'source_credentials', 'provider_sessions', 'ai_credential'})
MAX_BYTES = 16 * 1024 * 1024


class VaultError(RuntimeError):
    def __init__(self):
        super().__init__('Encrypted credential storage unavailable; check the server key and vault.')


def _private_regular(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise VaultError()
    if os.name == 'posix' and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()):
        raise VaultError()


def _private_key(path: Path) -> None:
    # systemd LoadCredential: root-owned 0440 + per-unit ACL in a private directory.
    credentials = os.environ.get('CREDENTIALS_DIRECTORY')
    if credentials and path.parent == Path(credentials):
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o007:
            raise VaultError()
        return
    _private_regular(path)


def _safe_parents(path: Path) -> None:
    if not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise VaultError()


def _private_directory(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise VaultError()
    if os.name == 'posix' and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()):
        raise VaultError()


class CredentialVault:
    """Single-process namespace transactions; deployment pins one backend worker."""

    def __init__(self, path: Path, key_path: Path):
        self.path = Path(path)
        self.lock = RLock()
        self._state: dict = {}
        self._failed = False
        try:
            _safe_parents(self.path)
            _safe_parents(Path(key_path))
            _private_key(Path(key_path))
            key = Path(key_path).read_bytes()
            if len(key.strip()) != 44:
                raise VaultError()
            self.cipher = Fernet(key.strip())
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            _private_directory(self.path.parent)
            if self.path.exists():
                _private_regular(self.path)
                if self.path.stat().st_size > MAX_BYTES:
                    raise VaultError()
                decoded = json.loads(self.cipher.decrypt(self.path.read_bytes()))
                if (not isinstance(decoded, dict) or set(decoded) != {'version', 'namespaces'}
                        or decoded['version'] != 1 or not isinstance(decoded['namespaces'], dict)
                        or not set(decoded['namespaces']) <= NAMESPACES
                        or any(not isinstance(value, dict) for value in decoded['namespaces'].values())):
                    raise VaultError()
                self._state = decoded['namespaces']
        except Exception:
            raise VaultError() from None

    def read(self, namespace: str) -> dict:
        with self.lock:
            if self._failed or namespace not in NAMESPACES:
                raise VaultError()
            return deepcopy(self._state.get(namespace, {}))

    def write(self, namespace: str, value: dict) -> None:
        with self.lock:
            temporary = None
            replaced = False
            try:
                if self._failed or namespace not in NAMESPACES or not isinstance(value, dict):
                    raise VaultError()
                _safe_parents(self.path)
                _private_directory(self.path.parent)
                if self.path.exists():
                    _private_regular(self.path)
                updated = {**self._state, namespace: deepcopy(value)}
                plaintext = json.dumps({'version': 1, 'namespaces': updated},
                                       allow_nan=False, separators=(',', ':')).encode()
                if len(plaintext) > MAX_BYTES:
                    raise VaultError()
                ciphertext = self.cipher.encrypt(plaintext)
                if len(ciphertext) > MAX_BYTES:
                    raise VaultError()
                fd, temporary = tempfile.mkstemp(prefix='.vault-', dir=self.path.parent)
                with os.fdopen(fd, 'wb') as output:
                    output.write(ciphertext)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
                replaced = True
                temporary = None
                if os.name == 'posix':
                    directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                self._state = updated
            except Exception:
                # A post-replace fsync error has an ambiguous durability result.
                # Never overwrite that newer disk state from the old RAM snapshot.
                if replaced:
                    self._failed = True
                raise VaultError() from None
            finally:
                if temporary is not None:
                    # Delete only this mkstemp-generated, uncommitted private artifact.
                    Path(temporary).unlink(missing_ok=True)


def valid_text(value) -> str:
    if not isinstance(value, str) or not value:
        raise VaultError()
    return value


def valid_cookies(value) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in value.items()):
        raise VaultError()
    return dict(value)
