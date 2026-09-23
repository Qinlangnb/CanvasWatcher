from pathlib import Path

import httpx
import pytest
import requests
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

from app.auth.fetchers import AnonymousFetcher, NtlmFetcher, ResourceFetcher
from app.auth.models import AuthRule, FetchResult, FetchStatus, NtlmCredential
from app.auth.store import CredentialStore
from app.config import Settings
from app.db import (
    Base,
    Course,
    CredentialProfile,
    DownloadedFile,
    PendingResource,
    SourceItem,
)
from app.services.auth import AuthService
from app.services.sync import SyncService
from app.sources.website import WebsiteAdapter

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://courses.physics.illinois.edu/phys225/fa2026/"
AUTH_RULES = [
    {
        "path_prefix": "/phys225/fa2026/secure/",
        "auth": {
            "type": "ntlm",
            "credential_id": "uiuc_netid",
            "probe_url": f"{BASE_URL}secure/office-hours.html",
        },
    }
]


@pytest.mark.asyncio
async def test_detects_ntlm_challenge() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, headers={"WWW-Authenticate": "NTLM"}, request=request
        )

    fetcher = AnonymousFetcher(httpx.MockTransport(handler))
    result = await fetcher.fetch(f"{BASE_URL}secure/office-hours.html")
    assert result.status is FetchStatus.AUTH_REQUIRED
    assert result.auth_scheme == "NTLM"


class FakeResponse:
    def __init__(self, status: int, content: bytes, content_type: str) -> None:
        self.status_code = status
        self.content = content
        self.url = f"{BASE_URL}secure/resource"
        self.headers = requests.structures.CaseInsensitiveDict(
            {"Content-Type": content_type}
        )


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.auth = None
        self.response = response

    def get(self, url: str, **kwargs) -> FakeResponse:
        del url, kwargs
        return self.response

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_successful_ntlm_fetch_is_normalized() -> None:
    store = CredentialStore()
    store.set_ntlm("uiuc_netid", "uofi\\example", "not-a-real-password")
    session = FakeSession(FakeResponse(200, b"<html>protected</html>", "text/html"))
    fetcher = ResourceFetcher(
        store=store, ntlm=NtlmFetcher(session_factory=lambda: session),
        anonymous=AnonymousFetcher(httpx.MockTransport(
            lambda request: httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
        )),
    )
    rule = AuthRule(
        path_prefix="/phys225/fa2026/secure/",
        auth_type="ntlm",
        credential_id="uiuc_netid",
        probe_url=f"{BASE_URL}secure/office-hours.html",
    )
    result = await fetcher.fetch(rule.probe_url, rule)
    assert result.status is FetchStatus.OK
    assert result.content_type == "text/html"
    assert result.content == b"<html>protected</html>"


class RoutingFetcher:
    def __init__(self, protected_status: FetchStatus = FetchStatus.AUTH_REQUIRED):
        self.protected_status = protected_status
        self.schedule = (FIXTURES / "phys225_public_schedule.html").read_bytes()
        self.office = (FIXTURES / "phys225_protected_office_hours.html").read_bytes()
        self.pdf = (FIXTURES / "fake_protected.pdf").read_bytes()

    async def fetch(self, url: str, auth_rule=None) -> FetchResult:
        if url.endswith("schedule.html"):
            return FetchResult(
                status=FetchStatus.OK,
                url=url,
                status_code=200,
                content=self.schedule,
                content_type="text/html",
            )
        if "/secure/" in url and self.protected_status is not FetchStatus.OK:
            return FetchResult(
                status=self.protected_status,
                url=url,
                status_code=401,
                auth_scheme="NTLM",
            )
        if url.endswith(".pdf"):
            return FetchResult(
                status=FetchStatus.OK,
                url=url,
                status_code=200,
                content=self.pdf,
                content_type="application/pdf",
            )
        return FetchResult(
            status=FetchStatus.OK,
            url=url,
            status_code=200,
            content=self.office,
            content_type="text/html",
        )


def make_adapter(fetcher: RoutingFetcher) -> WebsiteAdapter:
    return WebsiteAdapter(
        name="phys225_fa2026",
        course_code="PHYS225",
        base_url=BASE_URL,
        public_pages=["schedule.html"],
        auth_rules=AUTH_RULES,
        discovery={
            "same_origin_only": True,
            "allowed_path_prefixes": ["/phys225/fa2026/"],
            "max_depth": 1,
        },
        fetcher=fetcher,
    )


@pytest.mark.asyncio
async def test_missing_credential_records_protected_links_without_losing_public_page() -> None:
    course = Course(
        id=1, source="website", external_id="configured:PHYS225", course_code="PHYS225", name="Physics 225"
    )
    items = await make_adapter(RoutingFetcher()).fetch_items(course)
    assert any(item.url and item.url.endswith("schedule.html") and item.fetch_status == "ok" for item in items)
    protected = [item for item in items if item.url and "/secure/" in item.url]
    assert len(protected) == 2
    assert all(item.fetch_status == "auth_required" for item in protected)
    assert all(item.credential_id == "uiuc_netid" for item in protected)


@pytest.mark.asyncio
async def test_failed_protected_auth_does_not_stop_public_monitoring() -> None:
    course = Course(
        id=1,
        source="website",
        external_id="configured:PHYS225",
        course_code="PHYS225",
        name="Physics 225",
    )
    items = await make_adapter(RoutingFetcher(FetchStatus.AUTH_FAILED)).fetch_items(course)
    assert any(item.fetch_status == "ok" for item in items)
    assert any(item.fetch_status == "auth_failed" for item in items)


@pytest.mark.asyncio
async def test_protected_html_and_pdf_use_existing_pipeline(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(
            source="website", external_id="configured:PHYS225", course_code="PHYS225", name="Physics 225"
        )
        db.add(course)
        db.commit()
        service = SyncService(
            Settings(database_url="sqlite:///:memory:", download_root=tmp_path)
        )
        state = await service.run(
            db, [(course, make_adapter(RoutingFetcher(FetchStatus.OK)))]
        )
        assert state.state == "healthy"
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1
        assert db.scalar(
            select(func.count())
            .select_from(SourceItem)
            .where(SourceItem.item_type == "website_page")
        ) == 3


def test_secret_has_safe_repr_and_no_persistent_password_column() -> None:
    credential = NtlmCredential("uofi\\example", "not-a-real-password")
    representation = repr(credential)
    assert "not-a-real-password" not in representation
    assert "password" not in {column.name for column in inspect(Base.metadata.tables["credential_profiles"]).columns}


class RejectingFetcher:
    class NtlmSessions:
        def invalidate(self, credential_id: str) -> None:
            self.invalidated = credential_id

    def __init__(self) -> None:
        self.ntlm = self.NtlmSessions()

    async def fetch(self, url: str, auth_rule=None) -> FetchResult:
        del auth_rule
        return FetchResult(
            status=FetchStatus.AUTH_FAILED,
            url=url,
            status_code=401,
            auth_scheme="NTLM",
        )


@pytest.mark.asyncio
async def test_failed_credential_is_removed_from_memory(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = CredentialStore()
    with Session(engine) as db:
        profile = CredentialProfile(
            credential_id="uiuc_netid",
            auth_type="ntlm",
            display_name="PHYS225",
            probe_url=f"{BASE_URL}secure/office-hours.html",
            state="AUTH_REQUIRED",
        )
        db.add(profile)
        db.commit()
        service = AuthService(
            Settings(database_url="sqlite:///:memory:", courses_config=tmp_path / "none"),
            store=store,
            fetcher=ResourceFetcher(
                store=store,
                anonymous=AnonymousFetcher(httpx.MockTransport(
                    lambda request: httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
                )),
                ntlm=NtlmFetcher(session_factory=lambda: FakeSession(
                    FakeResponse(401, b"", "text/html")
                )),
            ),
        )
        verified = await service.set_and_verify(
            db, profile, "uofi\\example", "not-a-real-password"
        )
        assert not verified
        assert profile.state == "FAILED"
        assert store.get_ntlm("uiuc_netid") is None


@pytest.mark.asyncio
async def test_requeue_processes_pending_immediately(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(
            source="website", external_id="configured:PHYS225", course_code="PHYS225", name="Physics 225"
        )
        db.add(course)
        db.flush()
        pending = PendingResource(
            course_id=course.id,
            source_name="phys225_fa2026",
            url=f"{BASE_URL}secure/lecture-01.pdf",
            title="Lecture 01 notes",
            item_type="website_file",
            credential_id="uiuc_netid",
            state="AUTH_REQUIRED",
        )
        db.add(pending)
        db.commit()
        service = SyncService(
            Settings(database_url="sqlite:///:memory:", download_root=tmp_path)
        )

        async def adapters(_db):
            return [(course, make_adapter(RoutingFetcher(FetchStatus.OK)))]

        service.default_adapters = adapters
        count = await service.retry_pending_resources(db, "uiuc_netid")
        db.refresh(pending)
        assert count == 1
        assert pending.state == "FETCHED"
