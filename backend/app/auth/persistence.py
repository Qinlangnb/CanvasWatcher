"""Startup-only wiring. Login capabilities are deliberately not persistent."""
from app.auth.provider_sessions import provider_sessions
from app.auth.store import credential_store
from app.auth.vault import CredentialVault
from app.services.ai_settings import ai_probe_secrets


def restore_credentials(settings):
    vault = CredentialVault(settings.credential_vault_path, settings.credential_master_key_file) \
        if settings.credential_persistence_enabled else None
    credential_store.configure_persistence(vault)
    provider_sessions.configure_persistence(vault)
    ai_probe_secrets.configure_persistence(vault)
    return vault
