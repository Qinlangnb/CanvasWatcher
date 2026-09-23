import asyncio
import hashlib
import inspect
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .browser import BrowserLogin, LoginFailure, Profiles
from .config import Config

logger = logging.getLogger(__name__)


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    challenge_id: str = Field(min_length=16, max_length=128)
    instance_id: str = Field(min_length=16, max_length=128)
    provider: Literal["canvas", "gradescope", "prairielearn", "all"]
    one_time_token: SecretStr


@dataclass(repr=False)
class Job:
    token_hash: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    state: str = "LOGIN_OPENED"


def create_app(config: Config | None = None, *, login=None, transport=None):
    config = config or Config.from_env()
    profiles = Profiles(config.profiles, config.backend_origin)
    login = login or BrowserLogin(profiles)
    jobs: dict[str, Job] = {}
    start_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for job in jobs.values():
            if job.task and not job.task.done():
                job.task.cancel()
        await asyncio.gather(*(j.task for j in jobs.values() if j.task), return_exceptions=True)

    app = FastAPI(title="Academic Watcher Auth Broker", version="0.7.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    pna = {"allow_private_network": True} if "allow_private_network" in inspect.signature(CORSMiddleware).parameters else {}
    app.add_middleware(CORSMiddleware, allow_origins=list(config.frontend_origins),
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-AW-Broker"],
                       allow_credentials=False, **pna)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        hosts = {f"127.0.0.1:{config.port}", f"localhost:{config.port}", f"[::1]:{config.port}"}
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"detail": "AUTH_HOST_DENIED"}, status_code=403)
        origin = request.headers.get("origin")
        if origin not in config.frontend_origins:
            return JSONResponse({"detail": "AUTH_ORIGIN_DENIED"}, status_code=403)
        if request.method == "POST" and (request.headers.get("x-aw-broker") != "1"
                or request.headers.get("content-type", "").split(";")[0] != "application/json"):
            return JSONResponse({"detail": "AUTH_REQUEST_DENIED"}, status_code=403)
        response = await call_next(request)
        if (request.method == "OPTIONS" and response.status_code == 200
                and request.headers.get("access-control-request-private-network") == "true"):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse({"detail": "AUTH_REQUEST_INVALID"}, status_code=422)

    @app.get("/health")
    async def health():
        return {"state": "BROKER_AVAILABLE", "protocol_version": 1, "version": "0.7.0"}

    async def backend(path: str, body: dict):
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=35,
                                     follow_redirects=False) as client:
            response = await client.post(config.backend_origin + "/api/auth/broker/" + path,
                                         json=body, headers={"X-AW-Broker": "1"})
            if response.status_code != 200:
                raise LoginFailure("AUTH_CHALLENGE_INVALID")
            return response.json()

    async def perform(ticket: Ticket, target: dict, job: Job):
        identity = {"challenge_id": ticket.challenge_id, "instance_id": ticket.instance_id,
                    "provider": ticket.provider, "exchange_token": target["exchange_token"]}
        cookies = {}
        phase = "browser_login"
        try:
            async with asyncio.timeout(min(300, target["expires_in"])):
                if target.get("operation") == "clear_all":
                    profiles.clear_all()
                elif target.get("operation") == "clear":
                    profiles.clear(target)
                else:
                    job.state = "WAITING_FOR_USER"
                    cookies = await login.run(target, job.cancel)
                if job.cancel.is_set():
                    raise LoginFailure("AUTH_USER_CANCELLED")
                phase = "backend_verification"
                await backend("complete", {**identity, "cookies": cookies})
                job.state = "AUTHENTICATED"
        except (TimeoutError, asyncio.CancelledError):
            job.state = "CANCELLED" if job.cancel.is_set() else "TIMED_OUT"
            try:
                await backend("failed", {**identity, "reason": "AUTH_USER_CANCELLED" if job.cancel.is_set() else "AUTH_LOGIN_TIMEOUT"})
            except Exception:
                pass
        except Exception as exc:
            # Exception text/tracebacks can contain cookies, URLs or capabilities.
            logger.warning("Broker login failed: phase=%s error_type=%s", phase, type(exc).__name__)
            job.state = "FAILED"
            try:
                await backend("failed", {**identity, "reason": "AUTH_LOGIN_FAILED"})
            except Exception:
                pass
        finally:
            cookies.clear()
            target.pop("exchange_token", None)
            identity.clear()

    @app.post("/login")
    async def start(ticket: Ticket):
        if start_lock.locked():
            raise HTTPException(409, "AUTH_BROKER_BUSY")
        async with start_lock:
            return await start_locked(ticket)

    async def start_locked(ticket: Ticket):
        # One visible login at a time prevents profile-lock races and popup flooding.
        if any(j.task and not j.task.done() for j in jobs.values()):
            raise HTTPException(409, "AUTH_BROKER_BUSY")
        token = ticket.one_time_token.get_secret_value()
        try:
            target = await backend("claim", {"challenge_id": ticket.challenge_id,
                "instance_id": ticket.instance_id, "provider": ticket.provider, "one_time_token": token})
        except Exception:
            raise HTTPException(403, "AUTH_CHALLENGE_INVALID") from None
        if target.get("protocol_version") != 1 or target.get("provider") != ticket.provider:
            raise HTTPException(409, "AUTH_BROKER_VERSION_UNSUPPORTED")
        jobs.clear()
        job = Job(hashlib.sha256(token.encode()).hexdigest())
        jobs[ticket.challenge_id] = job
        job.task = asyncio.create_task(perform(ticket, target, job))
        return {"state": "LOGIN_OPENED"}

    @app.post("/cancel")
    async def cancel(ticket: Ticket):
        job = jobs.get(ticket.challenge_id)
        if job is None or not secrets.compare_digest(job.token_hash,
                hashlib.sha256(ticket.one_time_token.get_secret_value().encode()).hexdigest()):
            raise HTTPException(403, "AUTH_CHALLENGE_INVALID")
        job.cancel.set()
        if job.task and not job.task.done():
            job.task.cancel()
        return {"state": "CANCELLED"}

    app.state.jobs = jobs
    return app
