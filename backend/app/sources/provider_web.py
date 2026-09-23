"""Fixed-instance, GET-only transport. Redirects never forward session cookies."""

from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup


class ProviderError(ValueError):
    pass


def provider_base(provider: str, value: str) -> str:
    parsed = urlsplit(value)
    if (provider not in {"gradescope", "prairielearn"} or parsed.scheme != "https"
            or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in ({""} if provider == "gradescope" else {"", "/pl"})
            or any(ord(c) < 33 or c == "\\" for c in value)):
        raise ProviderError("PROVIDER_BASE_INVALID")
    try:
        _ = parsed.port
    except ValueError:
        raise ProviderError("PROVIDER_BASE_INVALID") from None
    return value.rstrip("/")


def authenticated_page(provider: str, html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one('input[type="password"]'):
        return False
    if provider == "gradescope":
        return bool(soup.select_one('a[href^="/logout"]') and soup.find("h1", string="Course Dashboard"))
    return bool(soup.select_one('a[href="/pl/logout"]') and soup.find("h1", string="PrairieLearn Homepage"))


class ProviderWeb:
    def __init__(self, session, transport=None):
        self.session = session
        self.base = provider_base(session.provider, session.base_url)
        self.transport = transport

    async def get(self, path=""):
        if path and (not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path or ".." in path or "\\" in path):
            raise ProviderError("PROVIDER_PATH_INVALID")
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False,
                    transport=self.transport) as client:
                async with client.stream("GET", self.base + path, cookies=self.session.cookies) as response:
                    if response.status_code in {301, 302, 303, 307, 308, 401}:
                        raise ProviderError("AUTH_REQUIRED")
                    if response.status_code == 403:
                        raise ProviderError("PROVIDER_PERMISSION_DENIED")
                    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
                        raise ProviderError("PROVIDER_RESPONSE_CHANGED")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 8_000_000:
                            raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")
        except httpx.HTTPError:
            raise ProviderError("PROVIDER_NETWORK_ERROR") from None

    async def probe(self):
        html = await self.get("/" if self.session.provider == "gradescope" else "")
        if not authenticated_page(self.session.provider, html):
            raise ProviderError("AUTH_REQUIRED")
        return html
