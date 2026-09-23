"""Optional official token-to-web bridge; failure never invalidates API auth."""

from urllib.parse import urlsplit

import httpx

from app.auth.models import CanvasCredential


async def session_url(credential, *, transport=None) -> str | None:
    if not isinstance(credential, CanvasCredential) or not credential.token:
        return None
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=10, follow_redirects=False) as client:
            response = await client.get(credential.base_url + "/login/session_token",
                headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/json"})
        if response.status_code != 200:
            return None
        value = response.json().get("session_url")
        target, base = urlsplit(value), urlsplit(credential.base_url)
        if (not isinstance(value, str) or (target.scheme, target.netloc) != (base.scheme, base.netloc)
                or target.username or target.password or target.fragment or any(ord(c) < 33 or c == "\\" for c in value)):
            return None
        return value
    except Exception:
        return None
