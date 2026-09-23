import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.auth.models import (
    CanvasAuthMode,
    CanvasBrowserSessionCredential,
    CanvasCredential,
    CanvasCredentialValue,
)


class CanvasErrorCode(StrEnum):
    AUTH_REQUIRED = "auth_required"
    INVALID_TOKEN = "invalid_token"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"
    RESOURCE_NOT_FOUND = "resource_not_found"


class CanvasAPIError(RuntimeError):
    def __init__(
        self,
        code: CanvasErrorCode,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class CanvasDownload:
    content: bytes
    status_code: int
    content_type: str | None
    content_length: int | None
    etag: str | None
    last_modified: str | None


class CanvasClient:
    """Small secret-safe Canvas API client with pagination and bounded retries."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        max_concurrency: int = 3,
        *,
        credential: CanvasCredentialValue | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = (credential.base_url if credential else base_url).rstrip("/")
        self.auth_mode = credential.auth_mode if credential else CanvasAuthMode.PAT
        self._token = credential.token if isinstance(credential, CanvasCredential) else token
        self._cookies = httpx.Cookies()
        if isinstance(credential, CanvasBrowserSessionCredential):
            self._set_canvas_cookies(credential.cookies)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._transport = transport
        self._sleep = sleep

    @classmethod
    def from_credential(
        cls,
        credential: CanvasCredentialValue,
        max_concurrency: int = 3,
        **kwargs: Any,
    ) -> "CanvasClient":
        return cls(
            credential.base_url,
            max_concurrency=max_concurrency,
            credential=credential,
            **kwargs,
        )

    def __repr__(self) -> str:
        return (
            f"CanvasClient(base_url={self.base_url!r}, "
            f"auth_mode={self.auth_mode.value!r}, credential=<redacted>)"
        )

    def _set_canvas_cookies(self, cookies: dict[str, str]) -> None:
        parts = urlsplit(self.base_url)
        host = parts.hostname
        if not host:
            raise CanvasAPIError(
                CanvasErrorCode.INVALID_RESPONSE, "Canvas base URL has no host"
            )
        for name, value in cookies.items():
            self._cookies.jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=host,
                    domain_specified=False,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=parts.scheme == "https",
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": None},
                    rfc2109=False,
                )
            )

    @property
    def _headers(self) -> dict[str, str]:
        if self.auth_mode in {CanvasAuthMode.PAT, CanvasAuthMode.OAUTH} and not self._token:
            raise CanvasAPIError(
                CanvasErrorCode.AUTH_REQUIRED, "Canvas authentication is required"
            )
        if self.auth_mode in {CanvasAuthMode.PAT, CanvasAuthMode.OAUTH}:
            return {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            }
        if not list(self._cookies.jar):
            raise CanvasAPIError(
                CanvasErrorCode.AUTH_REQUIRED, "Canvas authentication is required"
            )
        return {"Accept": "application/json"}

    def _url(self, path_or_url: str) -> str:
        url = urljoin(f"{self.base_url}/", path_or_url.lstrip("/"))
        expected = urlsplit(self.base_url)
        actual = urlsplit(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise CanvasAPIError(
                CanvasErrorCode.INVALID_RESPONSE,
                "Canvas pagination or download URL changed origin",
            )
        return url

    async def request(
        self,
        path_or_url: str,
        *,
        params: Any = None,
        timeout: float = 30,
        allow_external_redirect: bool = False,
    ) -> httpx.Response:
        url = self._url(path_or_url)
        last_error: CanvasAPIError | None = None
        for attempt in range(3):
            try:
                async with self._semaphore, httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    transport=self._transport,
                    cookies=self._cookies,
                ) as client:
                    response = await client.get(url, params=params, headers=self._headers)
                    for _ in range(6):
                        if not response.is_redirect:
                            break
                        destination = urljoin(str(response.url), response.headers.get("location", ""))
                        actual, expected = urlsplit(destination), urlsplit(self.base_url)
                        same_origin = (actual.scheme, actual.hostname, actual.port or 443) == (
                            expected.scheme, expected.hostname, expected.port or 443)
                        if actual.username or actual.password or actual.scheme != "https":
                            raise CanvasAPIError(CanvasErrorCode.INVALID_RESPONSE, "Unsafe Canvas redirect")
                        if not same_origin:
                            if not allow_external_redirect:
                                raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED,
                                                     "Canvas redirected outside its authenticated origin")
                            # External files use a separate client without headers or cookies.
                            async with httpx.AsyncClient(timeout=timeout, transport=self._transport,
                                                         follow_redirects=False) as anonymous:
                                response = await anonymous.get(destination)
                            # Never re-enter an authenticated redirect chain from an external host.
                            for _ in range(5):
                                if not response.is_redirect:
                                    break
                                destination = urljoin(str(response.url), response.headers.get("location", ""))
                                target = urlsplit(destination)
                                if target.scheme != "https" or target.username or target.password:
                                    raise CanvasAPIError(CanvasErrorCode.INVALID_RESPONSE, "Unsafe file redirect")
                                async with httpx.AsyncClient(timeout=timeout, transport=self._transport,
                                                             follow_redirects=False) as anonymous:
                                    response = await anonymous.get(destination)
                            break
                        response = await client.get(destination, headers=self._headers)
                    if response.is_redirect:
                        raise CanvasAPIError(CanvasErrorCode.INVALID_RESPONSE, "Too many Canvas redirects")
                    # Only same-origin response cookies enter this credential's jar.
                    for cookie in client.cookies.jar:
                        if cookie.domain.lstrip(".") == urlsplit(self.base_url).hostname:
                            self._cookies.jar.set_cookie(cookie)
            except httpx.TimeoutException as exc:
                last_error = CanvasAPIError(CanvasErrorCode.TIMEOUT, "Canvas request timed out")
                if attempt == 2:
                    raise last_error from exc
                await self._sleep(0.5 * (2**attempt))
                continue
            except httpx.NetworkError as exc:
                last_error = CanvasAPIError(
                    CanvasErrorCode.NETWORK_ERROR, "Canvas network request failed"
                )
                if attempt == 2:
                    raise last_error from exc
                await self._sleep(0.5 * (2**attempt))
                continue

            final = urlsplit(str(response.url))
            expected = urlsplit(self.base_url)
            if (
                self.auth_mode is CanvasAuthMode.BROWSER_SESSION
                and not allow_external_redirect
                and (final.scheme, final.netloc) != (expected.scheme, expected.netloc)
            ):
                raise CanvasAPIError(
                    CanvasErrorCode.AUTH_REQUIRED,
                    "Canvas browser session redirected to sign-in",
                )
            if response.status_code == 401:
                if self.auth_mode is CanvasAuthMode.BROWSER_SESSION:
                    raise CanvasAPIError(
                        CanvasErrorCode.AUTH_REQUIRED,
                        "Canvas browser session is no longer authenticated",
                        status_code=401,
                    )
                raise CanvasAPIError(
                    CanvasErrorCode.INVALID_TOKEN,
                    "Canvas rejected the access token",
                    status_code=401,
                )
            if response.status_code == 403:
                raise CanvasAPIError(
                    CanvasErrorCode.PERMISSION_DENIED,
                    "Canvas denied access to this API resource",
                    status_code=403,
                )
            if response.status_code == 404:
                raise CanvasAPIError(CanvasErrorCode.RESOURCE_NOT_FOUND,
                    "Canvas resource or endpoint is unavailable", status_code=404)
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after", "1")
                try:
                    delay = min(10.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = 1.0
                last_error = CanvasAPIError(
                    CanvasErrorCode.RATE_LIMITED,
                    "Canvas rate limit reached",
                    status_code=429,
                )
                if attempt == 2:
                    raise last_error
                await self._sleep(delay)
                continue
            if response.status_code >= 500:
                last_error = CanvasAPIError(
                    CanvasErrorCode.SERVER_ERROR,
                    "Canvas server returned a temporary error",
                    status_code=response.status_code,
                )
                if attempt == 2:
                    raise last_error
                await self._sleep(0.5 * (2**attempt))
                continue
            if response.is_error:
                raise CanvasAPIError(
                    CanvasErrorCode.INVALID_RESPONSE,
                    f"Canvas request failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            if "text/html" in response.headers.get("content-type", ""):
                sample = response.text[:8192].lower()
                if 'type="password"' in sample or "type='password'" in sample or "/login" in sample or "saml" in sample:
                    raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED, "Canvas sign-in required")
            return response
        raise last_error or CanvasAPIError(
            CanvasErrorCode.NETWORK_ERROR, "Canvas request failed"
        )

    async def get_json(self, path: str, params: Any = None) -> Any:
        response = await self.request(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            code = CanvasErrorCode.INVALID_RESPONSE
            raise CanvasAPIError(
                code, "Canvas returned a non-JSON response", status_code=response.status_code
            ) from exc

    async def get_all_pages(self, path: str, params: Any = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = path
        next_params = params
        while next_url:
            response = await self.request(next_url, params=next_params)
            next_params = None
            try:
                payload = response.json()
            except ValueError as exc:
                code = CanvasErrorCode.INVALID_RESPONSE
                raise CanvasAPIError(
                    code, "Canvas returned a non-JSON response", status_code=response.status_code
                ) from exc
            if not isinstance(payload, list):
                raise CanvasAPIError(
                    CanvasErrorCode.INVALID_RESPONSE,
                    "Canvas paginated endpoint did not return a list",
                    status_code=response.status_code,
                )
            rows.extend(row for row in payload if isinstance(row, dict))
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                self._url(next_url)
        return rows

    async def probe(self) -> dict[str, Any]:
        payload = await self.get_json("/api/v1/users/self/profile")
        if (
            not isinstance(payload, dict)
            or payload.get("id") is None
            or not (payload.get("name") or payload.get("short_name"))
        ):
            code = CanvasErrorCode.INVALID_RESPONSE
            raise CanvasAPIError(
                code,
                "Canvas profile response did not identify an account",
            )
        return payload

    async def download(self, url: str) -> CanvasDownload:
        response = await self.request(url, timeout=45, allow_external_redirect=True)
        length = response.headers.get("content-length")
        return CanvasDownload(
            content=response.content,
            status_code=response.status_code,
            content_type=response.headers.get("content-type", "").split(";", 1)[0]
            or None,
            content_length=int(length) if length and length.isdigit() else None,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
