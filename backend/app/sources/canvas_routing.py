"""Read-only failover between independently verified account credentials."""

import asyncio
from contextlib import asynccontextmanager

from app.auth.models import CanvasAuthMode
from app.sources.canvas_client import CanvasAPIError, CanvasClient, CanvasErrorCode


class RoutedCanvasClient(CanvasClient):
    def __init__(self, store, credential_id, account_id, refresh_oauth=None, **kwargs):
        current = store.get_canvas(credential_id)
        super().__init__(current.base_url, credential=current, **kwargs)
        self.store = store
        self.credential_id = credential_id
        self.account_id = str(account_id)
        self.routing_lock = store.routing_lock(credential_id)
        self.clients = {}
        self.refresh_oauth = refresh_oauth
        self.max_concurrency = kwargs.get("max_concurrency", 3)

    @asynccontextmanager
    async def locked_routing(self):
        # Scheduler and API run different event loops. A thread lock acquired
        # non-blockingly provides one process-wide boundary without blocking
        # either loop or leaking an acquisition when a waiter is cancelled.
        while not self.routing_lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self.routing_lock.release()

    def _client(self, mode):
        credential = self.store.canvas_slot(self.credential_id, mode)
        if credential is None:
            raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED, "Canvas authentication required")
        generation = self.store.generation(self.credential_id, mode)
        key = (mode, generation)
        if key not in self.clients:
            self.clients[key] = CanvasClient(credential.base_url, credential=credential,
                                             max_concurrency=self.max_concurrency,
                                             transport=self._transport, sleep=self._sleep)
        return self.clients[key], generation

    async def request(self, path_or_url, **kwargs):
        attempted = set()
        while len(attempted) < len(CanvasAuthMode):
            if self.refresh_oauth and getattr(self.store.get_canvas(self.credential_id), "auth_mode", None) == CanvasAuthMode.OAUTH:
                await self.refresh_oauth()
            current = self.store.get_canvas(self.credential_id)
            if current is None or current.auth_mode in attempted:
                raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED, "Canvas authentication required")
            mode = current.auth_mode
            attempted.add(mode)
            client, generation = self._client(mode)
            self.auth_mode = mode
            try:
                response = await client.request(path_or_url, **kwargs)
                if "json" in response.headers.get("content-type", ""):
                    return response
                sample = response.text[:8192].lower()
                if "text/html" in response.headers.get("content-type", "") and (
                    'type="password"' in sample or "/login" in sample or "saml" in sample
                ):
                    raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED, "Canvas sign-in required")
                return response
            except CanvasAPIError as error:
                if error.code not in {CanvasErrorCode.AUTH_REQUIRED, CanvasErrorCode.INVALID_TOKEN}:
                    raise
                async with self.locked_routing():
                    if self.store.generation(self.credential_id, mode) != generation and not (
                        self.store.generation(self.credential_id, mode) == generation + 1
                        and self.store.canvas_slot(self.credential_id, mode) is None
                        and self.store.slot_status(self.credential_id, mode)["state"] == "INVALID"
                    ):
                        raise CanvasAPIError(CanvasErrorCode.INVALID_RESPONSE, "Credential replaced; retry synchronization") from None
                    # Another request may already have completed this switch.
                    if self.store.canvas_slot(self.credential_id, mode) is not None:
                        try:
                            # The profile endpoint failure itself is the bounded
                            # account probe; do not recursively probe it again.
                            if "/api/v1/users/self/profile" in path_or_url:
                                raise error
                            identity = await client.probe()
                        except CanvasAPIError as probe_error:
                            if probe_error.code not in {CanvasErrorCode.AUTH_REQUIRED,
                                                        CanvasErrorCode.INVALID_TOKEN}:
                                raise probe_error from None
                        else:
                            if str(identity.get("id")) != self.account_id:
                                self.store.remove_canvas_slot(self.credential_id, mode, generation)
                                raise CanvasAPIError(CanvasErrorCode.PERMISSION_DENIED, "Account identity changed") from None
                            self.store.mark_verified(self.credential_id, mode)
                            raise CanvasAPIError(CanvasErrorCode.PERMISSION_DENIED, "Resource is unavailable for this account") from None
                        self.store.remove_canvas_slot(self.credential_id, mode, generation)
                    alternate = self.store.get_canvas(self.credential_id)
                    if alternate is None or alternate.auth_mode in attempted:
                        raise error
                    other, other_generation = self._client(alternate.auth_mode)
                    if not self.store.recently_verified(self.credential_id, alternate.auth_mode):
                        try:
                            identity = await other.probe()
                        except CanvasAPIError as alternate_error:
                            if alternate_error.code in {CanvasErrorCode.AUTH_REQUIRED, CanvasErrorCode.INVALID_TOKEN}:
                                self.store.remove_canvas_slot(self.credential_id, alternate.auth_mode, other_generation)
                                attempted.add(alternate.auth_mode)
                                continue
                            raise
                        if str(identity.get("id")) != self.account_id or other.base_url != self.base_url:
                            self.store.remove_canvas_slot(self.credential_id, alternate.auth_mode, other_generation)
                            raise CanvasAPIError(CanvasErrorCode.PERMISSION_DENIED, "Backup account identity mismatch") from None
                        if self.store.generation(self.credential_id, alternate.auth_mode) != other_generation:
                            raise CanvasAPIError(CanvasErrorCode.INVALID_RESPONSE, "Credential replaced; retry synchronization") from None
                        self.store.mark_verified(self.credential_id, alternate.auth_mode)
        raise CanvasAPIError(CanvasErrorCode.AUTH_REQUIRED, "Canvas authentication required")
