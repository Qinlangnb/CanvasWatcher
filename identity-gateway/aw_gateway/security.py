"""Password and capability helpers; never log raw credentials or tokens."""

import hashlib
import hmac
import os
import secrets
import threading
from ctypes import CDLL, c_char_p
from ctypes.util import find_library

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

_password_hasher = PasswordHasher()
_crypt_lock = threading.Lock()
_crypt_library = None


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compare_token(value: str, expected_digest: str) -> bool:
    return hmac.compare_digest(token_digest(value), expected_digest)


def hash_password(value: str) -> str:
    if len(value) < 12 or len(value) > 256:
        raise ValueError("Password must have 12 to 256 characters")
    return _password_hasher.hash(value)


def verify_password(value: str, encoded: str) -> bool:
    try:
        return _password_hasher.verify(encoded, value)
    except (VerificationError, ValueError, TypeError):
        return False


def verify_legacy_password(value: str, encoded: str) -> bool:
    if not encoded.startswith("$6$") or len(encoded) > 512 or "\x00" in value:
        return False
    try:
        if os.name == "posix":
            # libxcrypt is the supported SHA-512-crypt provider on Ubuntu. crypt(3)
            # uses a static return buffer, so serialize calls and copy it immediately.
            global _crypt_library
            with _crypt_lock:
                if _crypt_library is None:
                    library_name = find_library("crypt")
                    if not library_name:
                        return False
                    _crypt_library = CDLL(library_name)
                    _crypt_library.crypt.argtypes = [c_char_p, c_char_p]
                    _crypt_library.crypt.restype = c_char_p
                hashed = _crypt_library.crypt(value.encode("utf-8"), encoded.encode("ascii"))
                computed = hashed.decode("ascii") if hashed else ""
        else:
            # Developer tests on Windows retain a development-only compatibility
            # provider; the Linux production package has no passlib dependency.
            from passlib.hash import sha512_crypt

            return sha512_crypt.verify(value, encoded)
        return hmac.compare_digest(computed, encoded)
    except (AttributeError, OSError, UnicodeError, ValueError, TypeError, ImportError):
        return False


def csrf_token(secret: bytes, session_token: str) -> str:
    return hmac.new(secret, session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf(secret: bytes, session_token: str, provided: str) -> bool:
    return hmac.compare_digest(csrf_token(secret, session_token), provided)
