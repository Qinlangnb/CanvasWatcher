"""Browser UI creates capabilities; only the host broker exchanges sessions."""

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.broker_challenges import ChallengeError, broker_challenges
from app.auth.models import CanvasBrowserSessionCredential
from app.auth.provider_sessions import ProviderSession, provider_sessions
from app.auth.store import credential_store
from app.config import Settings, get_settings
from app.db import CredentialProfile, get_db, utcnow
from app.services.auth import AuthService
from app.services.canvas_web_session import session_url
from app.sources.canvas_client import CanvasClient
from app.sources.provider_web import ProviderWeb, provider_base

router = APIRouter(prefix="/api/auth/broker", tags=["auth-broker"])


class AccountMismatch(ValueError):
    pass


@router.get("/configuration")
def configuration(settings: Settings = Depends(get_settings)):
    return {"broker_url": f"http://127.0.0.1:{settings.auth_broker_port}", "protocol_version": 1}


class SafeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class NewChallenge(SafeInput):
    provider: Literal["canvas", "gradescope", "prairielearn", "all"]
    credential_id: str = Field(default="", max_length=120)
    operation: Literal["login", "clear", "clear_all"] = "login"


class Identity(SafeInput):
    challenge_id: str = Field(min_length=16, max_length=128)
    instance_id: str = Field(min_length=16, max_length=128)
    provider: Literal["canvas", "gradescope", "prairielearn", "all"]


class Claim(Identity):
    one_time_token: SecretStr


class Control(Identity):
    control_token: SecretStr


class Completion(Identity):
    exchange_token: SecretStr
    cookies: dict[str, SecretStr] = Field(default_factory=dict, max_length=100)


class Failure(Identity):
    exchange_token: SecretStr
    reason: Literal["AUTH_LOGIN_FAILED", "AUTH_USER_CANCELLED", "AUTH_LOGIN_TIMEOUT"]


def browser_boundary(request: Request, settings: Settings = Depends(get_settings)):
    if request.headers.get("origin") not in settings.cors_origin_list or request.headers.get("x-aw-broker") != "1":
        raise HTTPException(403, "AUTH_ORIGIN_DENIED")


def broker_boundary(request: Request):
    # Browser fetch metadata cannot be removed by frontend JavaScript. Reject it
    # even on a same-origin reverse proxy; no cookie-bearing response reaches JS.
    if (request.headers.get("origin") is not None or request.headers.get("sec-fetch-site") is not None
            or request.headers.get("x-aw-broker") != "1"):
        raise HTTPException(403, "AUTH_BROKER_ONLY")


def identity(payload):
    return payload.challenge_id, payload.instance_id, payload.provider


def checked(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except ChallengeError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/challenges", dependencies=[Depends(browser_boundary)])
def create(payload: NewChallenge, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if payload.provider == "all" and payload.operation == "clear_all":
        profiles = list(db.scalars(select(CredentialProfile).where(CredentialProfile.auth_type == "canvas_token")))
        return checked(broker_challenges.create, "all", {"operation": "clear_all", "credential_id": "all",
            "provider_generations": {profile.credential_id: provider_sessions.generation(profile.credential_id)
                for profile in db.scalars(select(CredentialProfile).where(CredentialProfile.auth_type == "provider_session"))},
            "generations": {profile.credential_id: credential_store.generation(profile.credential_id, "browser_session") for profile in profiles}})
    if payload.provider == "all" or payload.operation == "clear_all":
        raise HTTPException(422, "AUTH_OPERATION_INVALID")
    service = AuthService(settings)
    profile = service.profile(db, payload.credential_id)
    if payload.provider in {"gradescope", "prairielearn"}:
        if profile is None or profile.auth_type != "provider_session" or (profile.metadata_json or {}).get("provider") != payload.provider:
            raise HTTPException(404, "AUTH_PROFILE_NOT_FOUND")
        return checked(broker_challenges.create, payload.provider, {
            "base_url": provider_base(payload.provider, profile.metadata_json["base_url"]),
            "credential_id": profile.credential_id, "operation": payload.operation,
            "generation": provider_sessions.generation(profile.credential_id),
            "canvas_course_id": profile.metadata_json.get("canvas_course_id"),
        })
    if profile is None or profile.auth_type != "canvas_token":
        raise HTTPException(404, "AUTH_PROFILE_NOT_FOUND")
    base = service.settings.canvas_base_url.rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise HTTPException(422, "AUTH_PROVIDER_NOT_CONFIGURED")
    return checked(broker_challenges.create, payload.provider, {
        "base_url": base, "credential_id": profile.credential_id, "operation": payload.operation,
        "generation": credential_store.generation(profile.credential_id, "browser_session"),
    })


@router.post("/claim", dependencies=[Depends(broker_boundary)])
async def claim(payload: Claim, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    result = checked(broker_challenges.claim, *identity(payload), payload.one_time_token.get_secret_value())
    if payload.provider == "canvas" and result["operation"] == "login":
        credential = credential_store.canvas_slot(result["credential_id"], "oauth") or credential_store.canvas_slot(result["credential_id"], "pat")
        if credential and credential.base_url == result["base_url"]:
            result["launch_url"] = await session_url(credential)
    elif payload.provider == "gradescope" and result["operation"] == "login" and result.get("canvas_course_id"):
        from app.services.canvas_lti import gradescope_launch
        service = AuthService(settings)
        service.ensure_profiles(db)
        client = service.routed_client(db)
        if client:
            result["launch_url"] = await gradescope_launch(client, result["canvas_course_id"], result["base_url"])
            result["launch_origin"] = client.base_url
    return result


@router.post("/status", dependencies=[Depends(browser_boundary)])
def status(payload: Control):
    return checked(broker_challenges.control, *identity(payload), payload.control_token.get_secret_value())


@router.post("/cancel", dependencies=[Depends(browser_boundary)])
def cancel(payload: Control):
    return checked(broker_challenges.control, *identity(payload), payload.control_token.get_secret_value(), cancel=True)


@router.post("/failed", dependencies=[Depends(broker_boundary)])
def failed(payload: Failure):
    row = checked(broker_challenges.begin_completion, *identity(payload), payload.exchange_token.get_secret_value())
    broker_challenges.fail(row, payload.reason)
    return {"state": "FAILED", "reason": payload.reason}


@router.post("/complete", dependencies=[Depends(broker_boundary)])
async def complete(payload: Completion, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    row = checked(broker_challenges.begin_completion, *identity(payload), payload.exchange_token.get_secret_value())
    cookies = {name: value.get_secret_value() for name, value in payload.cookies.items()}
    try:
        if row.provider == "all" and row.target["operation"] == "clear_all":
            def clear_all():
                with credential_store.atomic():
                    service = AuthService(settings)
                    profiles = list(db.scalars(select(CredentialProfile).where(CredentialProfile.auth_type == "canvas_token")))
                    for profile in profiles:
                        expected = row.target["generations"].get(profile.credential_id)
                        if expected is not None and credential_store.generation(profile.credential_id, "browser_session") == expected:
                            service.remove_canvas_method(db, profile, "browser_session", expected)
                with provider_sessions.lock:
                    for key, expected in row.target.get("provider_generations", {}).items():
                        if provider_sessions.generation(key) == expected:
                            provider_sessions.remove(key)
                            profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == key))
                            if profile:
                                profile.state = "AUTH_REQUIRED"
                    db.commit()
            checked(broker_challenges.finish, row, clear_all)
            return {"state": "AUTHENTICATED"}
        if any(not name or any(c in name + value for c in "\r\n;\x00") or len(name) + len(value) > 16384
               for name, value in cookies.items()):
            raise ValueError("Invalid cookie")
        service = AuthService(settings)
        profile = service.profile(db, row.target["credential_id"])
        if row.provider in {"gradescope", "prairielearn"}:
            if profile is None or profile.auth_type != "provider_session" or (profile.metadata_json or {}).get("provider") != row.provider:
                raise ValueError("Unknown profile")
            session = ProviderSession(row.provider, row.target["base_url"], cookies)
            if row.target["operation"] == "login":
                await ProviderWeb(session).probe()
            def publish_provider():
                with provider_sessions.lock:
                    db.refresh(profile)
                    if provider_sessions.generation(profile.credential_id) != row.target["generation"] or profile.metadata_json.get("base_url") != row.target["base_url"]:
                        raise ValueError("Credential changed")
                    profile.state = "ACTIVE" if row.target["operation"] == "login" else "AUTH_REQUIRED"
                    profile.last_verified_at, profile.last_error_code = utcnow(), None
                    db.commit()
                    if row.target["operation"] == "login":
                        provider_sessions.set(profile.credential_id, session, row.target["generation"])
                    else:
                        provider_sessions.remove(profile.credential_id)
            checked(broker_challenges.finish, row, publish_provider)
            return {"state": "AUTHENTICATED"}
        if profile is None or profile.auth_type != "canvas_token" or row.provider != "canvas":
            raise ValueError("Unknown profile")
        account = None
        if row.target["operation"] == "login":
            account = await CanvasClient(row.target["base_url"], credential=CanvasBrowserSessionCredential(
                row.target["base_url"], cookies)).probe()

        def publish():
            db.refresh(profile)
            if credential_store.generation(profile.credential_id, "browser_session") != row.target["generation"]:
                raise ValueError("Credential changed")
            if row.target["operation"] == "clear":
                service.remove_canvas_method(db, profile, "browser_session")
                return
            metadata = profile.metadata_json or {}
            existing = str((metadata.get("account") or {}).get("id") or "")
            candidate = str(account.get("id") or "")
            if not candidate or (existing and (candidate != existing or metadata.get("base_url") != row.target["base_url"])):
                raise AccountMismatch()
            profile.state, profile.last_verified_at, profile.last_error_code = "ACTIVE", utcnow(), None
            preferred = credential_store.canvas_slot(profile.credential_id, "oauth") or credential_store.canvas_slot(profile.credential_id, "pat")
            profile.metadata_json = {**metadata, "base_url": row.target["base_url"],
                "account": {"id": candidate, "name": account.get("name")}, "auth_source": "memory",
                "auth_mode": preferred.auth_mode.value if preferred else "browser_session"}
            db.commit()
            credential_store.set_canvas_session(profile.credential_id, row.target["base_url"], cookies)
            credential_store.bind_identity(profile.credential_id, "browser_session", candidate)

        def atomic_publish():
            with credential_store.atomic():
                publish()

        checked(broker_challenges.finish, row, atomic_publish)
        return {"state": "AUTHENTICATED"}
    except AccountMismatch:
        broker_challenges.fail(row, "CANVAS_ACCOUNT_MISMATCH")
        raise HTTPException(409, "CANVAS_ACCOUNT_MISMATCH") from None
    except HTTPException:
        raise
    except Exception:
        broker_challenges.fail(row)
        raise HTTPException(409, "AUTH_LOGIN_FAILED") from None
    finally:
        cookies.clear()
        payload.cookies.clear()
