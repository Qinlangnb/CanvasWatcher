"""Stream a signed gateway request to exactly one private tenant socket."""

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from aw_gateway.config import GatewayConfig, load_tenant_token

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_DROP_REQUEST = _HOP_HEADERS | {
    "host", "cookie", "authorization", "content-length", "x-aw-internal-token",
    "x-aw-user", "x-aw-slot", "x-aw-identity", "x-aw-csrf",
}
_DROP_RESPONSE = _HOP_HEADERS | {"set-cookie", "content-length"}
_DROP_BUFFERED_RESPONSE = _DROP_RESPONSE | {"content-encoding"}
_DROP_REQUEST_BYTES = {item.encode("ascii") for item in _DROP_REQUEST}


def _target(config: GatewayConfig, slot: int):
    socket = config.socket_path(slot)
    token_file = config.token_path(slot)
    if not socket.is_socket():
        return None
    try:
        token = load_tenant_token(token_file)
    except (OSError, RuntimeError):
        return None
    return socket, token


def _headers(request: Request, config: GatewayConfig, token: bytes):
    headers = [
        (key, value)
        for key, value in request.scope["headers"]
        if key.lower() not in _DROP_REQUEST_BYTES and not key.lower().startswith(b"x-forwarded-")
    ]
    headers.extend(
        [
            (b"host", config.public_origin.split("://", 1)[1].encode("ascii")),
            (b"x-forwarded-proto", b"https" if not config.development else b"http"),
            (b"x-aw-internal-token", token),
        ]
    )
    return headers


def _url(request: Request, config: GatewayConfig, *, query: str | None = None) -> str:
    path = request.url.path.removeprefix(config.base_path)
    if not path.startswith("/api/"):
        raise ValueError("Invalid tenant API path")
    value = f"http://tenant{path}"
    effective_query = request.url.query if query is None else query
    if effective_query:
        value += "?" + effective_query
    return value


class TenantUnavailable(RuntimeError):
    pass


async def buffered_tenant_request(
    request: Request, *, config: GatewayConfig, slot: int, query: str | None = None,
    body_override: bytes | None = None, max_body: int = 32 * 1024,
) -> Response:
    """For small OAuth start/callback responses that must be rewritten by the gateway."""
    target = _target(config, slot)
    if target is None:
        raise TenantUnavailable
    socket, token = target
    body = body_override if body_override is not None else await request.body()
    if len(body) > max_body:
        raise ValueError("Tenant request too large")
    try:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)), timeout=35) as client:
            upstream = await client.request(
                request.method, _url(request, config, query=query),
                headers=_headers(request, config, token), content=body, follow_redirects=False,
            )
    except (httpx.HTTPError, OSError) as error:
        raise TenantUnavailable from error
    if len(upstream.content) > 256 * 1024:
        raise TenantUnavailable
    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in _DROP_BUFFERED_RESPONSE
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


async def proxy_to_tenant(request: Request, *, config: GatewayConfig, slot: int):
    target = _target(config, slot)
    if target is None:
        return JSONResponse({"detail": "INSTANCE_NOT_READY"}, status_code=503)
    socket, token = target

    try:
        url = _url(request, config)
    except ValueError:
        return JSONResponse({"detail": "INVALID_API_PATH"}, status_code=404)
    headers = _headers(request, config, token)
    client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)), timeout=1500)
    try:
        outgoing = client.build_request(
            request.method,
            url,
            headers=headers,
            content=request.stream() if request.method not in {"GET", "HEAD"} else None,
        )
        upstream = await client.send(outgoing, stream=True, follow_redirects=False)
    except (httpx.HTTPError, OSError):
        await client.aclose()
        return JSONResponse({"detail": "INSTANCE_UNAVAILABLE"}, status_code=503)

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _DROP_RESPONSE
    }
    return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers)
