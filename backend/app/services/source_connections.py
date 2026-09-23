from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.scope import in_auth_scope
from app.config import Settings
from app.db import Course, CourseSource, CredentialProfile, SourceConnection
from app.services.canvas_configuration import canvas_profile_id, canvas_settings
from app.services.course_terms import normalize_course_display, normalize_term
from app.sources.config import website_definitions


def normalized_http_url(value: str) -> str:
    if any(ord(char) < 32 or char == "\\" for char in value):
        raise ValueError("Website URL contains invalid characters")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Website base URL must not contain credentials, query parameters or a fragment")
    _ = parsed.port  # Validate before persisting a malformed authority.
    path = parsed.path.rstrip("/") or ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper())[:80] or "COURSE"


def normalized_probe_url(value: str) -> str:
    """Probe links retain meaningful query parameters; fragments are not sent."""
    if len(value) > 512 or any(ord(char) < 32 or char == "\\" for char in value):
        raise ValueError("Protected test page contains invalid characters")
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Protected test page must be an absolute HTTPS URL without credentials")
    _ = parsed.port
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if re.search(r"token|password|secret|signature|credential|authorization|cookie|session|sessid|api.?key|^sid$|^code$|^t$|^sig$|^key$|^auth$|^x-amz-|^x-goog-", key, re.I):
            raise ValueError("Protected test page must not contain secret or signed query parameters")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, ""))


def effective_website_config(config: dict, credential_id: str | None = None) -> dict:
    """Interpret legacy rules without silently downgrading or widening access."""
    value = dict(config)
    rule = next((row for row in value.get("auth_rules", [])
                 if (row.get("auth") or {}).get("type") == "ntlm"), {})
    value.setdefault("authentication_method", "ntlm" if credential_id or rule else "none")
    if rule:
        value.setdefault("protected_path_prefix", rule.get("path_prefix"))
        value.setdefault("probe_url", (rule.get("auth") or {}).get("probe_url"))
    return value


def stored_probe_url(value: str | None) -> str | None:
    """Discard unsafe legacy probes on edit, never relax candidate validation."""
    if not value:
        return None
    try:
        return normalized_probe_url(value)
    except ValueError:
        return None


def recover_echoed_probe(payload: dict, current: dict) -> dict:
    """Only an exact echo of this source's rejected old value may be discarded."""
    result = dict(payload)
    old = current.get("probe_url")
    if old and result.get("probe_url") == old and stored_probe_url(old) is None:
        result["probe_url"] = None
    return result


def _protected_prefix(value: str | None) -> str:
    prefix = (value or "").strip()
    if not prefix:
        return ""
    parsed = urlsplit(prefix)
    if parsed.scheme or parsed.netloc or "?" in prefix or "#" in prefix:
        raise ValueError("Protected path prefix must be a URL path")
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    return f"{prefix.rstrip('/')}/"


def website_ntlm_scope(base: str, prefix: str | None, probe: str | None) -> tuple[str, str]:
    """Default to the course path; preserve an explicitly narrower legacy rule."""
    path = urlsplit(base).path or "/"
    if "." in path.rsplit("/", 1)[-1]:
        path = path.rsplit("/", 1)[0] + "/"
    scope = _protected_prefix(prefix) or _protected_prefix(path)
    origin = urlsplit(base)
    scoped_url = urlunsplit((origin.scheme, origin.netloc, scope, "", ""))
    if not in_auth_scope(scoped_url, base, _protected_prefix(path)):
        raise ValueError("NTLM requires HTTPS and a protected path within this course website")
    target = normalized_probe_url(probe) if probe else base
    if probe and not in_auth_scope(target, base, scope):
        raise ValueError("Protected test page must be within this website's protected scope")
    return scope, target


def recover_echoed_prefix(payload: dict, current: dict) -> dict:
    """Recover only an omitted/empty/exact echo of a syntactically invalid old path."""
    result = dict(payload)
    old = current.get("protected_path_prefix")
    try:
        _protected_prefix(old)
    except ValueError:
        if not result.get("protected_path_prefix") or result.get("protected_path_prefix") == old:
            result["protected_path_prefix"] = None
    return result


def resolve_exact_term(db: Session, raw_name: str | None):
    text = (raw_name or "").strip()
    if not text:
        return normalize_term(None)
    existing = db.scalar(
        select(Course)
        .where(Course.term_name == text)
        .order_by(Course.id)
        .limit(1)
    )
    if existing:
        return normalize_term(
            text,
            term_id=existing.term_id,
            start_at=existing.term_start_at,
            end_at=existing.term_end_at,
        )
    return normalize_term(text)


def ensure_existing_connections(db: Session, settings: Settings) -> int:
    settings = canvas_settings(db, settings)
    created = 0
    enriched = False
    definitions = {row.config["name"]: row.config for row in website_definitions(settings)}
    rows = db.execute(
        select(CourseSource, Course).join(Course, Course.id == CourseSource.course_id)
    ).all()
    canvas_rows = [(source, course) for source, course in rows if source.source_type == "canvas"]
    if canvas_rows and settings.canvas_base_url:
        key = f"canvas:{normalized_http_url(settings.canvas_base_url)}"
        if db.scalar(select(SourceConnection).where(SourceConnection.external_key == key)) is None:
            db.add(
                SourceConnection(
                    source_type="canvas",
                    name="Canvas",
                    external_key=key,
                    base_url=normalized_http_url(settings.canvas_base_url),
                    credential_id=canvas_profile_id(db, settings),
                    config_json={"managed_course_source_ids": [row[0].id for row in canvas_rows]},
                    state="ACTIVE" if any(row[0].enabled for row in canvas_rows) else "DISABLED",
                )
            )
            created += 1
    seen: set[str] = set()
    for source, course in rows:
        if source.source_type != "website" or not source.url:
            continue
        base = normalized_http_url(source.url)
        key = f"website:{base}"
        if key in seen:
            continue
        seen.add(key)
        if not source.config_json and source.external_id in definitions:
            source.config_json = dict(definitions[source.external_id])
            enriched = True
        config = effective_website_config(source.config_json or {})
        config = {"course_name": course.name, "course_code": course.course_code,
                  "term": course.term_name or course.term, **config}
        credential_id = None
        for rule in config.get("auth_rules", []):
            credential_id = (rule.get("auth") or {}).get("credential_id") or credential_id
        existing = db.scalar(select(SourceConnection).where(SourceConnection.external_key == key))
        if existing:
            merged = {"course_name": course.name, "course_code": course.course_code,
                      "term": course.term_name or course.term, **(source.config_json or {}),
                      "course_source_id": source.id, **(existing.config_json or {})}
            merged = effective_website_config(merged, existing.credential_id)
            if merged != existing.config_json:
                existing.config_json = merged
                enriched = True
            if not existing.credential_id and merged["authentication_method"] == "ntlm" and credential_id:
                existing.credential_id = credential_id
                enriched = True
            continue
        db.add(
            SourceConnection(
                source_type="website",
                name=source.name or f"{course.course_code} Website",
                external_key=key,
                base_url=base,
                course_id=course.id,
                credential_id=credential_id,
                config_json={**config, "course_source_id": source.id},
                state=source.state.upper(),
            )
        )
        created += 1
    if created or enriched:
        db.commit()
    return created


def create_canvas_connection(db: Session, base_url: str, name: str | None = None) -> SourceConnection:
    from app.config import get_settings
    base = normalized_http_url(base_url)
    key = f"canvas:{base}"
    existing = db.scalar(select(SourceConnection).where(SourceConnection.external_key == key))
    if existing:
        return existing
    if db.scalar(select(SourceConnection).where(SourceConnection.source_type == "canvas")):
        raise ValueError("Edit the existing Canvas institution instead of adding a second instance")
    value = SourceConnection(
        source_type="canvas",
        name=name or "Canvas",
        external_key=key,
        base_url=base,
        credential_id=canvas_profile_id(db, get_settings()),
        config_json={},
        state="AUTH_REQUIRED",
    )
    db.add(value)
    db.commit()
    db.refresh(value)
    return value


def create_website_connection(db: Session, payload: dict, *, commit: bool = True) -> SourceConnection:
    base = normalized_http_url(payload["base_url"])
    protected_prefix = _protected_prefix(payload.get("protected_path_prefix"))
    probe_url = (payload.get("probe_url") or "").strip()
    if payload.get("authentication_method") == "ntlm":
        protected_prefix, probe_url = website_ntlm_scope(base, protected_prefix, probe_url)
    key = f"website:{base}"
    existing = db.scalar(select(SourceConnection).where(SourceConnection.external_key == key))
    if existing:
        return existing
    name = payload["course_name"].strip()
    code = (payload.get("course_code") or _slug(name)).strip()
    term_text = (payload.get("term") or "").strip() or None
    display = normalize_course_display(code, name, term_text)
    term = resolve_exact_term(db, term_text)
    course = db.scalar(
        select(Course).where(Course.external_id == f"website:{base}")
    )
    if course is None:
        course = Course(
            source="website",
            external_id=f"website:{base}",
            course_code=code,
            name=name,
            term=term_text,
            active=True,
            lifecycle_state="ACTIVE",
            term_id=term.term_id,
            term_name=term.display_name,
            term_start_at=term.start_at,
            term_end_at=term.end_at,
            term_sort_key=term.sort_key,
            display_course_code=display.course_code,
            display_name=display.name,
            section=display.section,
        )
        db.add(course)
        db.flush()
    credential_id = None
    auth_rules: list[dict] = []
    if payload.get("authentication_method") == "ntlm":
        credential_id = "website_" + hashlib.sha256(base.encode()).hexdigest()[:16]
        if protected_prefix:
            auth_rules.append(
                {
                    "path_prefix": protected_prefix,
                    "auth": {
                        "type": "ntlm",
                        "credential_id": credential_id,
                        "probe_url": probe_url,
                        "display_name": name,
                    },
                }
            )
        profile = db.scalar(
            select(CredentialProfile).where(CredentialProfile.credential_id == credential_id)
        )
        if profile is None:
            profile = CredentialProfile(
                credential_id=credential_id,
                auth_type="ntlm",
                display_name=f"{name} NTLM",
                probe_url=probe_url or base,
                metadata_json={"base_url": base, "protected_path_prefix": protected_prefix},
                state="AUTH_REQUIRED",
            )
            db.add(profile)
    config = {
        "name": (payload.get("name") or f"{code} Site").strip(),
        "base_url": base,
        "mode": "http",
        "public_pages": [base],
        "auth_rules": auth_rules,
        "protected_path_prefix": protected_prefix or None,
        "probe_url": probe_url or None,
        "course_name": name,
        "course_code": code,
        "term": term_text,
        "authentication_method": payload.get("authentication_method", "none"),
        "discovery_path_limit": payload.get("discovery_path_limit", 100),
    }
    source = CourseSource(
        course_id=course.id,
        name=config["name"],
        source_type="website",
        external_id=key,
        url=base,
        mode="http",
        enabled=True,
        config_json=config,
        state="AUTH_REQUIRED" if credential_id else "READY",
    )
    db.add(source)
    db.flush()
    connection = SourceConnection(
        source_type="website",
        name=config["name"],
        external_key=key,
        base_url=base,
        course_id=course.id,
        credential_id=credential_id,
        config_json={**config, "course_source_id": source.id},
        state=source.state,
    )
    db.add(connection)
    db.flush()
    if commit:
        db.commit()
    db.refresh(connection)
    return connection


async def update_source_connection(
    db: Session,
    connection_id: int,
    payload: dict,
    *,
    client: httpx.AsyncClient | None = None,
    commit: bool = True,
    probe_validated: bool = False,
) -> SourceConnection:
    connection = db.get(SourceConnection, connection_id)
    if connection is None:
        raise LookupError("Source connection not found")
    base = normalized_http_url(payload["base_url"])
    key = f"{connection.source_type}:{base}"
    duplicate = db.scalar(
        select(SourceConnection).where(
            SourceConnection.external_key == key,
            SourceConnection.id != connection.id,
        )
    )
    if duplicate:
        raise ValueError("Another source already uses this base URL")
    current_config = effective_website_config(connection.config_json or {}, connection.credential_id)
    if connection.source_type == "website":
        payload = recover_echoed_probe(payload, current_config)
        payload = recover_echoed_prefix(payload, current_config)
    auth_method = payload.get("authentication_method", current_config["authentication_method"])
    probe_url = normalized_probe_url(payload["probe_url"]) if payload.get("probe_url") else base
    if connection.source_type == "canvas":
        probe_url = f"{base}/api/v1/users/self/profile"
    current_base = normalized_http_url(connection.base_url or base)
    source_identity_name = current_config.get("source_identity_name") or connection.name
    current_auth_method = str(current_config.get("authentication_method") or "none")
    candidate_prefix = _protected_prefix(payload.get("protected_path_prefix"))
    invalid_stored_scope = False
    try:
        current_prefix = _protected_prefix(current_config.get("protected_path_prefix"))
    except ValueError:
        current_prefix = ""
        invalid_stored_scope = current_auth_method == "ntlm"
    current_probe = stored_probe_url(current_config.get("probe_url")) or current_base
    if connection.source_type == "canvas":
        current_probe = f"{current_base}/api/v1/users/self/profile"
    if auth_method == "ntlm":
        candidate_prefix, probe_url = website_ntlm_scope(
            base, candidate_prefix, payload.get("probe_url"))
    if current_auth_method == "ntlm" and connection.source_type == "website":
        # Compare effective scopes on both sides. An old empty prefix already
        # means the source path; deriving it must not turn a label edit into a probe.
        try:
            current_prefix, current_probe = website_ntlm_scope(
                current_base, current_prefix, current_probe)
        except ValueError:
            # A bad persisted scope must not prevent installing a verified repair.
            # Only the old comparison side is tolerant; candidate validation above
            # stays strict and an invalid old scope always requires verification.
            invalid_stored_scope = True
    requires_probe = base != current_base or invalid_stored_scope
    if connection.source_type == "canvas":
        requires_probe = requires_probe or (
            payload.get("default_auth_method") or "pat"
        ) != (current_config.get("default_auth_method") or "pat")
    else:
        requires_probe = requires_probe or auth_method != current_auth_method
        requires_probe = requires_probe or candidate_prefix != current_prefix
        requires_probe = requires_probe or (auth_method == "ntlm" and probe_url != current_probe)
    if requires_probe and not probe_validated:
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=12, follow_redirects=False)
        try:
            response = await client.get(probe_url)
        except httpx.HTTPError as error:
            raise ValueError("Candidate source endpoint could not be reached") from error
        finally:
            if owns_client:
                await client.aclose()
        acceptable = response.status_code < 400 or response.status_code in {401, 403}
        if not acceptable:
            raise ValueError(f"Candidate source probe returned HTTP {response.status_code}")

    connection.name = payload["name"].strip()
    connection.base_url = base
    connection.external_key = key
    if connection.source_type == "canvas":
        connection.config_json = {
            **(connection.config_json or {}),
            "default_auth_method": payload.get("default_auth_method") or "pat",
        }
        profile = db.scalar(
            select(CredentialProfile).where(CredentialProfile.credential_id == connection.credential_id)
        )
        if profile:
            profile.probe_url = probe_url
            profile.metadata_json = {**(profile.metadata_json or {}), "base_url": base}
        for source in db.scalars(
            select(CourseSource).where(CourseSource.source_type == "canvas")
        ):
            if source.external_id:
                source.url = f"{base}/courses/{source.external_id}"
                source.state = "PENDING_RECONCILIATION" if source.enabled else source.state
        connection.state = "PENDING_RECONCILIATION"
    else:
        course = db.get(Course, connection.course_id) if connection.course_id else None
        if course is None:
            raise ValueError("Course-linked source has no course")
        course_name = (payload.get("course_name") or course.name).strip()
        course_code = (payload.get("course_code") or course.course_code).strip()
        term_text = (payload.get("term", course.term_name or course.term) or "").strip() or None
        display = normalize_course_display(course_code, course_name, term_text)
        term = resolve_exact_term(db, term_text)
        course.name = course_name
        course.course_code = course_code
        if "term" in payload:
            course.term = term_text
            course.term_id = term.term_id
            course.term_name = term.display_name
            course.term_start_at = term.start_at
            course.term_end_at = term.end_at
            course.term_sort_key = term.sort_key
        course.display_course_code = display.course_code
        course.display_name = display.name
        course.section = display.section
        protected_prefix = candidate_prefix
        credential_id = connection.credential_id
        auth_rules: list[dict] = []
        if auth_method == "ntlm":
            credential_id = credential_id or "website_" + hashlib.sha256(base.encode()).hexdigest()[:16]
            auth_rules = [
                {
                    "path_prefix": protected_prefix,
                    "auth": {
                        "type": "ntlm",
                        "credential_id": credential_id,
                        "probe_url": probe_url,
                        "display_name": connection.name,
                    },
                }
            ]
            profile = db.scalar(
                select(CredentialProfile).where(
                    CredentialProfile.credential_id == credential_id
                )
            )
            if profile is None:
                profile = CredentialProfile(
                    credential_id=credential_id,
                    auth_type="ntlm",
                    display_name=f"{connection.name} NTLM",
                    probe_url=probe_url,
                    metadata_json={"base_url": base, "protected_path_prefix": protected_prefix},
                    state="AUTH_REQUIRED",
                )
                db.add(profile)
            else:
                profile.probe_url = probe_url
                profile.display_name = f"{connection.name} NTLM"
                profile.metadata_json = {**(profile.metadata_json or {}), "base_url": base,
                                         "protected_path_prefix": protected_prefix}
        else:
            credential_id = None
        config = {
            **(connection.config_json or {}),
            "name": connection.name,
            "source_identity_name": source_identity_name,
            "base_url": base,
            "public_pages": current_config.get("public_pages", [base]) if base == current_base else [base],
            "auth_rules": auth_rules,
            "protected_path_prefix": protected_prefix or None,
            "probe_url": probe_url if auth_method == "ntlm" else None,
            "discovery_path_limit": payload.get("discovery_path_limit", 100),
            "course_name": course_name,
            "course_code": course_code,
            "term": term_text,
            "authentication_method": auth_method,
        }
        connection.credential_id = credential_id
        connection.config_json = config
        source_id = config.get("course_source_id")
        source = db.get(CourseSource, source_id) if source_id else None
        if source:
            source.name = connection.name
            source.url = base
            source.config_json = config
            source.state = "PENDING_RECONCILIATION" if source.enabled else source.state
        connection.state = "PENDING_RECONCILIATION"
    db.flush()
    if commit:
        db.commit()
    db.refresh(connection)
    return connection
