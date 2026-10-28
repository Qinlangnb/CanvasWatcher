"""Reach the public HTTPS gateway even when the local resolver still serves a stale record.

Home routers/ISP resolvers can keep a parked A record for days after a nameserver move. The
gateway host is therefore resolved over DNS-over-HTTPS (by IP, certificate-verified), falling
back to the system resolver. The gateway connection itself still verifies the gateway's own
certificate via SNI, so a wrong address can fail but cannot impersonate the gateway.
"""
import asyncio
import ipaddress
import threading
import time
from urllib.parse import urlsplit

import httpx

DOH = (("https://1.1.1.1/dns-query", {"name": "", "type": "A"}),
       ("https://223.5.5.5/resolve", {"name": "", "type": "1"}))
TTL = 300
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def doh_address(host: str, *, transport=None) -> str | None:
    with _lock:
        cached = _cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    for url, params in DOH:
        try:
            with httpx.Client(transport=transport, trust_env=False, timeout=3) as client:
                response = client.get(url, params={**params, "name": host},
                                      headers={"Accept": "application/dns-json"})
            for answer in response.json().get("Answer", []):
                if answer.get("type") == 1:
                    address = ipaddress.IPv4Address(answer["data"])
                    if address.is_global:
                        with _lock:
                            _cache[host] = (time.monotonic() + TTL, str(address))
                        return str(address)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def _targets(request: httpx.Request, host: str) -> bool:
    return request.url.scheme == "https" and request.url.host == host


def _pin(request: httpx.Request, host: str, address: str | None) -> None:
    if address:
        # Host header was fixed when the request was built; SNI/cert check stay on the name.
        request.url = request.url.copy_with(host=address)
        request.extensions = {**request.extensions, "sni_hostname": host}


class PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, base_url: str, *, resolver=doh_address, **kwargs):
        super().__init__(**kwargs)
        self.host, self.resolver = urlsplit(base_url).hostname, resolver

    async def handle_async_request(self, request):
        if _targets(request, self.host):
            _pin(request, self.host, await asyncio.to_thread(self.resolver, self.host))
        return await super().handle_async_request(request)


class PinnedSyncTransport(httpx.HTTPTransport):
    def __init__(self, base_url: str, *, resolver=doh_address, **kwargs):
        super().__init__(**kwargs)
        self.host, self.resolver = urlsplit(base_url).hostname, resolver

    def handle_request(self, request):
        if _targets(request, self.host):
            _pin(request, self.host, self.resolver(self.host))
        return super().handle_request(request)
