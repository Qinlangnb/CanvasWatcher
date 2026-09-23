"""Saved institution configuration; preserve opaque legacy profile identities."""

from urllib.parse import urlsplit

from pydantic import SecretStr
from sqlalchemy import select

from app.db import CredentialProfile, SourceConnection


def canvas_profile_id(db, settings) -> str:
    connection = db.scalar(select(SourceConnection).where(SourceConnection.source_type == "canvas").order_by(SourceConnection.id))
    if connection and connection.credential_id:
        return connection.credential_id
    profile = db.scalar(select(CredentialProfile).where(CredentialProfile.auth_type == "canvas_token").order_by(CredentialProfile.id))
    return profile.credential_id if profile else settings.canvas_credential_id


def canvas_settings(db, settings):
    connection = db.scalar(select(SourceConnection).where(SourceConnection.source_type == "canvas").order_by(SourceConnection.id))
    base = (connection.base_url if connection else settings.canvas_base_url).rstrip("/")
    if not connection and not base:
        profile = db.scalar(select(CredentialProfile).where(
            CredentialProfile.auth_type == "canvas_token"
        ).order_by(CredentialProfile.id))
        if profile:
            saved = (profile.metadata_json or {}).get("base_url") or profile.probe_url
            parsed = urlsplit(saved or "")
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                base = f"https://{parsed.netloc}"
    values = {"canvas_base_url": base, "canvas_credential_id": canvas_profile_id(db, settings)}
    # Environment secrets are institution-bound, never retargeted by a UI edit.
    if base != settings.canvas_base_url.rstrip("/"):
        values.update(canvas_access_token=SecretStr(""), canvas_oauth_client_id="", canvas_oauth_client_secret=SecretStr(""))
    return settings.model_copy(update=values)
