import httpx
import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.fetchers import AnonymousFetcher, NtlmFetcher, ResourceFetcher
from app.auth.models import AuthRule, FetchStatus
from app.auth.scope import in_auth_scope
from app.auth.store import CredentialStore
from app.config import Settings
from app.db import Base, CredentialProfile
from app.services.auth import AuthService
from app.sources.website import WebsiteAdapter

BASE = "https://courses.example.edu/phys225/fall/"


@pytest.mark.parametrize("url", [
    "https://evil.example/phys225/fall/secure/a",
    "http://courses.example.edu/phys225/fall/secure/a",
    "https://courses.example.edu:444/phys225/fall/secure/a",
    BASE + "../other/a", BASE + "%2e%2e/other/a", BASE + "%252e%252e/other/a",
    BASE + "secure/../../other/a", BASE + "secure\\a",
    "https://courses.example.edu/phys225/fall-other/a",
    "https://user:password@courses.example.edu/phys225/fall/a",
])
def test_scope_rejects_origin_path_and_encoding_escapes(url):
    assert not in_auth_scope(url, BASE, "/phys225/fall/")


def test_legacy_rule_never_matches_other_host_or_adjacent_path():
    adapter = WebsiteAdapter(name="site", course_code="PHYS225", base_url=BASE,
        auth_rules=[{"path_prefix": "/phys225/fall/secure", "auth": {
            "type": "ntlm", "credential_id": "site", "probe_url": BASE + "secure/a"}}])
    assert adapter.auth_rule_for(BASE + "secure/a") is not None
    assert adapter.auth_rule_for(BASE + "secure-other/a") is None
    assert adapter.auth_rule_for("https://evil.example/phys225/fall/secure/a") is None
    assert in_auth_scope("https://courses.example.edu:443/phys225/fall/a", BASE, "/phys225/fall/")


class SessionStub:
    def __init__(self, status=200):
        self.auth = None
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response.url = url
        response._content = b"protected"
        response.headers["Location"] = "https://evil.example/steal"
        return response

    def close(self):
        pass


def fetcher_for(store, handler, session):
    return ResourceFetcher(store=store,
        anonymous=AnonymousFetcher(httpx.MockTransport(handler)),
        ntlm=NtlmFetcher(session_factory=lambda: session))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,challenge,calls", [(200, "", 0), (401, "Basic", 0), (401, "NTLM", 1), (401, "Negotiate", 1), (401, 'Basic realm="ntlm"', 0)])
async def test_only_in_scope_ntlm_challenge_uses_password(status, challenge, calls):
    store, session = CredentialStore(), SessionStub()
    store.set_ntlm("site", "example", "test-only")
    fetcher = fetcher_for(store, lambda request: httpx.Response(
        status, headers={"WWW-Authenticate": challenge}), session)
    result = await fetcher.fetch(BASE + "a", AuthRule(path_prefix="/phys225/fall/",
        auth_type="ntlm", credential_id="site", probe_url=BASE, base_url=BASE))
    assert len(session.calls) == calls
    assert result.authenticated is bool(calls)


@pytest.mark.asyncio
async def test_anonymous_redirect_does_not_authorize_external_ntlm():
    store, session = CredentialStore(), SessionStub()
    store.set_ntlm("site", "example", "test-only")
    def handler(request):
        if request.url.host == "courses.example.edu":
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
    result = await fetcher_for(store, handler, session).fetch(BASE, AuthRule(
        path_prefix="/phys225/fall/", auth_type="ntlm", credential_id="site", probe_url=BASE))
    assert result.error == "auth_scope_rejected"
    assert not session.calls


@pytest.mark.asyncio
async def test_authenticated_session_never_follows_redirect():
    store, session = CredentialStore(), SessionStub(302)
    store.set_ntlm("site", "example", "test-only")
    fetcher = fetcher_for(store, lambda request: httpx.Response(
        401, headers={"WWW-Authenticate": "NTLM"}), session)
    result = await fetcher.fetch(BASE, AuthRule(path_prefix="/phys225/fall/",
        auth_type="ntlm", credential_id="site", probe_url=BASE))
    assert result.status == FetchStatus.ERROR
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_candidate_failure_preserves_old_credential_and_public_is_not_verified(tmp_path, public):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store, session = CredentialStore(), SessionStub(401)
    store.set_ntlm("site", "old-user", "old-test-secret")
    original = store.get_ntlm("site")
    with Session(engine) as db:
        profile = CredentialProfile(credential_id="site", auth_type="ntlm",
            display_name="Site", probe_url=BASE, username="old-user", state="ACTIVE",
            metadata_json={"base_url": BASE})
        db.add(profile)
        db.commit()
        fetcher = fetcher_for(store, lambda request: httpx.Response(
            200 if public else 401, headers={"WWW-Authenticate": "NTLM"}), session)
        service = AuthService(Settings(courses_config=tmp_path / "absent"), store, fetcher)
        if not public:
            with pytest.raises(ValueError, match="existing credential kept"):
                await service.set_and_verify(db, profile, "new-user", "new-test-secret")
            assert store.get_ntlm("site") is original
            assert profile.state == "ACTIVE" and profile.username == "old-user"
        else:
            assert not await service.set_and_verify(db, profile, "old-user", "")
            assert store.get_ntlm("site") is original
            assert profile.state == "AUTH_REQUIRED"
            assert profile.last_error_code == "protected_access_not_verified"
            assert not session.calls
        with pytest.raises(ValueError, match="no matching credential"):
            await service.set_and_verify(db, profile, "other-user", "")


def test_no_prefix_source_has_course_scoped_challenge_rule():
    from app.db import CourseSource
    from app.services.source_connections import create_website_connection
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        connection = create_website_connection(db, {"course_name": "Test", "course_code": "TEST",
            "base_url": BASE, "authentication_method": "ntlm", "protected_path_prefix": " "})
        source = db.get(CourseSource, connection.config_json["course_source_id"])
        adapter = WebsiteAdapter(name="test", course_code="TEST", base_url=BASE,
                                 auth_rules=source.config_json["auth_rules"])
        rule = adapter.auth_rule_for(BASE + "secure/a")
        assert rule and rule.credential_id == connection.credential_id
        assert adapter.auth_rule_for("https://courses.example.edu/other/a") is None


@pytest.mark.parametrize("base,prefix,probe", [
    (BASE.replace("https:", "http:"), None, None),
    (BASE, "/", None), (BASE, None, "https://evil.example/test"),
    (BASE, None, BASE + "%2e%2e/test"),
])
def test_ntlm_configuration_rejects_invalid_scope_before_writes(base, prefix, probe):
    from sqlalchemy import func, select

    from app.db import SourceConnection
    from app.services.source_connections import create_website_connection
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(ValueError):
            create_website_connection(db, {"course_name": "Test", "base_url": base,
                "authentication_method": "ntlm", "protected_path_prefix": prefix, "probe_url": probe})
        assert db.scalar(select(func.count()).select_from(SourceConnection)) == 0
