"""Invitation-only public entry point; tenant backends remain private."""

import html
import ipaddress
import json
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from aw_gateway.broker_map import BrokerMap, BrokerMappingError
from aw_gateway.config import GatewayConfig, load_private_key
from aw_gateway.proxy import TenantUnavailable, buffered_tenant_request, proxy_to_tenant
from aw_gateway.security import (
    csrf_token,
    issue_token,
    token_digest,
    verify_csrf,
    verify_legacy_password,
    verify_password,
)
from aw_gateway.store import IdentityError, IdentityStore

_AUTH_STYLE = """
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#101713;color:#f4f6ed;
font:16px/1.5 system-ui,sans-serif}main{width:min(520px,calc(100vw - 32px));padding:clamp(20px,5vw,32px);
background:#1a231d;border:1px solid #364336;border-radius:20px}h1{font-size:1.6rem;margin:0 0 18px}
p{color:#b9c8bc}label{display:block;margin:13px 0 5px}input{box-sizing:border-box;width:100%;padding:11px 12px;
border:1px solid #657363;border-radius:9px;background:#111813;color:#fff;font:inherit}button{margin-top:20px;
background:#c8f470;color:#10200d;border:0;border-radius:10px;padding:11px 18px;font:inherit;cursor:pointer}
a{color:#d7ff91;text-underline-offset:3px}a:hover{color:#f3ffda}a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #d7ff91;outline-offset:3px}
.error{color:#ffb5ae}.hint{font-size:.9rem}.row{display:flex;justify-content:space-between}
.account-links{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}.nav-link{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 14px;border:1px solid #789472;border-radius:10px;background:#243629;color:#f4f8ee;text-decoration:none;font-weight:650}
.nav-link:hover{background:#34523a;border-color:#c8f470;color:#fff}.account-meta{overflow-wrap:anywhere}.account-actions{margin:0}.account-actions button{margin-top:8px}
"""


def _page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    content = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · Academic Watcher</title><style>{_AUTH_STYLE}</style>"
        f"</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )
    response = HTMLResponse(content, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def _set_session_cookies(response, config: GatewayConfig, key: bytes, token: str, *, bootstrap=False):
    seconds = 600 if bootstrap else 7 * 86400
    secure = not config.development
    response.set_cookie(
        "aw_session", token, max_age=seconds, path=config.base_path, secure=secure,
        httponly=True, samesite="lax",
    )
    response.set_cookie(
        "aw_csrf", csrf_token(key, token), max_age=seconds, path=config.base_path,
        secure=secure, httponly=False, samesite="lax",
    )


def _clear_session_cookies(response, config: GatewayConfig):
    response.delete_cookie("aw_session", path=config.base_path)
    response.delete_cookie("aw_csrf", path=config.base_path)


def _session(request: Request, store: IdentityStore):
    return store.session(request.cookies.get("aw_session", ""))


def _same_origin(request: Request, config: GatewayConfig) -> bool:
    return request.headers.get("origin", "") == config.public_origin


def _client_address(request: Request) -> str:
    peer = request.client.host if request.client is not None else "unknown"
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        forwarded = request.headers.get("x-real-ip", "")
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return str(address)
    return str(address)


def _canonical_api_rest(rest: str) -> bool:
    return bool(rest) and "\\" not in rest and all(segment not in {"", ".", ".."} for segment in rest.split("/"))


def _csrf_ok(request: Request, config: GatewayConfig, key: bytes, presented: str) -> bool:
    token = request.cookies.get("aw_session", "")
    cookie = request.cookies.get("aw_csrf", "")
    return (
        bool(token)
        and bool(presented)
        and cookie == presented
        and verify_csrf(key, token, presented)
        and _same_origin(request, config)
    )


async def _form(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
        raise IdentityError("Invalid form content type")
    chunks = bytearray()
    async for part in request.stream():
        chunks.extend(part)
        if len(chunks) > 16 * 1024:
            raise IdentityError("Form too large")
    values = parse_qs(chunks.decode("utf-8"), max_num_fields=12, errors="strict")
    if any(len(items) != 1 for items in values.values()):
        raise IdentityError("Duplicate form field")
    return {key: items[0] for key, items in values.items()}


async def _limited_json(request: Request, *, limit: int) -> tuple[dict, bytes]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise ValueError("Invalid JSON content type")
    chunks = bytearray()
    async for part in request.stream():
        chunks.extend(part)
        if len(chunks) > limit:
            raise ValueError("JSON request too large")
    value = json.loads(chunks)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value, bytes(chunks)


def create_app(config: GatewayConfig, *, csrf_key: bytes | None = None, store: IdentityStore | None = None):
    secret = csrf_key if csrf_key is not None else load_private_key(config.csrf_key_file)
    if len(secret) < 32:
        raise ValueError("CSRF key is too short")
    identity = store or IdentityStore(config.database_path, slots=config.slots)
    identity.initialize()
    broker_map = BrokerMap()
    app = FastAPI(title="Academic Watcher Identity Gateway", docs_url=None, redoc_url=None)
    expected_host = urlsplit(config.public_origin).netloc

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        if request.headers.get("host", "") != expected_host:
            return JSONResponse({"detail": "INVALID_HOST"}, status_code=421)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/watcher/health")
    def health():
        return {"status": "ready"}

    @app.get("/watcher/login")
    def login_page(request: Request):
        current = _session(request, identity)
        if current:
            destination = "/watcher/change-password" if current["session_kind"] == "bootstrap" else "/watcher/"
            return RedirectResponse(destination, status_code=303)
        body = (
            "<form method='post' action='/watcher/login'>"
            "<label for='username'>Username</label><input id='username' name='username' autocomplete='username' required>"
            "<label for='password'>Password</label><input id='password' name='password' type='password' autocomplete='current-password' required>"
            "<label for='bootstrap'>First sign-in code (migrated owner only)</label>"
            "<input id='bootstrap' name='bootstrap_code' autocomplete='off'>"
            "<button type='submit'>Sign in</button></form>"
            "<p class='hint'>New users need an invitation code from the administrator.</p>"
            "<a href='/watcher/register'>Register with an invitation</a>"
        )
        return _page("Sign in", body)

    @app.post("/watcher/login")
    async def login(request: Request):
        if not _same_origin(request, config):
            return _page("Sign-in failed", "<p class='error'>Security check failed.</p>", status_code=403)
        try:
            form = await _form(request)
        except (IdentityError, ValueError):
            return _page("Sign-in failed", "<p class='error'>Sign-in failed.</p>", status_code=400)
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        code = str(form.get("bootstrap_code", ""))
        rate_key = f"login:{_client_address(request)}:{username.lower()}"
        if not identity.record_login_attempt(rate_key):
            return _page("Sign-in failed", "<p class='error'>Please try again later.</p>", status_code=429)
        user = identity.find_user(username)
        if user is None or user["status"] != "active":
            return _page("Sign-in failed", "<p class='error'>Invalid username or password.</p>", status_code=401)
        session_token = issue_token()
        if user["must_change_password"]:
            if not user["legacy_hash"] or not verify_legacy_password(password, user["legacy_hash"]):
                return _page("Sign-in failed", "<p class='error'>Invalid username or password.</p>", status_code=401)
            if user["bootstrap_claimed_at"] is not None:
                return _page("First sign-in complete", "<p class='error'>This account has already completed its first sign-in.</p>", status_code=409)
            if not code:
                return _page("Code required", "<p class='error'>Enter the first sign-in code.</p>", status_code=403)
            if not identity.claim_owner(user["id"], code, session_token):
                return _page("Sign-in failed", "<p class='error'>Invalid username or password.</p>", status_code=401)
            response = RedirectResponse("/watcher/change-password", status_code=303)
            _set_session_cookies(response, config, secret, session_token, bootstrap=True)
        else:
            if not user["password_hash"] or not verify_password(password, user["password_hash"]):
                return _page("Sign-in failed", "<p class='error'>Invalid username or password.</p>", status_code=401)
            try:
                identity.create_session(user["id"], session_token)
            except IdentityError:
                return _page("Sign-in failed", "<p class='error'>Invalid username or password.</p>", status_code=401)
            response = RedirectResponse("/watcher/", status_code=303)
            _set_session_cookies(response, config, secret, session_token)
        identity.clear_login_attempts(rate_key)
        return response

    @app.get("/watcher/change-password")
    def change_password_page(request: Request):
        session = _session(request, identity)
        if session is None or session["session_kind"] != "bootstrap":
            return RedirectResponse("/watcher/login", status_code=303)
        csrf = csrf_token(secret, request.cookies["aw_session"])
        body = (
            "<p>Set a new password to complete your first sign-in. Your old access password will then stop working.</p>"
            "<form method='post' action='/watcher/change-password'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
            "<label for='new_password'>New password (at least 12 characters)</label>"
            "<input id='new_password' name='new_password' type='password' autocomplete='new-password' required>"
            "<label for='confirm_password'>Confirm new password</label>"
            "<input id='confirm_password' name='confirm_password' type='password' autocomplete='new-password' required>"
            "<button type='submit'>Set new password</button></form>"
        )
        return _page("Change password", body)

    @app.post("/watcher/change-password")
    async def change_password(request: Request):
        session = _session(request, identity)
        if session is None or session["session_kind"] != "bootstrap":
            return JSONResponse({"detail": "AUTH_REQUIRED"}, status_code=401)
        try:
            form = await _form(request)
        except (IdentityError, ValueError):
            return _page("Change password", "<p class='error'>Invalid input.</p>", status_code=400)
        if not _csrf_ok(request, config, secret, str(form.get("csrf", ""))):
            return JSONResponse({"detail": "CSRF_REQUIRED"}, status_code=403)
        password = str(form.get("new_password", ""))
        if password != str(form.get("confirm_password", "")):
            return _page("Change password", "<p class='error'>Passwords do not match.</p>", status_code=400)
        new_token = issue_token()
        try:
            identity.finish_owner_password_change(request.cookies["aw_session"], password, new_token)
        except (IdentityError, ValueError):
            return _page("Change password", "<p class='error'>The password does not meet the requirements, or the session expired.</p>", status_code=400)
        response = RedirectResponse("/watcher/", status_code=303)
        _set_session_cookies(response, config, secret, new_token)
        return response

    @app.get("/watcher/register")
    def register_page():
        body = (
            "<p>Registration requires an administrator invitation. The code is used only when you submit this form.</p>"
            "<form method='post' action='/watcher/register'>"
            "<label for='invite'>Invitation code</label><input id='invite' name='invite' required>"
            "<label for='email'>Invited email</label><input id='email' name='email' type='email' required>"
            "<label for='username'>Username</label><input id='username' name='username' required>"
            "<label for='password'>Password (at least 12 characters)</label>"
            "<input id='password' name='password' type='password' autocomplete='new-password' required>"
            "<button type='submit'>Register</button></form><a href='/watcher/login'>Back to sign in</a>"
        )
        return _page("Invitation registration", body)

    @app.post("/watcher/register")
    async def register(request: Request):
        if not _same_origin(request, config):
            return JSONResponse({"detail": "INVALID_ORIGIN"}, status_code=403)
        try:
            form = await _form(request)
            token = str(form.get("invite", ""))
            address = _client_address(request)
            if not identity.record_login_attempt("register-ip:" + address, limit=50):
                raise IdentityError("Too many attempts")
            if not identity.record_login_attempt("register:" + address + ":" + token_digest(token), limit=8):
                raise IdentityError("Too many attempts")
            identity.redeem_invitation(
                token, str(form.get("username", "")), str(form.get("email", "")),
                str(form.get("password", "")),
            )
        except (IdentityError, ValueError):
            return _page("Registration failed", "<p class='error'>Invalid invitation or account details.</p>", status_code=400)
        return _page(
            "Registration submitted", "<p>Your account is being initialized. You can sign in when it is ready.</p><a href='/watcher/login'>Back to sign in</a>",
            status_code=202,
        )

    @app.get("/watcher/account")
    def account(request: Request):
        user = _session(request, identity)
        if user is None:
            return RedirectResponse("/watcher/login", status_code=303)
        if user["session_kind"] == "bootstrap":
            return RedirectResponse("/watcher/change-password", status_code=303)
        csrf = csrf_token(secret, request.cookies["aw_session"])
        admin = "<a class='nav-link' href='/watcher/admin'>Manage invitations</a>" if user["role"] == "admin" else ""
        body = (
            f"<p class='account-meta'>{html.escape(user['username'])} · {html.escape(user['email'])}</p>"
            "<nav class='account-links' aria-label='Account navigation'>"
            "<a class='nav-link' href='/watcher/'>Back to dashboard</a>" + admin + "</nav>"
            "<form class='account-actions' method='post' action='/watcher/logout'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
            "<button type='submit'>Sign out</button></form>"
        )
        return _page("Account", body)

    @app.post("/watcher/logout")
    async def logout(request: Request):
        try:
            form = await _form(request)
        except (IdentityError, ValueError):
            return JSONResponse({"detail": "INVALID_FORM"}, status_code=400)
        if not _csrf_ok(request, config, secret, str(form.get("csrf", ""))):
            return JSONResponse({"detail": "CSRF_REQUIRED"}, status_code=403)
        identity.revoke_session(request.cookies["aw_session"])
        response = RedirectResponse("/watcher/login", status_code=303)
        _clear_session_cookies(response, config)
        return response

    @app.get("/watcher/admin")
    def admin_page(request: Request):
        user = _session(request, identity)
        if user is None or user["role"] != "admin" or user["session_kind"] != "normal":
            return JSONResponse({"detail": "ADMIN_REQUIRED"}, status_code=403)
        csrf = csrf_token(secret, request.cookies["aw_session"])
        rows = "".join(
            "<li>"
            f"{html.escape(row['username'])} · {html.escape(row['status'])} · #{row['slot']}"
            + (
                "<form method='post' action='/watcher/admin/disable'>"
                f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
                f"<input type='hidden' name='user_id' value='{html.escape(row['id'])}'>"
                "<button type='submit'>Disable account</button></form>"
                if row["role"] != "admin" and row["status"] != "disabled" else ""
            )
            + "</li>"
            for row in identity.list_users()
        )
        body = (
            f"<ul>{rows}</ul><form method='post' action='/watcher/admin/invite'>"
            f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
            "<label for='email'>Invitee email</label><input id='email' name='email' type='email' required>"
            "<button type='submit'>Create invitation code</button></form>"
            "<nav class='account-links'><a class='nav-link' href='/watcher/account'>Back to account</a></nav>"
        )
        return _page("User management", body)

    @app.post("/watcher/admin/invite")
    async def create_invite(request: Request):
        user = _session(request, identity)
        if user is None or user["role"] != "admin" or user["session_kind"] != "normal":
            return JSONResponse({"detail": "ADMIN_REQUIRED"}, status_code=403)
        try:
            form = await _form(request)
        except (IdentityError, ValueError):
            return JSONResponse({"detail": "INVALID_FORM"}, status_code=400)
        if not _csrf_ok(request, config, secret, str(form.get("csrf", ""))):
            return JSONResponse({"detail": "CSRF_REQUIRED"}, status_code=403)
        token = issue_token()
        try:
            identity.create_invitation(user["id"], str(form.get("email", "")), token)
        except IdentityError:
            return _page("Invitation failed", "<p class='error'>Invalid email or no available account slots.</p>", status_code=400)
        body = (
            "<p>Share this one-time code privately with the invitee. It is shown only now and expires in seven days.</p>"
            f"<p><code>{html.escape(token)}</code></p>"
            f"<p>Registration URL: {html.escape(config.public_origin)}/watcher/register</p>"
            "<nav class='account-links'><a class='nav-link' href='/watcher/admin'>Back to user management</a></nav>"
        )
        return _page("Invitation created", body)

    @app.post("/watcher/admin/disable")
    async def disable_account(request: Request):
        user = _session(request, identity)
        if user is None or user["role"] != "admin" or user["session_kind"] != "normal":
            return JSONResponse({"detail": "ADMIN_REQUIRED"}, status_code=403)
        try:
            form = await _form(request)
        except (IdentityError, ValueError):
            return JSONResponse({"detail": "INVALID_FORM"}, status_code=400)
        if not _csrf_ok(request, config, secret, str(form.get("csrf", ""))):
            return JSONResponse({"detail": "CSRF_REQUIRED"}, status_code=403)
        try:
            identity.disable_user(user["id"], str(form.get("user_id", "")))
        except IdentityError:
            return JSONResponse({"detail": "ACCOUNT_UNAVAILABLE"}, status_code=400)
        return RedirectResponse("/watcher/admin", status_code=303)

    async def oauth_start(request: Request, user, provider: str):
        try:
            upstream = await buffered_tenant_request(request, config=config, slot=user["slot"])
        except (TenantUnavailable, ValueError):
            return JSONResponse({"detail": "OAUTH_UNAVAILABLE"}, status_code=503)
        if upstream.status_code != 200:
            return upstream
        try:
            payload = json.loads(upstream.body)
            authorization_url = payload["authorization_url"]
            parsed = urlsplit(authorization_url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            states = [value for key, value in pairs if key == "state"]
            redirects = [value for key, value in pairs if key == "redirect_uri"]
            expected = config.public_origin + (
                "/watcher/api/auth/canvas/oauth/callback" if provider == "canvas"
                else "/watcher/api/integrations/google-calendar/oauth/callback"
            )
            if (
                parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or len(states) != 1 or not 1 <= len(states[0]) <= 128
                or redirects != [expected]
            ):
                raise ValueError("Invalid OAuth authorization URL")
            outer = issue_token()
            identity.register_oauth_callback(
                outer_state=outer, user_id=user["id"], slot=user["slot"], provider=provider,
                inner_state=states[0], lifetime_seconds=230 if provider == "canvas" else 590,
            )
            pairs = [(key, outer if key == "state" else value) for key, value in pairs]
            public_url = urlunsplit(parsed._replace(query=urlencode(pairs)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, IdentityError):
            return JSONResponse({"detail": "OAUTH_UNAVAILABLE"}, status_code=503)
        return JSONResponse({"authorization_url": public_url})

    async def oauth_callback(request: Request, provider: str):
        states = request.query_params.getlist("state")
        if len(states) != 1 or not 1 <= len(states[0]) <= 128:
            return JSONResponse({"detail": "OAUTH_STATE_INVALID"}, status_code=400)
        mapping = identity.consume_oauth_callback(states[0], provider)
        if mapping is None:
            return JSONResponse({"detail": "OAUTH_STATE_INVALID"}, status_code=400)
        slot, inner = mapping
        pairs = parse_qsl(request.url.query, keep_blank_values=True)
        query = urlencode([(key, inner if key == "state" else value) for key, value in pairs])
        try:
            return await buffered_tenant_request(request, config=config, slot=slot, query=query)
        except (TenantUnavailable, ValueError):
            return JSONResponse({"detail": "OAUTH_UNAVAILABLE"}, status_code=503)

    async def broker_browser(request: Request, user, action: str):
        if request.headers.get("x-aw-broker") != "1":
            return JSONResponse({"detail": "AUTH_ORIGIN_DENIED"}, status_code=403)
        try:
            payload, raw = await _limited_json(request, limit=32 * 1024)
            if action == "challenges":
                upstream = await buffered_tenant_request(
                    request, config=config, slot=user["slot"], body_override=raw,
                )
                if upstream.status_code != 200:
                    return upstream
                ticket = broker_map.create(upstream.body and json.loads(upstream.body),
                                           user_id=user["id"], slot=user["slot"])
                return JSONResponse(ticket)
            slot, inner = broker_map.browser_control(payload, user_id=user["id"])
            return await buffered_tenant_request(
                request, config=config, slot=slot,
                body_override=json.dumps(inner, separators=(",", ":")).encode(),
            )
        except (BrokerMappingError, ValueError, json.JSONDecodeError):
            return JSONResponse({"detail": "AUTH_CHALLENGE_INVALID"}, status_code=409)
        except TenantUnavailable:
            return JSONResponse({"detail": "INSTANCE_UNAVAILABLE"}, status_code=503)

    async def broker_return(request: Request, action: str):
        if (
            request.headers.get("origin") is not None
            or request.headers.get("sec-fetch-site") is not None
            or request.headers.get("x-aw-broker") != "1"
        ):
            return JSONResponse({"detail": "AUTH_BROKER_ONLY"}, status_code=403)
        try:
            payload, _ = await _limited_json(request, limit=2 * 1024 * 1024)
            if action == "claim":
                slot, inner = broker_map.broker_claim(payload)
            else:
                slot, inner = broker_map.broker_completion(payload)
            upstream = await buffered_tenant_request(
                request, config=config, slot=slot,
                body_override=json.dumps(inner, separators=(",", ":")).encode(),
                max_body=2 * 1024 * 1024,
            )
            if upstream.status_code != 200:
                return upstream
            if action == "claim":
                result = json.loads(upstream.body)
                result["exchange_token"] = broker_map.exchange(payload, result["exchange_token"])
                return JSONResponse(result)
            broker_map.finish(payload)
            return upstream
        except (BrokerMappingError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            return JSONResponse({"detail": "AUTH_CHALLENGE_INVALID"}, status_code=409)
        except TenantUnavailable:
            return JSONResponse({"detail": "INSTANCE_UNAVAILABLE"}, status_code=503)

    @app.api_route("/watcher/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def tenant_api(request: Request, rest: str):
        if not _canonical_api_rest(rest):
            return JSONResponse({"detail": "INVALID_API_PATH"}, status_code=400)
        if request.method == "GET" and rest == "auth/canvas/oauth/callback":
            return await oauth_callback(request, "canvas")
        if request.method == "GET" and rest == "integrations/google-calendar/oauth/callback":
            return await oauth_callback(request, "google")
        if request.method == "GET" and rest == "health":
            # Local Auth Broker readiness probe (no session); says nothing about any tenant.
            return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
        if request.method == "POST" and rest in {
            "auth/broker/claim", "auth/broker/failed", "auth/broker/complete",
        }:
            return await broker_return(request, rest.rsplit("/", 1)[1])
        user = _session(request, identity)
        if user is None:
            return JSONResponse({"detail": "AUTH_REQUIRED"}, status_code=401)
        if user["session_kind"] != "normal":
            return JSONResponse({"detail": "PASSWORD_CHANGE_REQUIRED"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not _csrf_ok(request, config, secret, request.headers.get("x-aw-csrf", "")):
                return JSONResponse({"detail": "CSRF_REQUIRED"}, status_code=403)
        if request.method == "POST" and rest == "auth/canvas/oauth/start":
            return await oauth_start(request, user, "canvas")
        if request.method == "POST" and rest == "integrations/google-calendar/connect":
            return await oauth_start(request, user, "google")
        if rest.startswith("auth/broker/"):
            action = rest.rsplit("/", 1)[1]
            if request.method == "GET" and action == "configuration":
                return await proxy_to_tenant(request, config=config, slot=user["slot"])
            if request.method == "POST" and action in {"challenges", "status", "cancel"}:
                return await broker_browser(request, user, action)
            return JSONResponse({"detail": "AUTH_OPERATION_INVALID"}, status_code=404)
        return await proxy_to_tenant(request, config=config, slot=user["slot"])

    @app.get("/watcher")
    def slash_redirect():
        return RedirectResponse("/watcher/", status_code=308)

    @app.get("/watcher/{rest:path}")
    def frontend(request: Request, rest: str):
        user = _session(request, identity)
        if user is None:
            return RedirectResponse("/watcher/login", status_code=303)
        if user["session_kind"] != "normal":
            return RedirectResponse("/watcher/change-password", status_code=303)
        root = config.static_root.resolve()
        target = (root / rest).resolve()
        if rest and target.is_relative_to(root) and target.is_file():
            response = FileResponse(target)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if rest.startswith("assets/") else "no-store"
            return response
        index = root / "index.html"
        if not index.is_file():
            return JSONResponse({"detail": "FRONTEND_NOT_READY"}, status_code=503)
        response = FileResponse(index, media_type="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def create_production_app():
    return create_app(GatewayConfig.from_environment())
