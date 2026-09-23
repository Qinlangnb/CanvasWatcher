"""Candidate-only website probes and transactional source/credential installation."""

import hashlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.fetchers import ResourceFetcher, resource_fetcher
from app.auth.models import AuthRule, FetchStatus, NtlmCredential
from app.auth.scope import in_auth_scope
from app.auth.store import CredentialStore, credential_store
from app.db import CredentialProfile, SourceConnection, utcnow
from app.schemas import NtlmCredentialIn
from app.services.source_connections import (
    create_website_connection,
    effective_website_config,
    normalized_http_url,
    normalized_probe_url,
    recover_echoed_prefix,
    recover_echoed_probe,
    stored_probe_url,
    update_source_connection,
    website_ntlm_scope,
)


@dataclass(repr=False)
class WebsiteProbe:
    credential: NtlmCredential | None
    verified: bool
    probe_url: str | None
    message: str


async def probe_website(
    config: dict, login: NtlmCredentialIn | None, previous: NtlmCredential | None,
    *, fetcher: ResourceFetcher = resource_fetcher,
) -> WebsiteProbe:
    base = normalized_http_url(config["base_url"])
    if config.get("authentication_method") != "ntlm":
        result = await fetcher.anonymous.fetch(base)
        if result.status != FetchStatus.OK:
            raise ValueError("Public website could not be reached")
        return WebsiteProbe(None, False, None, "Public website reachable.")
    scope, target = website_ntlm_scope(base, config.get("protected_path_prefix"), config.get("probe_url"))
    password = login.password.get_secret_value() if login else ""
    username = login.username.strip() if login else previous.username if previous else ""
    if not username:
        raise ValueError("Username is required")
    if not password:
        if previous is None or previous.username != username:
            raise ValueError("Enter a username and password; no matching credential is loaded")
        candidate = previous
    else:
        candidate = NtlmCredential(username, password)
    trial_id = f"website-trial-{uuid4().hex}"
    trial_store = CredentialStore()
    trial_store.set_ntlm(trial_id, candidate.username, candidate.password)
    trial = ResourceFetcher(store=trial_store, anonymous=fetcher.anonymous, ntlm=fetcher.ntlm)
    rule = AuthRule(path_prefix=scope, base_url=base, probe_url=target,
                    auth_type="ntlm", credential_id=trial_id)
    # Only actual links are visited. Six requests at most; no guessed paths.
    queue, visited = [target], set()
    public_reachable = False
    try:
        while queue and len(visited) < 6:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            scoped = in_auth_scope(url, base, scope)
            result = await trial.fetch(url, rule) if scoped else await fetcher.anonymous.fetch(url)
            if result.status == FetchStatus.OK and result.authenticated:
                return WebsiteProbe(candidate, True, result.url, "Protected access verified.")
            if result.status in {FetchStatus.AUTH_FAILED, FetchStatus.AUTH_REQUIRED}:
                raise ValueError("Protected access could not be verified; existing configuration kept")
            if result.status != FetchStatus.OK:
                if url == target:
                    raise ValueError("Candidate website test page could not be reached")
                continue
            # Never trust a public redirect into another origin/course.
            course_scope, _ = website_ntlm_scope(base, None, None)
            if not in_auth_scope(result.url, base, course_scope):
                raise ValueError("Website redirected outside its trusted course scope")
            public_reachable = True
            if "html" in (result.content_type or ""):
                soup = BeautifulSoup((result.content or b"")[:1_000_000], "html.parser")
                for link in soup.select("a[href]"):
                    try:
                        actual = normalized_probe_url(urljoin(result.url, link.get("href", "")))
                    except ValueError:
                        continue
                    if in_auth_scope(actual, base, scope) and actual not in visited and actual not in queue:
                        queue.append(actual)
                    if len(queue) >= 6 - len(visited):
                        break
            if url != base and base not in visited and not queue:
                queue.append(base)
        if public_reachable:
            if previous is not None and candidate is not previous:
                raise ValueError("Protected access not verified; existing credential kept")
            return WebsiteProbe(candidate, False, None,
                                "Public website reachable. Protected access not yet verified.")
        raise ValueError("Candidate website could not be reached")
    finally:
        trial_store.clear(trial_id)
        fetcher.ntlm.invalidate(trial_id)


async def save_website(
    db: Session, config: dict, login: NtlmCredentialIn | None, *,
    connection_id: int | None = None, test_only: bool = False,
    store: CredentialStore = credential_store, fetcher: ResourceFetcher = resource_fetcher,
) -> SourceConnection | WebsiteProbe:
    config = dict(config)
    base = normalized_http_url(config["base_url"])
    connection = db.get(SourceConnection, connection_id) if connection_id else None
    if connection_id and (connection is None or connection.source_type != "website"):
        raise ValueError("Website source not found")
    duplicate = db.scalar(select(SourceConnection).where(SourceConnection.external_key == f"website:{base}"))
    if duplicate and (connection is None or duplicate.id != connection.id):
        raise ValueError("Another source already uses this website; edit that source instead")
    old_config = effective_website_config(connection.config_json or {}, connection.credential_id) if connection else {}
    if connection:
        config = recover_echoed_probe(config, old_config)
        old_config["probe_url"] = stored_probe_url(old_config.get("probe_url"))
    if connection:
        for key in ("authentication_method", "protected_path_prefix", "probe_url"):
            if key not in config:
                config[key] = old_config.get(key)
    # Omission/blank advanced prefix is not permission to widen a legacy scope.
    if connection and old_config.get("protected_path_prefix") and not config.get("protected_path_prefix"):
        config["protected_path_prefix"] = old_config["protected_path_prefix"]
    if connection:
        config = recover_echoed_prefix(config, old_config)
    credential_id = connection.credential_id if connection else None
    credential_id = credential_id or "website_" + hashlib.sha256(base.encode()).hexdigest()[:16]
    previous = store.get_ntlm(credential_id)
    network_fields = ("base_url", "authentication_method", "protected_path_prefix")
    changed_network = not connection or any(
        (config.get(key) or "") != ((connection.base_url if key == "base_url" else old_config.get(key)) or "")
        for key in network_fields)
    if connection:
        changed_network = changed_network or (config.get("probe_url") or base) != (
            old_config.get("probe_url") or normalized_http_url(connection.base_url or base))
    if config.get("authentication_method") == "ntlm" and connection and previous and urlsplit(base).netloc != urlsplit(connection.base_url or "").netloc:
        if not login or not login.password.get_secret_value():
            raise ValueError("Enter a password to verify a different website origin")
    if connection and not changed_network and login is None and not test_only:
        return await update_source_connection(db, connection.id, config)
    result = await probe_website(config, login, previous, fetcher=fetcher)
    if test_only:
        return result
    if result.probe_url:
        config["probe_url"] = result.probe_url
    installed = False
    try:
        if store.get_ntlm(credential_id) is not previous:
            raise ValueError("Credential changed during verification; retry")
        saved = await update_source_connection(db, connection.id, config, commit=False, probe_validated=True) if connection else create_website_connection(db, config, commit=False)
        if result.credential:
            profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == saved.credential_id))
            profile.username = result.credential.username
            profile.state = "ACTIVE" if result.verified else "AUTH_REQUIRED"
            profile.last_verified_at = utcnow() if result.verified else None
            profile.last_error_code = None if result.verified else "protected_access_not_verified"
            db.flush()
            installed = store.replace_ntlm(credential_id, previous, result.credential)
            if not installed:
                raise ValueError("Credential changed during verification; retry")
        elif previous is not None:
            profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == credential_id))
            if profile:
                profile.state = "AUTH_REQUIRED"
                profile.last_verified_at = None
                profile.last_error_code = "credential_not_loaded"
            installed = store.replace_ntlm(credential_id, previous, None)
            if not installed:
                raise ValueError("Credential changed during verification; retry")
        db.commit()
        if installed:
            fetcher.ntlm.invalidate(credential_id)
        return saved
    except Exception:
        db.rollback()
        if installed:
            store.replace_ntlm(credential_id, result.credential, previous)
        raise
