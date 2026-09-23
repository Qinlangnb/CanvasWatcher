import httpx
import pytest
import requests
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth.fetchers import AnonymousFetcher, NtlmFetcher, ResourceFetcher
from app.auth.store import CredentialStore
from app.db import Base, Course, CredentialProfile, SourceConnection
from app.schemas import NtlmCredentialIn, WebsiteSourceCreateIn
from app.services.source_connections import create_website_connection
from app.services.website_access import save_website

BASE = "https://site.example/course/"
CONFIG = {"name": "TEST Site", "course_name": "Test description", "course_code": "TEST",
          "term": "Fall 2026", "base_url": BASE, "authentication_method": "ntlm"}


class TrialSession:
    def __init__(self, status):
        self.status, self.auth = status, None

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        response = requests.Response()
        response.status_code, response.url = self.status, url
        response._content = b"protected content"
        return response

    def close(self):
        pass


@pytest.fixture
def context():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db, CredentialStore()


def fetcher(status=200, protected=True):
    visits = []
    def handle(request):
        visits.append(str(request.url))
        if request.url.path.endswith("/private"):
            return httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
        return httpx.Response(200, headers={"Content-Type": "text/html"},
            text='<a href="https://evil.example/private">ignore</a><a href="/course/private">actual</a>' if protected else "public")
    return ResourceFetcher(anonymous=AnonymousFetcher(httpx.MockTransport(handle)),
        ntlm=NtlmFetcher(session_factory=lambda: TrialSession(status))), visits


@pytest.mark.asyncio
async def test_test_connection_does_not_save_any_source_or_secret(context):
    db, store = context
    client, visits = fetcher()
    result = await save_website(db, CONFIG, NtlmCredentialIn(username="test-user", password="test-secret"),
        test_only=True, store=store, fetcher=client)
    assert result.verified
    assert db.scalar(select(func.count()).select_from(SourceConnection)) == 0
    assert db.scalar(select(func.count()).select_from(CredentialProfile)) == 0
    assert len(visits) == 2 and all("evil" not in url for url in visits)


@pytest.mark.asyncio
async def test_no_prefix_save_discovers_real_protected_link_and_keeps_secret_in_memory(context):
    db, store = context
    client, visits = fetcher()
    login = NtlmCredentialIn(username="test-user", password="test-secret")
    source = await save_website(db, CONFIG, login, store=store, fetcher=client)
    profile = db.scalar(select(CredentialProfile))
    assert profile.state == "ACTIVE" and profile.probe_url == BASE + "private"
    assert store.get_ntlm(source.credential_id).password == "test-secret"
    assert "test-secret" not in str(source.config_json) + str(profile.metadata_json)
    assert source.config_json["auth_rules"][0]["path_prefix"] == "/course/"
    assert "ntlm" not in WebsiteSourceCreateIn(**CONFIG, ntlm=login).model_dump()


@pytest.mark.asyncio
async def test_failed_new_source_auth_creates_nothing(context):
    db, store = context
    client, _ = fetcher(401)
    with pytest.raises(ValueError, match="Protected access"):
        await save_website(db, CONFIG, NtlmCredentialIn(username="test-user", password="wrong-test"),
            store=store, fetcher=client)
    assert db.scalar(select(func.count()).select_from(Course)) == 0
    assert db.scalar(select(func.count()).select_from(SourceConnection)) == 0


@pytest.mark.asyncio
async def test_public_source_saved_honestly_pending_protected_verification(context):
    db, store = context
    client, visits = fetcher(protected=False)
    source = await save_website(db, CONFIG, NtlmCredentialIn(username="test-user", password="test-secret"),
        store=store, fetcher=client)
    profile = db.scalar(select(CredentialProfile))
    assert profile.state == "AUTH_REQUIRED" and profile.last_verified_at is None
    assert profile.last_error_code == "protected_access_not_verified"
    assert store.get_ntlm(source.credential_id)
    assert len(visits) == 1


@pytest.mark.asyncio
async def test_failed_candidate_keeps_source_identity_config_term_and_secret(context):
    db, store = context
    source = create_website_connection(db, CONFIG)
    store.set_ntlm(source.credential_id, "original", "original-test-secret")
    original = store.get_ntlm(source.credential_id)
    old_config = dict(source.config_json)
    course_id = source.course_id
    client, _ = fetcher(401)
    with pytest.raises(ValueError):
        await save_website(db, {**CONFIG, "course_name": "Changed", "term": "Different"},
            NtlmCredentialIn(username="candidate", password="wrong-test"),
            connection_id=source.id, store=store, fetcher=client)
    db.refresh(source)
    assert source.config_json == old_config and source.course_id == course_id
    assert store.get_ntlm(source.credential_id) is original
    assert db.get(Course, course_id).name == "Test description"


@pytest.mark.asyncio
async def test_blank_password_reuses_loaded_secret_and_ids(context):
    db, store = context
    client, _ = fetcher()
    source = await save_website(db, CONFIG, NtlmCredentialIn(username="original", password="test-secret"),
        store=store, fetcher=client)
    source_id, course_id = source.id, source.course_id
    original = store.get_ntlm(source.credential_id)
    config = {**CONFIG, "course_name": "Renamed", "probe_url": BASE + "private"}
    saved = await save_website(db, config, NtlmCredentialIn(username="original", password=""),
        connection_id=source.id, store=store, fetcher=client)
    assert saved.id == source_id and saved.course_id == course_id
    assert store.get_ntlm(source.credential_id) is original
    assert db.get(Course, course_id).name == "Renamed"


@pytest.mark.asyncio
async def test_label_only_edit_works_offline_without_password(context):
    db, store = context
    source = create_website_connection(db, CONFIG)
    config = {**source.config_json, "course_name": "Offline label edit"}
    def fail(request):
        raise AssertionError("Label-only edit must not fetch")
    client = ResourceFetcher(anonymous=AnonymousFetcher(httpx.MockTransport(fail)))
    saved = await save_website(db, config, None, connection_id=source.id, store=store, fetcher=client)
    assert saved.id == source.id
    assert db.get(Course, source.course_id).name == "Offline label edit"


@pytest.mark.asyncio
async def test_commit_failure_restores_live_credential_and_configuration(context, monkeypatch):
    db, store = context
    source = create_website_connection(db, CONFIG)
    store.set_ntlm(source.credential_id, "original", "original-test-secret")
    original = store.get_ntlm(source.credential_id)
    source_id, credential_id = source.id, source.credential_id
    client, _ = fetcher()
    def fail_commit():
        raise RuntimeError("simulated database write failure")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated database"):
        await save_website(db, {**CONFIG, "course_name": "Must roll back"},
            NtlmCredentialIn(username="replacement", password="replacement-test"),
            connection_id=source_id, store=store, fetcher=client)
    assert store.get_ntlm(credential_id) is original
    db.refresh(source)
    assert db.get(Course, source.course_id).name == "Test description"


@pytest.mark.asyncio
async def test_switch_to_public_removes_only_that_source_credential(context):
    db, store = context
    source = create_website_connection(db, CONFIG)
    credential_id = source.credential_id
    store.set_ntlm(credential_id, "original", "original-test-secret")
    store.set_ntlm("other-site", "other", "other-test")
    client, _ = fetcher(protected=False)
    saved = await save_website(db, {**CONFIG, "authentication_method": "none"}, None,
        connection_id=source.id, store=store, fetcher=client)
    assert saved.credential_id is None
    assert store.get_ntlm(credential_id) is None
    assert store.get_ntlm("other-site") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["http", "outside_prefix", "outside_probe", "malformed_prefix"])
@pytest.mark.parametrize("method", ["none", "ntlm"])
async def test_invalid_stored_scope_can_be_repaired_with_verified_candidate(context, broken, method):
    db, store = context
    source = create_website_connection(db, CONFIG)
    if broken == "http":
        source.base_url = BASE.replace("https:", "http:")
    else:
        field, value = {
            "outside_prefix": ("protected_path_prefix", "/other/"),
            "outside_probe": ("probe_url", "https://outside.example/private"),
            "malformed_prefix": ("protected_path_prefix", "https://outside.example/private"),
        }[broken]
        source.config_json = {**source.config_json, field: value}
    db.commit()
    client, visits = fetcher(protected=method == "ntlm")
    login = NtlmCredentialIn(username="fixture", password="fixture-secret") if method == "ntlm" else None
    saved = await save_website(db, {**CONFIG, "authentication_method": method,
        "protected_path_prefix": "/course/", "probe_url": BASE}, login,
        connection_id=source.id, store=store, fetcher=client)
    assert saved.base_url == BASE.rstrip("/")
    assert saved.config_json["authentication_method"] == method
    assert len(visits) == (2 if method == "ntlm" else 1)
    assert all(url == BASE.rstrip("/") or url.startswith(BASE) for url in visits)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["none", "ntlm"])
@pytest.mark.parametrize("echo", ["omitted", "blank", "exact", "new_invalid"])
async def test_invalid_legacy_prefix_echo_is_recoverable_but_new_invalid_input_is_not(context, method, echo):
    db, store = context
    source = create_website_connection(db, CONFIG)
    old = "https://outside.example/private"
    source.config_json = {**source.config_json, "protected_path_prefix": old}
    db.commit()
    payload = {**CONFIG, "authentication_method": method, "probe_url": ""}
    if echo != "omitted":
        payload["protected_path_prefix"] = {"blank": "", "exact": old,
            "new_invalid": "https://different.example/private"}[echo]
    client, visits = fetcher(protected=method == "ntlm")
    login = NtlmCredentialIn(username="fixture", password="fixture-secret") if method == "ntlm" else None
    if echo == "new_invalid":
        with pytest.raises(ValueError, match="URL path"):
            await save_website(db, payload, login, connection_id=source.id, store=store, fetcher=client)
        assert source.config_json["protected_path_prefix"] == old
        return
    saved = await save_website(db, payload, login, connection_id=source.id, store=store, fetcher=client)
    assert saved.config_json["authentication_method"] == method
    assert saved.config_json["protected_path_prefix"] == ("/course/" if method == "ntlm" else None)
    assert len(visits) == (2 if method == "ntlm" else 1)


def test_saved_probe_is_not_overwritten_by_original_yaml_seed(context, monkeypatch, tmp_path):
    import app.services.auth as auth_module
    from app.config import Settings
    db, store = context
    source = create_website_connection(db, {**CONFIG, "probe_url": BASE + "private"})
    monkeypatch.setattr(auth_module, "configured_auth_profiles", lambda settings: {
        source.credential_id: {"credential_id": source.credential_id, "auth_type": "ntlm",
            "display_name": "Old seed", "probe_url": BASE + "old"}})
    service = auth_module.AuthService(Settings(courses_config=tmp_path / "absent"), store)
    profiles = service.ensure_profiles(db)
    assert profiles[0].probe_url == BASE + "private"
    assert profiles[0].metadata_json["protected_path_prefix"] == "/course/"


@pytest.mark.asyncio
async def test_edited_yaml_source_keeps_ids_and_uses_saved_configuration(context, monkeypatch, tmp_path):
    import app.services.sync as sync_module
    from app.config import Settings
    from app.db import CourseSource
    from app.services.source_connections import (
        ensure_existing_connections,
        update_source_connection,
    )
    from app.sources.config import WebsiteDefinition

    db, store = context
    definition = WebsiteDefinition(course_code="TEST", course_name="Original", timezone="America/Chicago",
        term_calendar={}, export={}, config={"name": "seeded-site", "base_url": BASE, "mode": "http"})
    monkeypatch.setattr(sync_module, "website_definitions", lambda settings: [definition])
    settings = Settings(courses_config=tmp_path / "absent", download_root=tmp_path)
    service = sync_module.SyncService(settings)
    pairs = await service.default_adapters(db, include_canvas=False)
    original_course, original_adapter = pairs[0]
    source_id = original_adapter.course_source_id
    ensure_existing_connections(db, settings)
    connection = db.scalar(select(SourceConnection))
    await update_source_connection(db, connection.id,
        {"name": "Renamed display", "course_name": "New description", "course_code": "RENAMED",
         "base_url": BASE + "new", "authentication_method": "none"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    pairs = await service.default_adapters(db, include_canvas=False)
    assert len(pairs) == 1
    course, adapter = pairs[0]
    assert course.id == original_course.id and adapter.course_source_id == source_id
    assert adapter.name == original_adapter.name == "seeded-site"
    assert adapter.url == BASE + "new"
    assert db.scalar(select(func.count()).select_from(CourseSource)) == 1
    assert db.scalar(select(func.count()).select_from(Course)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded", [False, True])
async def test_saved_credential_is_consumed_by_actual_sync_adapter(context, monkeypatch, tmp_path, seeded):
    import app.services.sync as sync_module
    from app.auth.models import FetchStatus
    from app.config import Settings
    from app.services.source_connections import ensure_existing_connections
    from app.sources.config import WebsiteDefinition

    db, store = context
    definition = WebsiteDefinition(course_code="TEST", course_name="Seed", timezone="America/Chicago",
        term_calendar={}, export={}, config={"name": "seeded-site", "base_url": BASE,
        "auth_rules": [{"path_prefix": "/course/", "auth": {"type": "ntlm",
        "credential_id": "yaml_rule_id", "probe_url": BASE + "private"}}]})
    monkeypatch.setattr(sync_module, "website_definitions", lambda settings: [definition] if seeded else [])
    settings = Settings(courses_config=tmp_path / "absent", download_root=tmp_path)
    service = sync_module.SyncService(settings)
    connection = None
    if seeded:
        await service.default_adapters(db, include_canvas=False)
        ensure_existing_connections(db, settings)
        connection = db.scalar(select(SourceConnection))
        assert connection.credential_id == "yaml_rule_id"
        assert connection.config_json["authentication_method"] == "ntlm"
    client, _ = fetcher()
    saved = await save_website(db, CONFIG, NtlmCredentialIn(username="test-user", password="test-secret"),
        connection_id=connection.id if connection else None, store=store, fetcher=client)
    pairs = await service.default_adapters(db, include_canvas=False)
    assert len(pairs) == 1
    adapter = pairs[0][1]
    assert adapter.auth_rules[0].credential_id == saved.credential_id
    runtime = ResourceFetcher(store=store, anonymous=client.anonymous, ntlm=client.ntlm)
    result = await runtime.fetch(BASE + "private", adapter.auth_rules[0])
    assert result.status == FetchStatus.OK and result.authenticated


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_discovered_query_probe_saves_without_losing_query(context, editing):
    db, store = context
    connection = create_website_connection(db, CONFIG) if editing else None
    visits = []
    def handle(request):
        visits.append(str(request.url))
        if request.url.path.endswith("/private"):
            assert request.url.query == b"id=7"
            return httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
        return httpx.Response(200, headers={"Content-Type": "text/html"},
            text='<a href="/course/private?id=7#section">Notes</a>')
    client = ResourceFetcher(anonymous=AnonymousFetcher(httpx.MockTransport(handle)),
        ntlm=NtlmFetcher(session_factory=lambda: TrialSession(200)))
    saved = await save_website(db, CONFIG, NtlmCredentialIn(username="test-user", password="test-secret"),
        connection_id=connection.id if connection else None, store=store, fetcher=client)
    assert saved.config_json["probe_url"] == BASE + "private?id=7"
    assert store.get_ntlm(saved.credential_id)
    assert all("#" not in url for url in visits)


@pytest.mark.asyncio
async def test_legacy_metadata_edit_preserves_auth_and_exact_term_identity(context):
    db, store = context
    source = create_website_connection(db, CONFIG)
    credential_id = source.credential_id
    course = db.get(Course, source.course_id)
    course.term_id = "original-external-term-id"
    before_term = (course.term, course.term_name, course.term_id, course.term_sort_key)
    source.config_json = {key: value for key, value in source.config_json.items()
                          if key not in {"term", "authentication_method", "protected_path_prefix", "probe_url"}}
    db.commit()
    store.set_ntlm(credential_id, "test-user", "test-secret")
    original = store.get_ntlm(credential_id)
    def fail(request):
        raise AssertionError("Metadata edits must remain offline")
    client = ResourceFetcher(anonymous=AnonymousFetcher(httpx.MockTransport(fail)))
    saved = await save_website(db, {"name": "New label", "base_url": source.base_url}, None,
        connection_id=source.id, store=store, fetcher=client)
    assert saved.config_json["authentication_method"] == "ntlm"
    assert saved.credential_id == credential_id and store.get_ntlm(credential_id) is original
    assert saved.config_json["auth_rules"][0]["path_prefix"] == "/course/"
    assert (course.term, course.term_name, course.term_id, course.term_sort_key) == before_term


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_scope", [False, True])
@pytest.mark.parametrize("ui_payload", [False, True])
async def test_legacy_ntlm_without_profile_allows_offline_metadata_edit(context, empty_scope, ui_payload, monkeypatch):
    db, store = context
    source = create_website_connection(db, CONFIG)
    source.credential_id = None
    if empty_scope:
        source.config_json = {**source.config_json, "protected_path_prefix": None, "probe_url": None}
    db.commit()

    def fail(request):
        raise AssertionError("Metadata-only repair must not probe or need credentials")

    client = ResourceFetcher(anonymous=AnonymousFetcher(httpx.MockTransport(fail)))
    monkeypatch.setattr("app.services.source_connections.httpx.AsyncClient",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("No network allowed")))
    payload = {"name": "No-profile label repaired", "base_url": source.base_url}
    if ui_payload:
        payload.update(authentication_method="ntlm", probe_url="",
                       protected_path_prefix=source.config_json.get("protected_path_prefix") or "")
    saved = await save_website(db, payload,
        None, connection_id=source.id, store=store, fetcher=client)
    assert saved.name == "No-profile label repaired"
    assert saved.config_json["authentication_method"] == "ntlm"
    assert not store.get_ntlm(saved.credential_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["http", "prefix", "probe", "malformed"])
async def test_direct_invalid_scope_repair_probes_only_candidate_once(context, broken):
    from app.services.source_connections import update_source_connection
    db, _ = context
    source = create_website_connection(db, CONFIG)
    if broken == "http":
        source.base_url = BASE.replace("https:", "http:")
    else:
        field, value = {"prefix": ("protected_path_prefix", "/other/"),
            "probe": ("probe_url", "https://outside.example/private"),
            "malformed": ("protected_path_prefix", "https://outside.example/private")}[broken]
        source.config_json = {**source.config_json, field: value}
    db.commit()
    visits = []
    def handle(request):
        visits.append(str(request.url))
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        saved = await update_source_connection(db, source.id, {**CONFIG,
            "authentication_method": "none", "protected_path_prefix": "/course/"}, client=client)
    assert visits == [BASE.rstrip("/")]
    assert saved.config_json["authentication_method"] == "none"


@pytest.mark.parametrize("query", ["access_token=synthetic", "X-Amz-Signature=synthetic", "password=synthetic",
    "session=synthetic", "sid=synthetic", "jsessionid=synthetic", "phpsessid=synthetic", "code=synthetic", "t=synthetic"])
def test_probe_query_cannot_persist_secrets(query):
    from app.services.source_connections import normalized_probe_url
    with pytest.raises(ValueError, match="secret or signed"):
        normalized_probe_url(BASE + "private?" + query)


def test_existing_empty_connection_recovers_yaml_metadata_without_replacing_ids(context, monkeypatch, tmp_path):
    import app.services.source_connections as connections_module
    from app.config import Settings
    from app.db import CourseSource
    from app.sources.config import WebsiteDefinition

    db, store = context
    connection = create_website_connection(db, CONFIG)
    source = db.get(CourseSource, connection.config_json["course_source_id"])
    original_ids = (connection.id, source.id, connection.course_id)
    source.external_id = "legacy-seed"
    source.config_json = {}
    connection.config_json = {"course_source_id": source.id}
    connection.credential_id = None
    db.commit()
    definition = WebsiteDefinition(course_code="TEST", course_name="Seed", timezone="America/Chicago",
        term_calendar={}, export={}, config={"name": "legacy-seed", "base_url": BASE,
        "auth_rules": [{"path_prefix": "/course/private/", "auth": {"type": "ntlm",
        "credential_id": "legacy-yaml-id", "probe_url": BASE + "private/index"}}]})
    monkeypatch.setattr(connections_module, "website_definitions", lambda settings: [definition])
    settings = Settings(courses_config=tmp_path / "absent")
    assert connections_module.ensure_existing_connections(db, settings) == 0
    assert (connection.id, source.id, connection.course_id) == original_ids
    assert connection.credential_id == "legacy-yaml-id"
    assert connection.config_json["authentication_method"] == "ntlm"
    assert connection.config_json["protected_path_prefix"] == "/course/private/"
    assert connection.config_json["course_code"] == "TEST"


@pytest.mark.asyncio
async def test_metadata_save_preserves_custom_monitoring_seeds(context, monkeypatch, tmp_path):
    import app.services.sync as sync_module
    from app.config import Settings
    from app.db import CourseSource

    db, store = context
    connection = create_website_connection(db, CONFIG)
    seeds = [BASE + "schedule.html", BASE + "course-grading.html"]
    connection.config_json = {**connection.config_json, "public_pages": seeds}
    db.commit()
    saved = await save_website(db, {"name": "New label", "base_url": connection.base_url}, None,
        connection_id=connection.id, store=store)
    assert saved.config_json["public_pages"] == seeds
    assert db.get(CourseSource, saved.config_json["course_source_id"]).config_json["public_pages"] == seeds
    monkeypatch.setattr(sync_module, "website_definitions", lambda settings: [])
    pairs = await sync_module.SyncService(Settings(courses_config=tmp_path / "absent")).default_adapters(db, include_canvas=False)
    assert pairs[0][1].public_pages == seeds


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("probe", [None, "", BASE.rstrip("/"), BASE + "private?id=7", BASE + "private?session=synthetic"])
async def test_legacy_secret_probe_can_be_cleared_or_replaced(context, direct, probe):
    from app.services.source_connections import update_source_connection

    db, store = context
    connection = create_website_connection(db, CONFIG)
    connection.config_json = {**connection.config_json, "probe_url": BASE + "private?session=synthetic"}
    db.commit()
    store.set_ntlm(connection.credential_id, "test-user", "test-secret")
    payload = {"name": "Recovered label", "base_url": connection.base_url,
               "protected_path_prefix": "/course/"}
    if probe is not None:
        payload["probe_url"] = probe
    visits = []
    def handle(request):
        visits.append(str(request.url))
        assert "session=" not in str(request.url)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="Public")
    if direct:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            saved = await update_source_connection(db, connection.id, payload, client=client)
    else:
        client = ResourceFetcher(anonymous=AnonymousFetcher(httpx.MockTransport(handle)))
        saved = await save_website(db, payload, None, connection_id=connection.id, store=store, fetcher=client)
    assert saved.name == "Recovered label"
    expected = probe if probe and "session=" not in probe else connection.base_url
    assert saved.config_json["probe_url"] == expected
    assert "session=" not in str(saved.config_json)
    profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == saved.credential_id))
    assert profile.probe_url == saved.config_json["probe_url"]
    if not probe or "session=" in probe:
        assert visits == []  # Metadata-only recovery never needs the site online.


@pytest.mark.asyncio
async def test_echoed_probe_test_is_safe_but_new_secret_probe_rejected(context):
    db, store = context
    connection = create_website_connection(db, CONFIG)
    unsafe = BASE + "private?session=synthetic"
    connection.config_json = {**connection.config_json, "probe_url": unsafe}
    db.commit()
    client, visits = fetcher()
    login = NtlmCredentialIn(username="test-user", password="test-secret")
    result = await save_website(db, {**CONFIG, "probe_url": unsafe}, login,
        connection_id=connection.id, test_only=True, store=store, fetcher=client)
    assert result.verified
    assert all("session=" not in url for url in visits)
    assert connection.config_json["probe_url"] == unsafe  # Test must not persist anything.
    for candidate in (unsafe + "-changed", BASE + "private?t=synthetic"):
        with pytest.raises(ValueError, match="secret or signed"):
            await save_website(db, {**CONFIG, "probe_url": candidate}, login,
                connection_id=connection.id, store=store, fetcher=client)


def test_source_and_profile_responses_redact_legacy_probes_without_mutation(context):
    from app.schemas import CredentialProfileOut, SourceConnectionOut

    db, store = context
    connection = create_website_connection(db, CONFIG)
    unsafe = BASE + "private?session=synthetic"
    config = {**connection.config_json, "probe_url": unsafe,
              "auth_rules": [{"path_prefix": "/course/", "auth": {"type": "ntlm", "probe_url": unsafe}}]}
    connection.config_json = config
    profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == connection.credential_id))
    profile.probe_url = unsafe
    db.commit()
    assert "session=" not in SourceConnectionOut.model_validate(connection).model_dump_json()
    assert "session=" not in CredentialProfileOut.model_validate(profile).model_dump_json()
    assert connection.config_json == config and profile.probe_url == unsafe
