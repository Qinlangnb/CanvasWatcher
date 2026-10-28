"""Require a private gateway capability before any tenant API route is reached."""

import hmac
import os
import stat
from pathlib import Path

from starlette.responses import JSONResponse

_TOKEN_HEADER = b"x-aw-internal-token"
_FORBIDDEN_BROWSER_HEADERS = {b"cookie", b"authorization", b"proxy-authorization"}


def load_internal_token(path: Path) -> bytes:
    """Fail closed if the internal capability is absent or exposed to other users."""
    if path.is_symlink():
        raise RuntimeError("Gateway token must not be a symlink")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("Gateway token must be a regular file")
    if os.name == "posix" and details.st_mode & 0o007:
        raise RuntimeError("Gateway token must not be readable by other users")
    value = path.read_bytes().strip()
    if len(value) < 32 or len(value) > 256 or any(byte < 33 or byte > 126 for byte in value):
        raise RuntimeError("Gateway token is invalid")
    return value


class InternalGatewayAuth:
    """ASGI guard for a per-user backend reachable only through the identity gateway.

    The gateway credential is removed before forwarding to application handlers.
    Ordinary browser credentials must never reach a tenant instance.
    """

    def __init__(self, app, *, token: bytes):
        if len(token) < 32:
            raise ValueError("Internal gateway token is too short")
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", ())
        presented = [value for key, value in headers if key.lower() == _TOKEN_HEADER]
        forbidden = any(key.lower() in _FORBIDDEN_BROWSER_HEADERS for key, _ in headers)
        valid = (
            len(presented) == 1
            and not forbidden
            and hmac.compare_digest(presented[0], self.token)
        )
        if not valid:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "GATEWAY_AUTH_REQUIRED"}, status_code=403)(
                    scope, receive, send
                )
            return

        private_scope = dict(scope)
        private_scope["headers"] = [
            (key, value) for key, value in headers if key.lower() != _TOKEN_HEADER
        ]
        await self.app(private_scope, receive, send)
