import asyncio

import httpx
from fastapi import FastAPI, Request

from app.auth.internal_gateway import InternalGatewayAuth


def test_internal_gateway_rejects_direct_calls_and_strips_capability():
    app = FastAPI()

    @app.get("/api/files/1/content")
    def content(request: Request):
        return {"headers": sorted(request.headers.keys())}

    guarded = InternalGatewayAuth(app, token=b"a" * 48)

    async def run():
        transport = httpx.ASGITransport(app=guarded)
        async with httpx.AsyncClient(transport=transport, base_url="http://tenant") as client:
            direct = await client.get("/api/files/1/content")
            assert direct.status_code == 403
            assert (await client.get("/api/files/1/content", headers={"x-aw-internal-token": "bad"})).status_code == 403
            with_cookie = await client.get(
                "/api/files/1/content",
                headers={"x-aw-internal-token": "a" * 48, "Cookie": "aw_session=secret"},
            )
            assert with_cookie.status_code == 403
            valid = await client.get(
                "/api/files/1/content", headers={"x-aw-internal-token": "a" * 48}
            )
            assert valid.status_code == 200
            assert "x-aw-internal-token" not in valid.json()["headers"]
            assert "cookie" not in valid.json()["headers"]

    asyncio.run(run())


def test_internal_gateway_rejects_duplicate_tokens():
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    guarded = InternalGatewayAuth(app, token=b"b" * 48)

    async def run():
        transport = httpx.ASGITransport(app=guarded)
        async with httpx.AsyncClient(transport=transport, base_url="http://tenant") as client:
            response = await client.get(
                "/api/health",
                headers=[("x-aw-internal-token", "b" * 48), ("X-AW-Internal-Token", "b" * 48)],
            )
            assert response.status_code == 403

    asyncio.run(run())
