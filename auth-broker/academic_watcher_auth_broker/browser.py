"""Dedicated browser profiles only; never inspect the user's browser profile."""

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .config import provider_origin


def failure_category(error: Exception) -> str:
    """Map untrusted browser errors to fixed labels; never expose their text."""
    message = str(error).lower()
    for markers, category in (
        (("auth_profile_path_invalid",), "profile_path_rejected"),
        (("executable doesn't exist", "executable does not exist"), "browser_missing"),
        (("processsingleton", "profile appears to be in use", "user data directory is already in use"), "profile_busy"),
        (("access is denied", "permission denied", "eacces", "eperm"), "permission_denied"),
        (("filename or extension is too long", "enametoolong"), "path_too_long"),
        (("err_name_not_resolved",), "dns_failed"),
        (("err_cert_",), "certificate_failed"),
        (("err_connection_", "err_proxy_", "err_tunnel_"), "connection_failed"),
        (("target page, context or browser has been closed", "browser closed"), "browser_closed"),
        (("timeout", "timed out"), "timeout"),
    ):
        if any(marker in message for marker in markers):
            return category
    return "unknown"


class LoginFailure(RuntimeError):
    pass


class Profiles:
    def __init__(self, root: Path, backend_origin: str):
        self.root = root
        self.backend_origin = backend_origin

    def path(self, target: dict) -> Path:
        identity = "|".join((self.backend_origin, target["provider"], target["base_url"], target["credential_id"]))
        # Stable namespace identifier, not a source-file integrity check.
        key = hashlib.sha256(identity.encode()).hexdigest()
        root = self.namespace()
        child = root / key
        if child.is_symlink() or child.resolve().parent != root:
            raise LoginFailure("AUTH_PROFILE_PATH_INVALID")
        return child

    def namespace(self) -> Path:
        root = self.root.resolve()
        if self.root.is_symlink():
            raise LoginFailure("AUTH_PROFILE_PATH_INVALID")
        child = root / hashlib.sha256(self.backend_origin.encode()).hexdigest()
        if child.is_symlink() or child.resolve().parent != root:
            raise LoginFailure("AUTH_PROFILE_PATH_INVALID")
        return child

    def clear_all(self):
        namespace = self.namespace()
        if namespace.exists():
            shutil.rmtree(namespace)

    def clear(self, target: dict):
        path = self.path(target)
        if path.exists():
            shutil.rmtree(path)


class BrowserLogin:
    def __init__(self, profiles: Profiles):
        self.profiles = profiles

    async def run(self, target: dict, cancelled: asyncio.Event) -> dict[str, str]:
        try:
            return await self._run(target, cancelled)
        except Exception as error:
            logging.getLogger(__name__).warning("Browser failure category=%s", failure_category(error))
            raise

    async def _run(self, target: dict, cancelled: asyncio.Event) -> dict[str, str]:
        from playwright.async_api import async_playwright

        configured = urlsplit(target["base_url"])
        base = provider_origin(f"{configured.scheme}://{configured.netloc}")
        provider = target["provider"]
        if provider not in {"canvas", "gradescope", "prairielearn"}:
            raise LoginFailure("AUTH_PROVIDER_UNSUPPORTED")
        if configured.query or configured.fragment or configured.path.rstrip("/") not in ({"", "/pl"} if provider == "prairielearn" else {""}):
            raise LoginFailure("AUTH_PROVIDER_UNSUPPORTED")
        entry = base + "/login" if provider == "canvas" else target["base_url"]
        probe = base + "/api/v1/users/self/profile" if provider == "canvas" else entry
        directory = self.profiles.path(target)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        logging.getLogger(__name__).warning("Browser stage=driver_start")
        async with async_playwright() as playwright:
            logging.getLogger(__name__).warning("Browser stage=chromium_launch")
            context = await playwright.chromium.launch_persistent_context(
                str(directory), headless=False, accept_downloads=False,
                ignore_default_args=["--password-store=basic", "--use-mock-keychain"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                launch = target.get("launch_url")
                if launch:
                    parts, expected = urlsplit(launch), urlsplit(provider_origin(target.get("launch_origin", base)))
                    if (parts.scheme, parts.netloc) != (expected.scheme, expected.netloc) or parts.username or parts.password:
                        raise LoginFailure("AUTH_LAUNCH_ORIGIN_INVALID")
                try:
                    await page.goto(launch or entry, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    await page.goto(entry, wait_until="domcontentloaded", timeout=30000)
                    launch = None
                while not cancelled.is_set():
                    if not context.pages:
                        raise LoginFailure("AUTH_USER_CANCELLED")
                    try:
                        # Isolated API probe uses only this profile's cookies, not a PAT.
                        response = await context.request.get(probe,
                                                             max_redirects=0, timeout=10000)
                        if provider == "canvas" and response.status == 200 and "json" in response.headers.get("content-type", ""):
                            account = await response.json()
                            if isinstance(account, dict) and account.get("id"):
                                cookies = await context.cookies([probe])
                                return {row["name"]: row["value"] for row in cookies}
                        elif provider != "canvas" and response.status == 200:
                            html = await response.text()
                            marker = "Course Dashboard" if provider == "gradescope" else "PrairieLearn Homepage"
                            logout = "/logout" if provider == "gradescope" else "/pl/logout"
                            if marker in html and logout in html:
                                cookies = await context.cookies([probe])
                                return {row["name"]: row["value"] for row in cookies}
                    except Exception:
                        # Never log Playwright exceptions: URLs can contain login secrets.
                        pass
                    if launch:
                        launch = None
                        await page.goto(entry, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1)
                raise LoginFailure("AUTH_USER_CANCELLED")
            finally:
                await context.close()
