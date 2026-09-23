import asyncio
import re
from collections.abc import Callable
from threading import BoundedSemaphore, Lock

import httpx
import requests
from requests_ntlm import HttpNtlmAuth

from app.auth.models import AuthRule, FetchResult, FetchStatus, NtlmCredential
from app.auth.scope import in_auth_scope
from app.auth.store import CredentialStore, credential_store


def detect_auth_scheme(headers: requests.structures.CaseInsensitiveDict | httpx.Headers) -> str | None:
    challenge = headers.get("www-authenticate", "")
    schemes = {part.strip().split(" ", 1)[0].lower() for part in re.split(r",", challenge)}
    if "ntlm" in schemes:
        return "NTLM"
    if "negotiate" in schemes:
        return "Negotiate"
    return None


def _status_result(
    *,
    url: str,
    status_code: int,
    headers: requests.structures.CaseInsensitiveDict | httpx.Headers,
    content: bytes | None,
    authenticated: bool = False,
) -> FetchResult:
    scheme = detect_auth_scheme(headers)
    content_length_text = headers.get("content-length")
    try:
        content_length = int(content_length_text) if content_length_text else None
    except ValueError:
        content_length = None
    common = {
        "url": url,
        "status_code": status_code,
        "content_type": headers.get("content-type", "").split(";", 1)[0] or None,
        "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"),
        "content_length": content_length,
        "auth_scheme": scheme,
        "authenticated": authenticated,
    }
    if 200 <= status_code < 300:
        return FetchResult(status=FetchStatus.OK, content=content, **common)
    if status_code == 304:
        return FetchResult(status=FetchStatus.NOT_MODIFIED, **common)
    if status_code == 401:
        status = FetchStatus.AUTH_FAILED if authenticated else FetchStatus.AUTH_REQUIRED
        return FetchResult(status=status, **common)
    if status_code == 404:
        return FetchResult(status=FetchStatus.NOT_FOUND, **common)
    return FetchResult(status=FetchStatus.ERROR, error=f"http_{status_code}", **common)


class AnonymousFetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def fetch(self, url: str) -> FetchResult:
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True, transport=self.transport
            ) as client:
                response = await client.get(url)
            return _status_result(
                url=str(response.url),
                status_code=response.status_code,
                headers=response.headers,
                content=response.content if response.status_code < 300 else None,
            )
        except httpx.TimeoutException:
            return FetchResult(status=FetchStatus.ERROR, url=url, error="timeout")
        except httpx.NetworkError:
            return FetchResult(status=FetchStatus.ERROR, url=url, error="network_error")
        except httpx.HTTPError:
            return FetchResult(status=FetchStatus.ERROR, url=url, error="http_client_error")


class NtlmFetcher:
    def __init__(
        self,
        session_factory: Callable[[], requests.Session] = requests.Session,
        max_concurrency: int = 3,
    ) -> None:
        self.session_factory = session_factory
        self._sessions: dict[str, tuple[NtlmCredential, requests.Session, Lock]] = {}
        self._sessions_lock = Lock()
        self._global_limit = BoundedSemaphore(max_concurrency)

    def invalidate(self, credential_id: str) -> None:
        with self._sessions_lock:
            cached = self._sessions.pop(credential_id, None)
        if cached:
            cached[1].close()

    def invalidate_all(self) -> None:
        with self._sessions_lock:
            cached_sessions = list(self._sessions.values())
            self._sessions.clear()
        for _, session, _ in cached_sessions:
            session.close()

    def _session(
        self, credential_id: str, credential: NtlmCredential
    ) -> tuple[requests.Session, Lock]:
        with self._sessions_lock:
            cached = self._sessions.get(credential_id)
            if cached and cached[0] is credential:
                return cached[1], cached[2]
            if cached:
                cached[1].close()
            session = self.session_factory()
            session.auth = HttpNtlmAuth(credential.username, credential.password)
            lock = Lock()
            self._sessions[credential_id] = (credential, session, lock)
            return session, lock

    async def fetch(
        self, url: str, credential_id: str, credential: NtlmCredential
    ) -> FetchResult:
        session, session_lock = self._session(credential_id, credential)
        return await asyncio.to_thread(
            self._fetch_sync, session, session_lock, url
        )

    def _fetch_sync(
        self, session: requests.Session, session_lock: Lock, url: str
    ) -> FetchResult:
        try:
            with self._global_limit, session_lock:
                # A Requests auth hook may replay credentials on redirects. Never
                # let the authenticated session follow an unvalidated destination.
                response = session.get(url, timeout=30, allow_redirects=False)
            return _status_result(
                url=response.url,
                status_code=response.status_code,
                headers=response.headers,
                content=response.content if response.status_code < 300 else None,
                authenticated=True,
            )
        except requests.Timeout:
            return FetchResult(status=FetchStatus.ERROR, url=url, error="timeout")
        except requests.ConnectionError:
            return FetchResult(status=FetchStatus.ERROR, url=url, error="network_error")
        except requests.RequestException:
            return FetchResult(status=FetchStatus.ERROR, url=url, error="http_client_error")


class ResourceFetcher:
    def __init__(
        self,
        store: CredentialStore = credential_store,
        anonymous: AnonymousFetcher | None = None,
        ntlm: NtlmFetcher | None = None,
    ) -> None:
        self.store = store
        self.anonymous = anonymous or AnonymousFetcher()
        self.ntlm = ntlm or NtlmFetcher()

    async def fetch(self, url: str, auth_rule: AuthRule | None = None) -> FetchResult:
        if auth_rule and auth_rule.auth_type.lower() == "ntlm":
            base = auth_rule.base_url or auth_rule.probe_url
            if not in_auth_scope(url, base, auth_rule.path_prefix):
                return FetchResult(status=FetchStatus.ERROR, url=url, error="auth_scope_rejected")
            public = await self.anonymous.fetch(url)
            if public.status_code != 401 or public.auth_scheme not in {"NTLM", "Negotiate"}:
                return public
            # Anonymous redirects do not confer trust on their final destination.
            if not in_auth_scope(public.url, base, auth_rule.path_prefix):
                return FetchResult(status=FetchStatus.ERROR, url=url, error="auth_scope_rejected")
            credential = self.store.get_ntlm(auth_rule.credential_id)
            if credential is None:
                return FetchResult(
                    status=FetchStatus.AUTH_REQUIRED,
                    url=url,
                    auth_scheme="NTLM",
                    error="credential_not_loaded",
                )
            return await self.ntlm.fetch(public.url, auth_rule.credential_id, credential)
        return await self.anonymous.fetch(url)


resource_fetcher = ResourceFetcher()
