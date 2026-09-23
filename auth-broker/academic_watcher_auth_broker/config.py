import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or parsed.port is None):
        raise ValueError("Expected an explicit loopback HTTP origin and port")
    return value.rstrip("/")


def provider_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(ord(c) < 33 or c == "\\" for c in value)):
        raise ValueError("Provider must have a configured HTTPS origin")
    return value.rstrip("/")


def frontend_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or "*" in value
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or any(ord(c) < 33 or c == "\\" for c in value)):
        raise ValueError("Frontend must be an explicit HTTP/HTTPS origin without wildcard or path")
    _ = parsed.port
    return value


def default_profile_root() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "AcademicWatcher" / "AuthBroker" / "profiles"
    return Path.home() / ".local" / "share" / "AcademicWatcher" / "AuthBroker" / "profiles"


@dataclass(frozen=True)
class Config:
    backend_origin: str = "http://127.0.0.1:8000"
    frontend_origins: tuple[str, ...] = ("http://localhost:8080", "http://127.0.0.1:8080")
    port: int = 8765
    profiles: Path = field(default_factory=default_profile_root)

    def __post_init__(self):
        loopback_origin(self.backend_origin)
        for origin in self.frontend_origins:
            frontend_origin(origin)
        if not self.frontend_origins or not 1024 <= self.port <= 65535:
            raise ValueError("Explicit frontend origins and an unprivileged port are required")

    @classmethod
    def from_env(cls):
        return cls(backend_origin=os.getenv("AW_BROKER_BACKEND", "http://127.0.0.1:8000"),
                   frontend_origins=tuple(os.getenv("AW_BROKER_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",")),
                   port=int(os.getenv("AW_BROKER_PORT", "8765")))
