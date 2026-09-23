from time import monotonic
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.broker import browser_boundary
from app.auth.store import credential_store
from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.services.auth import AuthService
from app.services.canvas_configuration import canvas_settings
from app.services.canvas_oauth import OAuthError, canvas_oauth, configured

router = APIRouter(prefix="/api/auth/canvas/oauth", tags=["canvas-oauth"])


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_id: str = Field(min_length=1, max_length=120)


@router.get("/capability")
def capability(settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    settings = canvas_settings(db, settings)
    return {"configured": configured(settings), "reason": None if configured(settings) else "CANVAS_OAUTH_NOT_CONFIGURED"}


@router.post("/start", dependencies=[Depends(browser_boundary)])
def start(payload: Start, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    settings = canvas_settings(db, settings)
    profile = AuthService(settings).profile(db, payload.credential_id)
    if not profile or profile.auth_type != "canvas_token":
        raise HTTPException(404, "AUTH_PROFILE_NOT_FOUND")
    try:
        return {"authorization_url": canvas_oauth.begin(settings, profile.credential_id)}
    except OAuthError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/callback", response_class=HTMLResponse)
async def callback(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    settings = canvas_settings(db, settings)
    success = False
    try:
        if request.headers.get("host") != urlsplit(settings.canvas_oauth_redirect_uri).netloc:
            raise OAuthError("CANVAS_OAUTH_FAILED")
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        if not state or len(state) > 128 or not code or len(code) > 8192:
            raise OAuthError("CANVAS_OAUTH_FAILED")
        expires, credential_id, generation, base, _ = canvas_oauth.consume(settings, state)
        credential, account = await canvas_oauth.exchange(settings, {"grant_type": "authorization_code", "code": code})
        profile = AuthService(settings).profile(db, credential_id)
        if not profile or profile.auth_type != "canvas_token":
            raise OAuthError("CANVAS_OAUTH_FAILED")
        with credential_store.atomic():
            db.refresh(profile)
            bound = (profile.metadata_json or {}).get("account", {}).get("id")
            if (monotonic() >= expires or credential_store.generation(credential_id, "oauth") != generation
                    or (bound and (str(bound) != str(account.get("id"))
                        or (profile.metadata_json or {}).get("base_url") != base))):
                raise OAuthError("CANVAS_ACCOUNT_MISMATCH")
            profile.metadata_json = {**(profile.metadata_json or {}), "base_url": base,
                "account": {"id": str(account["id"]), "name": account.get("name")},
                "auth_mode": "oauth", "auth_source": "memory"}
            profile.last_verified_at, profile.state, profile.last_error_code = utcnow(), "ACTIVE", None
            db.commit()
            credential_store.set_canvas_oauth(credential_id, credential)
            credential_store.bind_identity(credential_id, "oauth", str(account["id"]))
        success = True
    except Exception:
        # No provider response, code, state or stack trace is reflected or logged.
        pass
    return HTMLResponse("Canvas connected. You may close this window and return to Academic Watcher."
                        if success else "Canvas authorization failed or expired. Return to Academic Watcher and retry.",
                        status_code=200 if success else 400,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
