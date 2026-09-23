import asyncio

from apscheduler.schedulers.background import BackgroundScheduler

from app.ai.queue import AnalysisWorker
from app.config import Settings
from app.db import SessionLocal
from app.services.google_calendar import GoogleCalendarError, GoogleCalendarService
from app.services.sync import SyncService


def start_scheduler(settings: Settings) -> BackgroundScheduler | None:
    if not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone="UTC")

    def run_sync() -> None:
        async def cycle() -> None:
            with SessionLocal() as db:
                await SyncService(settings).run(db)

        asyncio.run(cycle())

    def run_analysis() -> None:
        with SessionLocal() as db:
            asyncio.run(AnalysisWorker(settings).run_pending(db))

    def run_calendar_sync() -> None:
        async def cycle() -> None:
            with SessionLocal() as db:
                try:
                    await GoogleCalendarService(settings).sync(db)
                except GoogleCalendarError:
                    # State and a sanitized diagnostic are persisted by the service.
                    return

        asyncio.run(cycle())

    scheduler.add_job(run_sync, "interval", minutes=2, id="canvas_fast_sync", max_instances=1, coalesce=True)
    scheduler.add_job(
        run_analysis,
        "interval",
        seconds=30,
        id="ai_analysis_queue",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_calendar_sync,
        "interval",
        minutes=settings.calendar_sync_interval_minutes,
        id="google_calendar_incremental_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
