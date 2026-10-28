"""Explicit production paths; the gateway never trusts tenant paths from requests."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GatewayConfig:
    database_path: Path
    static_root: Path
    socket_root: Path
    token_root: Path
    csrf_key_file: Path
    public_origin: str = "https://watcher.example.com"
    base_path: str = "/watcher"
    slots: int = 5
    development: bool = False

    def __post_init__(self):
        origin = urlsplit(self.public_origin)
        if origin.scheme not in ({"http", "https"} if self.development else {"https"}):
            raise ValueError("The public origin must use HTTPS")
        if not origin.netloc or origin.path or origin.query or origin.fragment:
            raise ValueError("The public origin must contain only scheme and host")
        if self.base_path != "/watcher":
            raise ValueError("This release requires the /watcher public base")
        if not 1 <= self.slots <= 5:
            raise ValueError("The initial deployment supports at most five slots")

    @classmethod
    def from_environment(cls):
        return cls(
            database_path=Path(os.environ.get("AW_GATEWAY_DATABASE", "/var/lib/academic-watcher-gateway/identity.db")),
            static_root=Path(os.environ.get("AW_GATEWAY_STATIC_ROOT", "/opt/academic-watcher/frontend/dist")),
            socket_root=Path(os.environ.get("AW_GATEWAY_SOCKET_ROOT", "/run/academic-watcher")),
            token_root=Path(os.environ.get("AW_GATEWAY_TOKEN_ROOT", "/etc/academic-watcher/tenant-tokens")),
            csrf_key_file=Path(os.environ.get("AW_GATEWAY_CSRF_KEY_FILE", "/etc/academic-watcher/gateway-csrf.key")),
            public_origin=os.environ.get("AW_GATEWAY_PUBLIC_ORIGIN", "https://watcher.example.com"),
            development=os.environ.get("AW_GATEWAY_ENV", "production") == "development",
        )

    def socket_path(self, slot: int) -> Path:
        if not 1 <= slot <= self.slots:
            raise ValueError("Invalid instance slot")
        # One directory per tenant (tenant UID : gateway group, 0750); uvicorn makes
        # the socket itself 0666, so the directory is the access boundary.
        return self.socket_root / f"t{slot}" / "api.sock"

    def token_path(self, slot: int) -> Path:
        if not 1 <= slot <= self.slots:
            raise ValueError("Invalid instance slot")
        return self.token_root / f"tenant-{slot}.token"


def load_private_key(path: Path) -> bytes:
    if path.is_symlink():
        raise RuntimeError("Gateway key cannot be a symlink")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("Gateway key must be a regular file")
    # systemd LoadCredential files are 0440 inside a per-unit private directory.
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    from_systemd = bool(credentials) and path.parent == Path(credentials)
    if os.name == "posix" and not from_systemd and details.st_mode & 0o077:
        raise RuntimeError("Gateway key must be private to the gateway account")
    value = path.read_bytes()
    if len(value) < 32 or len(value) > 256:
        raise RuntimeError("Gateway key is invalid")
    return value


def load_tenant_token(path: Path) -> bytes:
    if path.is_symlink():
        raise RuntimeError("Tenant token cannot be a symlink")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("Tenant token must be a regular file")
    if os.name == "posix" and details.st_mode & 0o007:
        raise RuntimeError("Tenant token must not be readable by other users")
    value = path.read_bytes().strip()
    if len(value) < 32 or len(value) > 256 or any(byte < 33 or byte > 126 for byte in value):
        raise RuntimeError("Tenant token is invalid")
    return value
