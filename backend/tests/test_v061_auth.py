import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.auth.store import CredentialStore
from app.sources.canvas_client import CanvasAPIError, CanvasClient
from app.sources.canvas_routing import RoutedCanvasClient


def store():
    result = CredentialStore()
    result.set_canvas("test", "https://canvas.example", "fixture-pat")
    result.set_canvas_session("test", "https://canvas.example", {"session": "fixture-session"})
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_mode,standby_mode", [("pat", "browser_session"), ("browser_session", "pat")])
async def test_concurrent_failover_retries_each_read_once_without_probe_storm(failed_mode, standby_mode):
    credentials, seen = store(), Counter()
    credentials.select_canvas_mode("test", failed_mode)
    async def handler(request):
        mode = "pat" if request.headers.get("authorization") else "browser_session"
        assert not (request.headers.get("authorization") and request.headers.get("cookie"))
        seen[(mode, request.url.path)] += 1
        await asyncio.sleep(0)
        return httpx.Response(401, json={}) if mode == failed_mode else httpx.Response(200, json={"id": 42, "name": "Fixture"})
    client = RoutedCanvasClient(credentials, "test", 42, transport=httpx.MockTransport(handler))
    results = await asyncio.gather(*(client.get_json("/api/v1/courses") for _ in range(8)))
    assert len(results) == 8
    assert seen[(failed_mode, "/api/v1/users/self/profile")] == 1
    assert seen[(standby_mode, "/api/v1/users/self/profile")] == 0  # recently verified standby
    assert seen[(standby_mode, "/api/v1/courses")] == 8
    assert credentials.slot_status("test", failed_mode)["state"] == "INVALID"
    assert credentials.get_canvas("test").auth_mode == standby_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_resource_permission_and_transient_errors_do_not_cycle(status):
    credentials, seen = store(), []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"id": 42, "name": "Fixture"})
        return httpx.Response(status, json={}, headers={"retry-after": "0"})
    async def no_sleep(_): pass
    client = RoutedCanvasClient(credentials, "test", 42, transport=httpx.MockTransport(handler), sleep=no_sleep)
    with pytest.raises(CanvasAPIError):
        await client.get_json("/api/v1/courses/locked")
    assert all(row.headers.get("authorization") for row in seen)
    assert credentials.canvas_slot("test", "pat") is not None
    assert credentials.canvas_slot("test", "browser_session") is not None


@pytest.mark.asyncio
async def test_late_failure_cannot_remove_replacement():
    credentials = store()
    async def handler(request):
        credentials.set_canvas("test", "https://canvas.example", "new-fixture")
        return httpx.Response(401, json={})
    client = RoutedCanvasClient(credentials, "test", 42, transport=httpx.MockTransport(handler))
    with pytest.raises(CanvasAPIError):
        await client.get_json("/api/v1/courses")
    assert credentials.canvas_slot("test", "pat").token == "new-fixture"


@pytest.mark.asyncio
async def test_external_redirect_is_stopped_before_request_and_download_is_anonymous():
    credentials, seen = store(), []
    def handler(request):
        seen.append(request)
        if request.url.host == "canvas.example":
            return httpx.Response(302, headers={"location": "https://files.example/file"})
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=b"fixture", headers={"content-type": "application/pdf"})
    client = CanvasClient.from_credential(credentials.canvas_slot("test", "browser_session"), transport=httpx.MockTransport(handler))
    with pytest.raises(CanvasAPIError):
        await client.get_json("/api/v1/users/self/profile")
    assert len(seen) == 1
    assert (await client.download("/files/1/download")).content == b"fixture"
    assert len(seen) == 3


def test_remove_pat_retains_session_and_blocks_environment_reload():
    credentials = store()
    credentials.remove_canvas_slot("test", "pat", state="DISABLED")
    assert credentials.canvas_blocked("test")
    assert credentials.get_canvas("test").auth_mode == "browser_session"
    credentials.set_canvas_session("test", "https://canvas.example", {"session": "replacement"})
    assert credentials.canvas_blocked("test")
    credentials.set_canvas("test", "https://canvas.example", "replacement")
    assert not credentials.canvas_blocked("test")


def test_routing_serializes_different_event_loops():
    credentials, seen = store(), Counter()
    async def cycle():
        async def handler(request):
            mode = "pat" if request.headers.get("authorization") else "session"
            seen[(mode, request.url.path)] += 1
            await asyncio.sleep(0.02)
            return httpx.Response(401, json={}) if mode == "pat" else httpx.Response(200, json={"id": 42, "name": "Fixture"})
        client = RoutedCanvasClient(credentials, "test", 42, transport=httpx.MockTransport(handler))
        return await asyncio.gather(*(client.get_json("/api/v1/courses") for _ in range(3)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(asyncio.run, cycle()) for _ in range(2)]
        assert all(len(job.result(timeout=10)) == 3 for job in jobs)
    assert seen[("pat", "/api/v1/users/self/profile")] == 1


def test_stale_removal_preserves_replacement():
    credentials = store()
    generation = credentials.generation("test", "pat")
    credentials.set_canvas("test", "https://canvas.example", "new-pat")
    assert not credentials.remove_canvas_slot("test", "pat", generation, state="DISABLED")
    assert credentials.canvas_slot("test", "pat").token == "new-pat"
    assert credentials.slot_status("test", "pat")["state"] == "VALID"
