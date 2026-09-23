import hashlib
from datetime import date, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.fetchers import ResourceFetcher, resource_fetcher
from app.auth.models import (
    AuthRule,
    CanvasAuthMode,
    CanvasBrowserSessionCredential,
    CanvasCredential,
    CanvasCredentialValue,
    FetchStatus,
    NtlmCredential,
)
from app.auth.store import CredentialStore, credential_store
from app.config import Settings
from app.db import CourseSource, CredentialProfile, Notification, SourceConnection, utcnow
from app.services.canvas_configuration import canvas_profile_id, canvas_settings
from app.services.canvas_lifecycle import (
    ExpirationState,
    expiration_status,
    new_pat_metadata,
)
from app.sources.canvas_client import CanvasAPIError, CanvasClient, CanvasErrorCode
from app.sources.config import configured_auth_profiles


class CanvasCredentialReplacementError(ValueError):
    """A candidate PAT was rejected while the current credential stayed intact."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AuthService:
    def __init__(
        self,
        settings: Settings,
        store: CredentialStore = credential_store,
        fetcher: ResourceFetcher = resource_fetcher,
    ) -> None:
        self.settings = settings
        self.installation_settings = settings
        self.store = store
        self.fetcher = fetcher
        from app.services.canvas_oauth import CanvasOAuth, canvas_oauth
        self.oauth = canvas_oauth if store is credential_store else CanvasOAuth(store)

    def ensure_profiles(self, db: Session) -> list[CredentialProfile]:
        self.settings = canvas_settings(db, self.installation_settings)
        configured = configured_auth_profiles(self.settings)
        # A saved source edit is authoritative over its original YAML seed.
        for connection in db.scalars(select(SourceConnection).where(
            SourceConnection.source_type == "website", SourceConnection.credential_id.is_not(None)
        )):
            config = connection.config_json or {}
            rules = config.get("auth_rules") or []
            rule = next((row for row in rules if (row.get("auth") or {}).get("credential_id") == connection.credential_id), {})
            auth = rule.get("auth") or {}
            configured[connection.credential_id] = {
                "credential_id": connection.credential_id, "auth_type": "ntlm",
                "display_name": f"{connection.name} NTLM",
                "probe_url": config.get("probe_url") or auth.get("probe_url") or connection.base_url,
                "metadata_json": {"base_url": connection.base_url,
                    "protected_path_prefix": config.get("protected_path_prefix") or rule.get("path_prefix")},
            }
        for credential_id, data in configured.items():
            profile = db.scalar(
                select(CredentialProfile).where(
                    CredentialProfile.credential_id == credential_id
                )
            )
            if profile is None:
                profile = CredentialProfile(**data, state="AUTH_REQUIRED")
                db.add(profile)
            else:
                profile.auth_type = data["auth_type"]
                profile.probe_url = data["probe_url"]
                profile.display_name = data["display_name"]
            profile.metadata_json = {**(profile.metadata_json or {}), **data.get("metadata_json", {})}
            stored_mode = profile.metadata_json.get("auth_mode")
            if (
                self.store.canvas_mode(credential_id) is None
                and stored_mode in {mode.value for mode in CanvasAuthMode}
            ):
                self.store.select_canvas_mode(credential_id, stored_mode)
            if profile.auth_type == "canvas_token":
                self._refresh_canvas_lifecycle(db, profile)
            loaded = self._credential_available(profile)
            if not loaded and profile.state in {
                "ACTIVE",
                "VERIFYING",
            }:
                profile.state = "AUTH_REQUIRED"
            if (
                profile.auth_type == "canvas_token"
                and self.store.get_canvas(credential_id) is None
                and not self.store.canvas_blocked(credential_id)
                and self.settings.legacy_canvas_token
                and profile.state != "FAILED"
            ):
                profile.state = "AUTH_REQUIRED"
                profile.metadata_json = {
                    **profile.metadata_json,
                    "auth_source": "environment",
                    "auth_mode": CanvasAuthMode.PAT.value,
                }
        db.commit()
        return list(db.scalars(select(CredentialProfile).order_by(CredentialProfile.credential_id)))

    def _refresh_canvas_lifecycle(
        self,
        db: Session,
        profile: CredentialProfile,
        now: datetime | None = None,
    ) -> None:
        metadata = profile.metadata_json or {}
        status = expiration_status(metadata.get("expires_at"), now)
        if status.state is ExpirationState.EXPIRED and metadata.get("expiration_source") in {"CANVAS_API", "USER_ENTERED"}:
            self.store.remove_canvas_slot(profile.credential_id, CanvasAuthMode.PAT, state="EXPIRED")
            profile.state = "ACTIVE" if self.store.get_canvas(profile.credential_id) else "AUTH_REQUIRED"
            profile.last_error_code = "token_expired"
        effective = self.store.get_canvas(profile.credential_id)
        if effective:
            profile.metadata_json = {**metadata, "auth_mode": effective.auth_mode.value, "auth_source": "memory"}
            profile.state = "ACTIVE"
        if status.state in {
            ExpirationState.WARNING,
            ExpirationState.URGENT,
            ExpirationState.CRITICAL,
            ExpirationState.EXPIRED,
        }:
            self._record_expiration_notification(db, profile, status.state, status.days_remaining)

    @staticmethod
    def _record_expiration_notification(
        db: Session,
        profile: CredentialProfile,
        state: ExpirationState,
        days_remaining: int | None,
    ) -> Notification:
        lifecycle = (profile.metadata_json or {}).get("expiration_lifecycle_id") or (
            profile.metadata_json or {}
        ).get("expires_at", "unknown")
        key = hashlib.sha256(
            f"canvas-expiration:{profile.credential_id}:{lifecycle}:{state.value}".encode()
        ).hexdigest()
        existing = db.scalar(select(Notification).where(Notification.dedupe_key == key))
        if existing:
            return existing
        expired = state is ExpirationState.EXPIRED
        notification = Notification(
            level="critical" if state in {ExpirationState.CRITICAL, ExpirationState.EXPIRED} else "important",
            title="Canvas API token expired" if expired else "Canvas API token expires soon",
            body=(
                "Replace the Canvas PAT or switch to Browser Session."
                if expired
                else f"Canvas API token has {days_remaining} day(s) remaining. Replace it in Sources."
            ),
            dedupe_key=key,
            status="pending",
        )
        db.add(notification)
        db.flush()
        return notification

    def reset_after_restart(self, db: Session) -> None:
        self.store.clear_all()
        self.fetcher.ntlm.invalidate_all()
        db.execute(
            update(CredentialProfile)
            .where(CredentialProfile.state.in_(["ACTIVE", "VERIFYING"]))
            .values(state="AUTH_REQUIRED")
        )
        canvas_profile = db.scalar(
            select(CredentialProfile).where(
                CredentialProfile.credential_id == canvas_profile_id(db, self.settings)
            )
        )
        if canvas_profile:
            canvas_profile.state = "AUTH_REQUIRED"
            canvas_profile.metadata_json = {
                **(canvas_profile.metadata_json or {}),
                "auth_source": "not_loaded",
            }
        stored_mode = (
            (canvas_profile.metadata_json or {}).get("auth_mode")
            if canvas_profile
            else None
        )
        if canvas_profile and stored_mode == CanvasAuthMode.BROWSER_SESSION.value:
            self.store.select_canvas_mode(
                canvas_profile.credential_id, CanvasAuthMode.BROWSER_SESSION
            )
        db.commit()

    def canvas_status(self, db: Session) -> dict:
        self.ensure_profiles(db)
        profile = db.scalar(
            select(CredentialProfile).where(
                CredentialProfile.credential_id == canvas_profile_id(db, self.settings)
            )
        )
        if profile is None:
            return {"configured": False, "credential_state": "NOT_CONFIGURED"}
        sources = list(
            db.scalars(
                select(CourseSource).where(CourseSource.source_type == "canvas")
            )
        )
        source_health = "UNKNOWN"
        if sources:
            states = {source.state.upper() for source in sources if source.enabled}
            source_health = (
                "HEALTHY"
                if states and states <= {"HEALTHY"}
                else "DEGRADED"
            )
        metadata = profile.metadata_json or {}
        account = metadata.get("account") or {}
        available = self.store.get_canvas(profile.credential_id) is not None
        effective = self.store.canvas_mode(profile.credential_id).value if available else None
        credentials = {mode.value: self.store.slot_status(profile.credential_id, mode) for mode in CanvasAuthMode}
        credentials["pat"].update({"expires_at": metadata.get("expires_at"),
                                   "expiration_source": metadata.get("expiration_source", "UNKNOWN"),
                                   "origin": metadata.get("pat_origin", "memory")})
        return {
            "configured": True,
            "auth_mode": effective or profile.auth_mode,
            "credential_state": "ACTIVE" if available else "AUTH_REQUIRED",
            "connection_state": "ACTIVE" if available else "AUTH_REQUIRED",
            "preferred_method": self.store.preferred_mode(profile.credential_id).value, "effective_method": effective,
            "backup_ready": sum(row["present"] and row["state"] == "VALID" for row in credentials.values()) >= 2,
            "credentials": credentials, "fallback": self.store.fallback_status(profile.credential_id),
            "source_health": source_health,
            "verified": profile.state == "ACTIVE" and available,
            "verified_at": profile.last_verified_at,
            "account_id": str(account.get("id")) if account.get("id") else None,
            "account_display_name": account.get("name") or profile.username,
            "expires_at": profile.expires_at if profile.auth_mode == "pat" else None,
            "days_remaining": (
                profile.days_remaining if profile.auth_mode == "pat" else None
            ),
            "expiration_state": (
                profile.expiration_state if profile.auth_mode == "pat" else "unknown"
            ),
            "expiration_source": (
                profile.expiration_source if profile.auth_mode == "pat" else None
            ),
        }

    def _credential_available(self, profile: CredentialProfile) -> bool:
        if profile.auth_type == "canvas_token":
            return self.canvas_credential(profile.credential_id) is not None
        return self.store.get_ntlm(profile.credential_id) is not None

    def canvas_credential(
        self, credential_id: str | None = None
    ) -> CanvasCredentialValue | None:
        credential_id = credential_id or self.settings.canvas_credential_id
        in_memory = self.store.get_canvas(credential_id)
        if in_memory is not None:
            return in_memory
        if self.store.canvas_blocked(credential_id):
            return None
        token = self.settings.legacy_canvas_token
        if token:
            return CanvasCredential(self.settings.canvas_base_url.rstrip("/"), token)
        return None

    def routed_client(self, db: Session):
        from app.sources.canvas_routing import RoutedCanvasClient

        profile = db.scalar(select(CredentialProfile).where(CredentialProfile.credential_id == canvas_profile_id(db, self.settings)))
        if profile is None or self.store.get_canvas(profile.credential_id) is None:
            return None
        return RoutedCanvasClient(self.store, profile.credential_id,
            (profile.metadata_json or {}).get("account", {}).get("id"),
            refresh_oauth=lambda: self.oauth.refresh(self.settings, profile.credential_id,
                (profile.metadata_json or {}).get("account", {}).get("id")),
            max_concurrency=self.settings.canvas_max_concurrency)

    def profile(self, db: Session, credential_id: str) -> CredentialProfile | None:
        self.ensure_profiles(db)
        return db.scalar(
            select(CredentialProfile).where(
                CredentialProfile.credential_id == credential_id
            )
        )

    async def set_and_verify(
        self,
        db: Session,
        profile: CredentialProfile,
        username: str,
        password: str,
    ) -> bool:
        previous = self.store.get_ntlm(profile.credential_id)
        if not password:
            if previous is None or (username and username != previous.username):
                raise ValueError("Enter a password; no matching credential is loaded")
            candidate = previous
        else:
            if not username.strip():
                raise ValueError("Username is required")
            candidate = NtlmCredential(username.strip(), password)
        # The trial slot and its connection pool cannot displace the live secret.
        trial_id = f"ntlm-candidate-{uuid4().hex}"
        trial_store = CredentialStore()
        trial_store.set_ntlm(trial_id, candidate.username, candidate.password)
        trial = ResourceFetcher(store=trial_store, anonymous=self.fetcher.anonymous,
                                ntlm=self.fetcher.ntlm)
        rule = self._ntlm_rule(profile).model_copy(update={"credential_id": trial_id})
        try:
            result = await trial.fetch(profile.probe_url, rule)
        finally:
            trial_store.clear(trial_id)
            self.fetcher.ntlm.invalidate(trial_id)
        verified = result.status is FetchStatus.OK and result.authenticated
        public_only = result.status is FetchStatus.OK and not result.authenticated
        if public_only and previous is not None and candidate is not previous:
            raise ValueError("Protected access not verified; existing credential kept")
        if not verified and not public_only:
            if previous is not None:
                raise ValueError("Candidate authentication failed; existing credential kept")
            profile.state = "FAILED"
            profile.last_error_code = result.error or result.status.value
            profile.last_failure_at = utcnow()
            db.commit()
            return False
        if not self.store.replace_ntlm(profile.credential_id, previous, candidate):
            raise ValueError("Credential changed during verification; retry")
        self.fetcher.ntlm.invalidate(profile.credential_id)
        profile.username = candidate.username
        profile.state = "ACTIVE" if verified else "AUTH_REQUIRED"
        profile.last_verified_at = utcnow() if verified else None
        profile.last_error_code = None if verified else "protected_access_not_verified"
        db.commit()
        return verified

    @staticmethod
    def _ntlm_rule(profile: CredentialProfile) -> AuthRule:
        metadata = profile.metadata_json or {}
        base = metadata.get("base_url") or profile.probe_url
        prefix = metadata.get("protected_path_prefix")
        if not prefix:
            prefix = urlsplit(base).path or "/"
            if not prefix.endswith("/") and "." in prefix.rsplit("/", 1)[-1]:
                prefix = prefix.rsplit("/", 1)[0] + "/"
        return AuthRule(path_prefix=prefix, auth_type=profile.auth_type,
                        credential_id=profile.credential_id, probe_url=profile.probe_url,
                        base_url=base)

    async def set_canvas_and_verify(
        self,
        db: Session,
        profile: CredentialProfile,
        base_url: str,
        token: str,
        expiration_date: date | None = None,
    ) -> bool:
        normalized_base = base_url.rstrip("/")
        current = self.canvas_credential(profile.credential_id)
        replacing = current is not None
        try:
            account = await CanvasClient(
                normalized_base,
                token,
                self.settings.canvas_max_concurrency,
            ).probe()
        except CanvasAPIError as exc:
            if replacing:
                raise CanvasCredentialReplacementError(
                    "replacement_invalid",
                    "Replacement PAT could not be verified; the current PAT remains active.",
                ) from None
            profile.last_failure_at = utcnow()
            profile.last_error_code = exc.code.value
            profile.state = (
                "FAILED"
                if exc.code
                in {CanvasErrorCode.INVALID_TOKEN, CanvasErrorCode.PERMISSION_DENIED}
                else "DEGRADED"
            )
            db.commit()
            return False

        current_account_id = str(
            ((profile.metadata_json or {}).get("account") or {}).get("id") or ""
        )
        candidate_account_id = str(account.get("id") or "")
        if current_account_id and (candidate_account_id != current_account_id or
                (profile.metadata_json or {}).get("base_url", normalized_base) != normalized_base):
            raise CanvasCredentialReplacementError(
                "account_mismatch",
                "Replacement PAT belongs to a different Canvas account; the current PAT remains active.",
            )

        previous_pat = self.store.canvas_slot(profile.credential_id, CanvasAuthMode.PAT)
        same_pat = isinstance(previous_pat, CanvasCredential) and previous_pat.token == token
        metadata = (dict(profile.metadata_json or {}) if same_pat and expiration_date is None else
                    new_pat_metadata(profile.metadata_json or {}, expiration_date=expiration_date))
        self.store.set_canvas(profile.credential_id, normalized_base, token)
        self.store.bind_identity(profile.credential_id, "pat", candidate_account_id)
        profile.probe_url = f"{normalized_base}/api/v1/users/self/profile"
        profile.state = "ACTIVE"
        profile.last_verified_at = utcnow()
        profile.last_error_code = None
        profile.username = str(
            account.get("name") or account.get("short_name") or "Canvas account"
        )
        profile.metadata_json = {
            **metadata,
            "account": {
                "id": candidate_account_id,
                "name": account.get("name") or account.get("short_name"),
                "login_id": account.get("login_id"),
            },
            "auth_source": "memory",
            "auth_mode": CanvasAuthMode.PAT.value,
            "base_url": normalized_base,
            "pat_origin": "memory",
        }
        db.commit()
        return True

    async def set_canvas_session_and_verify(
        self,
        db: Session,
        profile: CredentialProfile,
        base_url: str,
        cookies: dict[str, str],
    ) -> bool:
        candidate = CanvasBrowserSessionCredential(base_url.rstrip("/"), dict(cookies))
        try:
            account = await CanvasClient(candidate.base_url, credential=candidate).probe()
        except CanvasAPIError as exc:
            if self.canvas_credential(profile.credential_id):
                raise CanvasCredentialReplacementError("replacement_invalid", "Session could not be verified; current credentials remain available.") from None
            profile.state = "AUTH_REQUIRED" if exc.code is CanvasErrorCode.AUTH_REQUIRED else "FAILED" if exc.code is CanvasErrorCode.PERMISSION_DENIED else "DEGRADED"
            self.store.select_canvas_mode(profile.credential_id, CanvasAuthMode.BROWSER_SESSION)
            profile.last_error_code = exc.code.value
            profile.last_failure_at = utcnow()
            self.record_auth_required(db, profile.credential_id, profile.display_name)
            db.commit()
            return False
        metadata = profile.metadata_json or {}
        existing_id = str((metadata.get("account") or {}).get("id") or "")
        if existing_id and (str(account["id"]) != existing_id or
                            metadata.get("base_url", candidate.base_url) != candidate.base_url):
            raise CanvasCredentialReplacementError("account_mismatch", "Session must belong to the configured Canvas account and origin.")
        self.store.set_canvas_session(
            profile.credential_id, base_url.rstrip("/"), cookies
        )
        self.store.bind_identity(profile.credential_id, "browser_session", str(account["id"]))
        profile.probe_url = f"{base_url.rstrip('/')}/api/v1/users/self/profile"
        profile.state = "ACTIVE"
        profile.last_verified_at = utcnow()
        profile.last_error_code = None
        profile.metadata_json = {
            **(profile.metadata_json or {}),
            "auth_source": "memory",
            "auth_mode": self.store.get_canvas(profile.credential_id).auth_mode.value,
            "account": {"id": str(account["id"]), "name": account.get("name")},
            "base_url": base_url.rstrip("/"),
        }
        db.commit()
        return True

    async def verify(self, db: Session, profile: CredentialProfile) -> bool:
        if profile.auth_type == "canvas_token":
            return await self._verify_canvas(db, profile)
        if self.store.get_ntlm(profile.credential_id) is None:
            profile.state = "AUTH_REQUIRED"
            profile.last_error_code = "credential_not_loaded"
            db.commit()
            return False
        rule = self._ntlm_rule(profile)
        profile.state = "VERIFYING"
        db.commit()
        result = await self.fetcher.fetch(profile.probe_url, rule)
        if result.status is FetchStatus.OK and result.authenticated:
            profile.state = "ACTIVE"
            profile.last_verified_at = utcnow()
            profile.last_error_code = None
            db.commit()
            return True
        if result.status is FetchStatus.OK:
            profile.state = "AUTH_REQUIRED"
            profile.last_error_code = "protected_access_not_verified"
            profile.last_verified_at = None
            db.commit()
            return False
        profile.last_failure_at = utcnow()
        profile.last_error_code = result.error or result.status.value
        if result.status in {FetchStatus.AUTH_FAILED, FetchStatus.AUTH_REQUIRED}:
            profile.state = "FAILED"
            self.store.clear(profile.credential_id)
            self.fetcher.ntlm.invalidate(profile.credential_id)
            self.record_auth_required(db, profile.credential_id, profile.display_name)
        else:
            profile.state = "FAILED"
        db.commit()
        return False

    async def _verify_canvas(
        self, db: Session, profile: CredentialProfile, mode: str | None = None
    ) -> bool:
        credential = self.store.canvas_slot(profile.credential_id, mode) if mode else self.canvas_credential(profile.credential_id)
        if credential is None:
            profile.state = "AUTH_REQUIRED"
            profile.last_error_code = "credential_not_loaded"
            db.commit()
            return False
        profile.state = "VERIFYING"
        profile.last_error_code = None
        db.commit()
        generation = self.store.generation(profile.credential_id, credential.auth_mode)
        try:
            # A health probe must never borrow another credential's success.
            if credential.auth_mode == CanvasAuthMode.OAUTH:
                await self.oauth.refresh(self.settings, profile.credential_id,
                    (profile.metadata_json or {}).get("account", {}).get("id"))
                refreshed = self.store.canvas_slot(profile.credential_id, "oauth")
                if refreshed is None:
                    raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED, "OAuth authorization expired")
                credential = refreshed
                generation = self.store.generation(profile.credential_id, "oauth")
            client = CanvasClient(credential.base_url, credential=credential,
                                  max_concurrency=self.settings.canvas_max_concurrency)
            account = await client.probe()
        except CanvasAPIError as exc:
            profile.last_failure_at = utcnow()
            profile.last_error_code = exc.code.value
            if exc.code in {CanvasErrorCode.AUTH_REQUIRED, CanvasErrorCode.INVALID_TOKEN} and self.store.canvas_slot(profile.credential_id, credential.auth_mode) is not None:
                self.store.remove_canvas_slot(profile.credential_id, credential.auth_mode, generation)
            # Routed reads own generation-specific invalidation. Never clear a
            # replacement or a healthy backup in this outer consumer.
            available = self.store.get_canvas(profile.credential_id) is not None
            profile.state = "ACTIVE" if available else "AUTH_REQUIRED"
            if not available and exc.code in {CanvasErrorCode.AUTH_REQUIRED, CanvasErrorCode.INVALID_TOKEN}:
                self.record_auth_required(db, profile.credential_id, profile.display_name)
            db.commit()
            return False
        metadata = profile.metadata_json or {}
        bound = str((metadata.get("account") or {}).get("id") or "")
        if bound and (str(account.get("id")) != bound or metadata.get("base_url", credential.base_url) != credential.base_url):
            profile.last_error_code = "account_mismatch"
            db.commit()
            return False
        if self.store.generation(profile.credential_id, credential.auth_mode) != generation:
            return False
        if self.store.get_canvas(profile.credential_id) is None and isinstance(credential, CanvasCredential):
            self.store.set_canvas(profile.credential_id, credential.base_url, credential.token)
            profile.metadata_json = {**metadata, "pat_origin": "environment"}
        self.store.mark_verified(profile.credential_id, credential.auth_mode)
        self.store.bind_identity(profile.credential_id, credential.auth_mode, str(account["id"]))
        profile.state = "ACTIVE"
        profile.last_verified_at = utcnow()
        profile.last_error_code = None
        profile.username = str(account.get("name") or account.get("short_name") or "Canvas account")
        profile.metadata_json = {
            **(profile.metadata_json or {}),
            "account": {
                "id": str(account.get("id")),
                "name": account.get("name") or account.get("short_name"),
                "login_id": account.get("login_id"),
            },
            "base_url": credential.base_url,
            "auth_source": (
                "memory"
                if self.store.get_canvas(profile.credential_id) is not None
                else "environment"
            ),
            "auth_mode": (self.store.get_canvas(profile.credential_id) or credential).auth_mode.value,
        }
        if (
            isinstance(credential, CanvasCredential)
            and not profile.metadata_json.get("expires_at")
        ):
            profile.metadata_json = new_pat_metadata(profile.metadata_json)
        db.commit()
        return True

    def clear(self, db: Session, profile: CredentialProfile) -> None:
        self.store.clear(profile.credential_id)
        if profile.auth_type == "ntlm":
            self.fetcher.ntlm.invalidate(profile.credential_id)
        if profile.auth_type == "canvas_token":
            self.store.block_canvas(profile.credential_id)
            for mode in CanvasAuthMode:
                self.store.remove_canvas_slot(profile.credential_id, mode, state="DISABLED")
        profile.state = "AUTH_REQUIRED"
        profile.last_error_code = "credential_cleared"
        profile.metadata_json = {**(profile.metadata_json or {}), "auth_source": "not_loaded"}
        db.commit()

    def remove_canvas_method(self, db: Session, profile: CredentialProfile, mode: str, generation: int | None = None) -> None:
        if generation is not None and generation != self.store.generation(profile.credential_id, mode):
            raise CanvasCredentialReplacementError("stale_generation", "Credential was replaced; refresh before removing it")
        self.store.remove_canvas_slot(profile.credential_id, mode, generation, state="DISABLED")
        effective = self.store.get_canvas(profile.credential_id)
        profile.state = "ACTIVE" if effective else "AUTH_REQUIRED"
        profile.metadata_json = {**(profile.metadata_json or {}),
            "auth_mode": effective.auth_mode.value if effective else None,
            "auth_source": "memory" if effective else "not_loaded"}
        db.commit()

    def mark_runtime_failure(
        self, db: Session, credential_id: str, error_code: str
    ) -> None:
        profile = db.scalar(
            select(CredentialProfile).where(
                CredentialProfile.credential_id == credential_id
            )
        )
        if profile is None:
            return
        if profile.auth_type == "canvas_token":
            available = self.store.get_canvas(credential_id) is not None
            profile.state = "ACTIVE" if available else "AUTH_REQUIRED"
            profile.last_failure_at = utcnow()
            profile.last_error_code = error_code
            if not available and error_code in {"auth_failed", "auth_required", "invalid_token"}:
                self.record_auth_required(db, credential_id, profile.display_name)
            db.commit()
            return
        selected_mode = self.store.canvas_mode(credential_id)
        browser_session = selected_mode is CanvasAuthMode.BROWSER_SESSION
        if error_code in {"auth_failed", "auth_required", "invalid_token"}:
            profile.state = "AUTH_REQUIRED" if browser_session else "FAILED"
            self.store.clear(credential_id)
            if browser_session:
                profile.metadata_json = {
                    **(profile.metadata_json or {}),
                    "auth_source": "not_loaded",
                }
            if profile.auth_type == "ntlm":
                self.fetcher.ntlm.invalidate(credential_id)
        elif error_code == "permission_denied":
            profile.last_failure_at = utcnow()
            profile.last_error_code = error_code
            db.commit()
            return
        elif error_code in {"timeout", "network_error", "rate_limited", "server_error"}:
            profile.state = "DEGRADED"
        elif profile.state != "FAILED":
            profile.state = "AUTH_REQUIRED"
        profile.last_failure_at = utcnow()
        profile.last_error_code = error_code
        self.record_auth_required(db, credential_id, profile.display_name)
        db.commit()

    @staticmethod
    def record_auth_required(
        db: Session, credential_id: str, display_name: str | None = None
    ) -> Notification:
        key = hashlib.sha256(f"auth-required:{credential_id}".encode()).hexdigest()
        existing = db.scalar(select(Notification).where(Notification.dedupe_key == key))
        if existing:
            return existing
        notification = Notification(
            level="important",
            title=f"{display_name or credential_id} authentication required",
            body=(
                "Protected course resources require authentication. "
                "Public course monitoring remains active."
            ),
            dedupe_key=key,
            status="pending",
        )
        db.add(notification)
        db.flush()
        return notification
