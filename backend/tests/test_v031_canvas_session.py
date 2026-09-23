from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth.canvas_session import (
    CanvasSessionImportError,
    parse_canvas_session_import,
)
from app.auth.models import (
    CanvasAuthMode,
    CanvasBrowserSessionCredential,
    CanvasCredential,
)
from app.auth.store import CredentialStore, credential_store
from app.config import Settings
from app.db import (
    Base,
    ChangeEvent,
    Course,
    CourseSource,
    CredentialProfile,
    Notification,
    SourceItem,
    SourceSnapshot,
)
from app.logging import redact_secrets
from app.schemas import CredentialProfileOut, DiscoveredCourse
from app.services.auth import AuthService
from app.services.sync import SyncService
from app.sources.canvas import CanvasAdapter
from app.sources.canvas_client import CanvasAPIError, CanvasClient, CanvasErrorCode


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        download_root=tmp_path / "downloads",
        courses_config=tmp_path / "courses.yaml",
        file_rules_config=tmp_path / "rules.yaml",
        availability_config=tmp_path / "availability.yaml",
        scheduler_enabled=False,
        **overrides,
    )


def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a=1; b=2", {"a": "1", "b": "2"}),
        ("Cookie: a=1; b=2", {"a": "1", "b": "2"}),
        (
            "curl 'https://canvas.illinois.edu/api/v1/users/self/profile' "
            "-H 'Accept: application/json' -H 'Cookie: a=1; b=two'",
            {"a": "1", "b": "two"},
        ),
        (
            'curl ^"https://canvas.illinois.edu/api/v1/users/self/profile^" ^\r\n'
            '  -H ^"accept: application/json^" ^\r\n'
            '  -H ^"Cookie: _csrf_token=safe-csrf; canvas_session=safe-session^"',
            {"_csrf_token": "safe-csrf", "canvas_session": "safe-session"},
        ),
        (
            'curl.exe ^"https://canvas.illinois.edu/api/v1/users/self/profile^" '
            '-b ^"a=1; b=2^"',
            {"a": "1", "b": "2"},
        ),
    ],
)
def test_session_import_formats(value: str, expected: dict[str, str]) -> None:
    assert parse_canvas_session_import(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "curl https://canvas.illinois.edu/api/v1/users/self/profile", "broken", "a"],
)
def test_session_import_missing_or_malformed_cookie_is_safe(value: str) -> None:
    with pytest.raises(CanvasSessionImportError) as captured:
        parse_canvas_session_import(value)
    assert str(captured.value) in {
        "Canvas Cookie data is required",
        "No Cookie header found in session import",
    }


def test_session_import_never_executes_pasted_text(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    payload = (
        "curl https://canvas.illinois.edu/api/v1/users/self/profile "
        f"-H 'Cookie: a=$(touch {marker})'"
    )
    assert parse_canvas_session_import(payload)["a"].startswith("$(touch")
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("external_host", ["example.com", "login.illinois.edu"])
async def test_browser_session_cookie_is_host_scoped_on_file_redirect(
    external_host: str,
) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("cookie")))
        if request.url.host == "canvas.illinois.edu" and request.url.path == "/file/7":
            return httpx.Response(
                302,
                headers={"Location": f"https://{external_host}/download/7"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"safe file bytes",
            headers={"Content-Type": "application/pdf"},
            request=request,
        )

    credential = CanvasBrowserSessionCredential(
        "https://canvas.illinois.edu", {"canvas_session": "session-secret"}
    )
    client = CanvasClient.from_credential(
        credential, transport=httpx.MockTransport(handler)
    )
    result = await client.download("https://canvas.illinois.edu/file/7")
    assert result.content == b"safe file bytes"
    assert seen[0] == (
        "canvas.illinois.edu",
        "canvas_session=session-secret",
    )
    assert seen[1] == (external_host, None)
    assert "session-secret" not in repr(client)


@pytest.mark.asyncio
async def test_browser_session_profile_redirect_to_sso_is_auth_required() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("cookie")))
        if request.url.host == "canvas.illinois.edu":
            return httpx.Response(
                302,
                headers={"Location": "https://login.illinois.edu/sso"},
                request=request,
            )
        return httpx.Response(
            200,
            text="<html>Sign in</html>",
            headers={"Content-Type": "text/html"},
            request=request,
        )

    client = CanvasClient.from_credential(
        CanvasBrowserSessionCredential(
            "https://canvas.illinois.edu", {"canvas_session": "session-secret"}
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CanvasAPIError) as captured:
        await client.probe()
    assert captured.value.code is CanvasErrorCode.AUTH_REQUIRED
    assert seen == [
        ("canvas.illinois.edu", "canvas_session=session-secret"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "expected"),
    [
        (401, "application/json", CanvasErrorCode.AUTH_REQUIRED),
        (403, "application/json", CanvasErrorCode.PERMISSION_DENIED),
        (200, "text/html", CanvasErrorCode.INVALID_RESPONSE),
    ],
)
async def test_browser_session_strict_profile_verification(
    status: int, content_type: str, expected: CanvasErrorCode
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text="<html>Login</html>" if content_type == "text/html" else "{}",
            headers={"Content-Type": content_type},
            request=request,
        )

    client = CanvasClient.from_credential(
        CanvasBrowserSessionCredential(
            "https://canvas.illinois.edu", {"canvas_session": "safe-session"}
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CanvasAPIError) as captured:
        await client.probe()
    assert captured.value.code is expected


class SessionProbeClient:
    result = {"id": 42, "name": "Example Student", "login_id": "student"}
    error: CanvasAPIError | None = None

    def __init__(self, *args, credential=None, **kwargs):
        del args, kwargs
        assert isinstance(credential, CanvasBrowserSessionCredential)

    async def probe(self):
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_browser_session_is_active_memory_only_and_secret_safe(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.services.auth.CanvasClient", SessionProbeClient)
    store = CredentialStore()
    secret = "unique-browser-session-secret"
    with memory_db() as db:
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.illinois.edu/api/v1/users/self/profile",
            metadata_json={},
            state="AUTH_REQUIRED",
        )
        db.add(profile)
        db.commit()
        service = AuthService(settings(tmp_path), store=store)
        verified = await service.set_canvas_session_and_verify(
            db,
            profile,
            "https://canvas.illinois.edu",
            {"canvas_session": secret, "_csrf_token": "safe-csrf"},
        )
        assert verified
        assert profile.state == "ACTIVE"
        assert profile.metadata_json["auth_mode"] == "browser_session"
        assert store.canvas_mode("canvas_uiuc") is CanvasAuthMode.BROWSER_SESSION
        serialized = CredentialProfileOut.model_validate(profile).model_dump_json()
        database_row = str(
            db.execute(
                select(
                    CredentialProfile.username,
                    CredentialProfile.metadata_json,
                    CredentialProfile.last_error_code,
                )
            ).one()
        )
        assert secret not in serialized
        assert secret not in database_row
        assert "canvas_session" not in serialized
        assert "_csrf_token" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "state"),
    [
        (CanvasErrorCode.AUTH_REQUIRED, "AUTH_REQUIRED"),
        (CanvasErrorCode.PERMISSION_DENIED, "FAILED"),
    ],
)
async def test_browser_session_auth_failure_stops_retries_and_preserves_data(
    tmp_path: Path, monkeypatch, code: CanvasErrorCode, state: str
) -> None:
    class FailingSessionProbe(SessionProbeClient):
        error = CanvasAPIError(code, "safe failure")

    monkeypatch.setattr("app.services.auth.CanvasClient", FailingSessionProbe)
    store = CredentialStore()
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:1",
            course_code="TEST 101",
            name="Existing Course",
            active=True,
        )
        profile = CredentialProfile(
            credential_id="canvas_uiuc",
            auth_type="canvas_token",
            display_name="Canvas",
            probe_url="https://canvas.illinois.edu/api/v1/users/self/profile",
            metadata_json={},
            state="ACTIVE",
        )
        db.add_all([course, profile])
        db.commit()
        service = AuthService(settings(tmp_path), store=store)
        assert not await service.set_canvas_session_and_verify(
            db,
            profile,
            "https://canvas.illinois.edu",
            {"canvas_session": "expired-session"},
        )
        assert profile.state == state
        assert store.get_canvas("canvas_uiuc") is None
        assert store.canvas_mode("canvas_uiuc") is CanvasAuthMode.BROWSER_SESSION
        assert db.scalar(select(func.count(Course.id))) == 1
        assert db.get(Course, course.id).name == "Existing Course"


def test_stored_browser_form_preference_does_not_disable_environment_pat(
    tmp_path: Path,
) -> None:
    store = CredentialStore()
    with memory_db() as db:
        db.add(
            CredentialProfile(
                credential_id="canvas_uiuc",
                auth_type="canvas_token",
                display_name="Canvas",
                probe_url="https://canvas.illinois.edu/api/v1/users/self/profile",
                metadata_json={"auth_mode": "browser_session"},
                state="ACTIVE",
            )
        )
        db.commit()
        service = AuthService(
            settings(tmp_path, canvas_base_url="https://canvas.illinois.edu", canvas_access_token=SecretStr("environment-token")),
            store=store,
        )
        service.ensure_profiles(db)
        profile = db.scalar(
            select(CredentialProfile).where(
                CredentialProfile.credential_id == "canvas_uiuc"
            )
        )
        assert profile.state == "AUTH_REQUIRED"
        assert service.canvas_credential() is not None
        assert service.canvas_credential().auth_mode is CanvasAuthMode.PAT
        # Eligible does not mean verified. Explicit removal still disables it.
        service.remove_canvas_method(db, profile, "pat")
        assert service.canvas_credential() is None


@pytest.mark.asyncio
async def test_runtime_session_expiry_preserves_items_and_deduplicates_notice(
    tmp_path: Path,
) -> None:
    credential_store.clear_all()
    credential_store.set_canvas_session(
        "canvas_uiuc",
        "https://canvas.illinois.edu",
        {"canvas_session": "expiring-session"},
    )
    try:
        with memory_db() as db:
            course = Course(
                source="canvas",
                external_id="canvas:1",
                course_code="TEST 101",
                name="Existing Course",
                active=True,
            )
            profile = CredentialProfile(
                credential_id="canvas_uiuc",
                auth_type="canvas_token",
                display_name="Canvas",
                probe_url="https://canvas.illinois.edu/api/v1/users/self/profile",
                metadata_json={"auth_mode": "browser_session"},
                state="ACTIVE",
            )
            db.add_all([course, profile])
            db.flush()
            source = CourseSource(
                course_id=course.id,
                name="canvas:1",
                source_type="canvas",
                external_id="1",
                url="https://canvas.illinois.edu/courses/1",
                enabled=True,
                config_json={"baseline_complete": True},
            )
            item = SourceItem(
                course_id=course.id,
                source_type="canvas",
                source_name="canvas:1",
                external_id="assignment:1",
                item_type="assignment",
                title="Existing Homework",
                url="https://canvas.illinois.edu/courses/1/assignments/1",
                current_hash="existing",
            )
            db.add_all([source, item])
            db.flush()
            item.course_source_id = source.id
            db.commit()
            adapter = CanvasAdapter(
                "https://canvas.illinois.edu",
                credential=CanvasBrowserSessionCredential(
                    "https://canvas.illinois.edu",
                    {"canvas_session": "expiring-session"},
                ),
                canvas_course_id="1",
                course_source_id=source.id,
            )

            async def expired(_course, since=None):
                del since
                # V0.6.1 transport owns confirmed generation-specific expiry.
                credential_store.remove_canvas_slot("canvas_uiuc", "browser_session")
                adapter.errors = {"assignment": "auth_required"}
                adapter.successful_item_types = set()
                return []

            adapter.fetch_items = expired
            service = SyncService(settings(tmp_path))
            await service.run(db, [(course, adapter)])
            await service.run(db, [(course, adapter)])
            db.refresh(item)
            db.refresh(profile)
            assert not item.is_deleted
            assert profile.state == "AUTH_REQUIRED"
            assert credential_store.get_canvas("canvas_uiuc") is None
            assert db.scalar(select(func.count(Notification.id))) == 1
    finally:
        credential_store.clear_all()


@pytest.mark.asyncio
async def test_switching_auth_modes_reuses_canonical_canvas_objects(
    tmp_path: Path, monkeypatch
) -> None:
    discovered = [
        DiscoveredCourse(
            source="canvas",
            external_id="77",
            course_code="TEST 101",
            name="Test Course",
        )
    ]

    async def discover(_self):
        return discovered

    monkeypatch.setattr(CanvasAdapter, "discover_courses", discover)
    with memory_db() as db:
        service = SyncService(settings(tmp_path))
        session_pairs = await service.discover_canvas(
            db,
            CanvasBrowserSessionCredential(
                "https://canvas.illinois.edu", {"canvas_session": "safe-session"}
            ),
        )
        course_id = session_pairs[0][0].id
        source_id = session_pairs[0][1].course_source_id
        pat_pairs = await service.discover_canvas(
            db, CanvasCredential("https://canvas.illinois.edu", "safe-token")
        )
        assert pat_pairs[0][0].id == course_id
        assert pat_pairs[0][1].course_source_id == source_id
        assert db.scalar(select(func.count(Course.id))) == 1
        assert db.scalar(select(func.count(CourseSource.id))) == 1


def test_redaction_covers_canvas_session_headers_and_values() -> None:
    secret = "redaction-secret"
    result = redact_secrets(
        None,
        "info",
        {
            "Authorization": f"Bearer {secret}",
            "headers": {
                "Cookie": f"canvas_session={secret}",
                "Set-Cookie": f"_csrf_token={secret}",
            },
            "session_import": secret,
            "event": f"Cookie: canvas_session={secret}",
        },
    )
    assert secret not in str(result)
    assert result["Authorization"] == "<redacted>"
    assert result["headers"]["Cookie"] == "<redacted>"


def test_canvas_secrets_never_enter_snapshots_or_change_events() -> None:
    secret = "snapshot-forbidden-secret"
    with memory_db() as db:
        course = Course(
            source="canvas",
            external_id="canvas:1",
            course_code="TEST 101",
            name="Test",
            active=True,
        )
        db.add(course)
        db.flush()
        item = SourceItem(
            course_id=course.id,
            source_type="canvas",
            source_name="canvas:1",
            external_id="assignment:1",
            item_type="assignment",
            title="Homework",
            url="https://canvas.illinois.edu/courses/1/assignments/1",
            current_hash="safe",
        )
        db.add(item)
        db.flush()
        snapshot = SourceSnapshot(
            source_item_id=item.id,
            content_hash="safe",
            structured_json={"id": 1, "name": "Homework"},
            normalized_text="Homework",
        )
        db.add(snapshot)
        db.flush()
        db.add(
            ChangeEvent(
                source_item_id=item.id,
                new_snapshot_id=snapshot.id,
                change_type="CREATED",
                importance="normal",
                requires_action=False,
                summary="Safe change",
            )
        )
        db.commit()
        serialized = str(
            (
                db.scalar(select(SourceSnapshot)).structured_json,
                db.scalar(select(ChangeEvent)).summary,
            )
        )
        assert secret not in serialized
        assert "canvas_session" not in serialized
