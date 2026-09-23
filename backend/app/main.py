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


app = FastAPI(title="Academic Watcher", version="0.7.1", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def safe_ai_validation_error(request: Request, error: RequestValidationError):
    if request.url.path.startswith(("/api/auth/broker", "/api/auth/canvas/oauth", "/api/provider-sources")):
        return JSONResponse(status_code=422, content={"detail": "AUTH_REQUEST_INVALID"})
    if request.url.path.startswith("/api/settings/ai"):
        return JSONResponse(status_code=422, content={"detail": "Invalid AI settings. Select a provider and check the input fields."})
    return await request_validation_exception_handler(request, error)


app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)
app.include_router(broker_router)
app.include_router(canvas_oauth_router)
app.include_router(provider_sources_router)
app.include_router(change_filters_router)
