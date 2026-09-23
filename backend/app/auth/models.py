from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FetchStatus(StrEnum):
    OK = "ok"
    NOT_MODIFIED = "not_modified"
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    NOT_FOUND = "not_found"
    ERROR = "error"


class CanvasAuthMode(StrEnum):
    OAUTH = "oauth"
    PAT = "pat"
    BROWSER_SESSION = "browser_session"


class FetchResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: FetchStatus
    url: str
    status_code: int | None = None
    content: bytes | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_length: int | None = None
    auth_scheme: str | None = None
    error: str | None = None
    authenticated: bool = False


class AuthRule(BaseModel):
    path_prefix: str
    auth_type: str
    credential_id: str
    probe_url: str
    base_url: str | None = None


@dataclass(slots=True, repr=False)
class NtlmCredential:
    username: str
    password: str

    def __repr__(self) -> str:
        return "NtlmCredential(username=<redacted>, password=<redacted>)"


@dataclass(slots=True, repr=False)
class CanvasCredential:
    base_url: str
    token: str

    @property
    def auth_mode(self) -> CanvasAuthMode:
        return CanvasAuthMode.PAT

    def __repr__(self) -> str:
        return "CanvasCredential(base_url=<configured>, token=<redacted>)"


@dataclass(slots=True, repr=False)
class CanvasBrowserSessionCredential:
    base_url: str
    cookies: dict[str, str]

    @property
    def auth_mode(self) -> CanvasAuthMode:
        return CanvasAuthMode.BROWSER_SESSION

    def __repr__(self) -> str:
        return "CanvasBrowserSessionCredential(base_url=<configured>, cookies=<redacted>)"


@dataclass(slots=True, repr=False)
class CanvasOAuthCredential(CanvasCredential):
    refresh_token: str
    expires_clock: float

    @property
    def auth_mode(self) -> CanvasAuthMode:
        return CanvasAuthMode.OAUTH

    def __repr__(self) -> str:
        return "CanvasOAuthCredential(credentials=<redacted>)"


CanvasCredentialValue = CanvasCredential | CanvasBrowserSessionCredential | CanvasOAuthCredential
