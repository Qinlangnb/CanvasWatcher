from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.broker import router as broker_router
from app.api.canvas_oauth import router as canvas_oauth_router
from app.api.change_filters import router as change_filters_router
from app.api.provider_sources import router as provider_sources_router
from app.api.routes import router
from app.auth.internal_gateway import InternalGatewayAuth, load_internal_token
from app.auth.persistence import restore_credentials
from app.auth.vault import VaultError
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.logging import configure_logging
from app.scheduler import start_scheduler
from app.services.auth import AuthService
from app.services.downloader import FileDownloader
from app.services.source_connections import ensure_existing_connections
from app.sources.config import course_export_configs

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    restore_credentials(settings)
    init_db()
    with SessionLocal() as db:
        auth = AuthService(settings)
        auth.reset_after_restart(db)
        auth.ensure_profiles(db)
        ensure_existing_connections(db, settings)
        FileDownloader(
            settings.download_root,
            settings.file_rules_config,
            course_export_configs(settings),
        ).audit_existing(db)
    scheduler = start_scheduler(settings)
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Academic Watcher", version="0.7.2", lifespan=lifespan)


@app.exception_handler(VaultError)
async def vault_unavailable(request: Request, error: VaultError):
    return JSONResponse(status_code=503, content={'detail': str(error)})


@app.get('/api/settings/credential-storage')
def credential_storage():
    return {'mode': 'server_encrypted' if settings.credential_persistence_enabled else 'memory'}


@app.exception_handler(RequestValidationError)
async def safe_ai_validation_error(request: Request, error: RequestValidationError):
    if request.url.path.startswith(("/api/auth/broker", "/api/auth/canvas/oauth", "/api/provider-sources")):
        return JSONResponse(status_code=422, content={"detail": "AUTH_REQUEST_INVALID"})
    if request.url.path.startswith("/api/settings/ai"):
        return JSONResponse(status_code=422, content={"detail": "Invalid AI settings. Select a provider and check the input fields."})
    return await request_validation_exception_handler(request, error)


app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
if settings.internal_gateway_auth_required:
    app.add_middleware(
        InternalGatewayAuth,
        token=load_internal_token(settings.internal_gateway_token_file),
    )
app.include_router(router)
app.include_router(broker_router)
app.include_router(canvas_oauth_router)
app.include_router(provider_sources_router)
app.include_router(change_filters_router)
