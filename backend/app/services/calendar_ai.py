from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import (
    CalendarEvent,
    Course,
    StudyAvailabilityOverride,
    StudyAvailabilityRule,
)
from app.schemas import CalendarMutationPlan
from app.services.ai_settings import calendar_chat_provider, sanitized_error
from app.services.course_terms import normalize_course_display
from app.timezone import UIUC_TIMEZONE, as_utc

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def calendar_context(db: Session, now: datetime | None = None) -> dict:
    current = as_utc(now or datetime.now(UTC))
    minimum, maximum = current - timedelta(days=7), current + timedelta(days=45)
    events = list(
        db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.start_at < maximum, CalendarEvent.end_at > minimum)
            .order_by(CalendarEvent.start_at)
            .limit(300)
        )
    )
    return {
        "current_datetime": current.isoformat(),
        "timezone": UIUC_TIMEZONE,
        "weekly_availability": [
            {
                "weekday": row.weekday,
                "start": row.start_local_time,
                "end": row.end_local_time,
                "enabled": row.enabled,
            }
            for row in db.scalars(select(StudyAvailabilityRule).order_by(StudyAvailabilityRule.weekday))
        ],
        "date_overrides": [
            {"date": row.date.isoformat(), "intervals": row.available_intervals}
            for row in db.scalars(
                select(StudyAvailabilityOverride)
                .where(StudyAvailabilityOverride.date >= current.date())
                .order_by(StudyAvailabilityOverride.date)
                .limit(60)
            )
        ],
        "events": [
            {
                "id": row.id,
                "summary": row.summary,
                "start": as_utc(row.start_at).isoformat(),
                "end": as_utc(row.end_at).isoformat(),
                "source": row.source,
                "read_only": row.read_only,
            }
            for row in events
        ],
        "courses": [
            {
                "id": row.id,
                "label": normalize_course_display(row.course_code, row.name, row.term_name or row.term).course_code,
                "commute_minutes": row.commute_minutes,
            }
            for row in db.scalars(
                select(Course).where(Course.lifecycle_state == "ACTIVE").order_by(Course.course_code)
            )
        ],
    }


async def propose_calendar_mutation(db: Session, message: str) -> CalendarMutationPlan:
    provider = calendar_chat_provider(db)
    system = """You plan Calendar changes for Academic Watcher. Return JSON only and match the supplied schema exactly. You may create/update/delete manual events, set weekly availability, set/remove date overrides, and set course commute minutes. Imported Google or ICS events are read-only: never propose changing or deleting them. Every response must require confirmation. Use IANA timezones and explicit ISO-8601 offsets. Do not propose Task, Canvas, OAuth, email, or general agent actions."""
    context = calendar_context(db)
    return await provider.structured_generate(
        system_prompt=system,
        user_prompt=f"Calendar context:\n{json.dumps(context, ensure_ascii=False)}\n\nUser request:\n{message}",
        response_model=CalendarMutationPlan,
    )


def execute_calendar_mutation(db: Session, plan: CalendarMutationPlan) -> dict:
    if not plan.requires_confirmation:
        raise ValueError("Calendar mutation requires confirmation")
    applied: list[dict] = []
    for operation in plan.operations:
        kind = operation.operation
        if kind == "create_manual_event":
            event = operation.event
            row = CalendarEvent(
                calendar_id="manual",
                source="manual",
                summary=event.summary,
                description=event.description,
                location=event.location,
                start_at=as_utc(event.start),
                end_at=as_utc(event.end),
                timezone=event.timezone,
                event_type=event.event_type,
                course_id=event.course_id,
                read_only=False,
            )
            db.add(row)
            db.flush()
            applied.append({"operation": kind, "event_id": row.id})
        elif kind == "update_manual_event":
            row = db.get(CalendarEvent, operation.event_id)
            if row is None or row.source != "manual" or row.read_only:
                raise ValueError("Only manual Academic Watcher events can be updated")
            event = operation.event
            row.summary = event.summary
            row.description = event.description
            row.location = event.location
            row.start_at = as_utc(event.start)
            row.end_at = as_utc(event.end)
            row.timezone = event.timezone
            row.event_type = event.event_type
            row.course_id = event.course_id
            applied.append({"operation": kind, "event_id": row.id})
        elif kind == "delete_manual_event":
            row = db.get(CalendarEvent, operation.event_id)
            if row is None or row.source != "manual" or row.read_only:
                raise ValueError("Only manual Academic Watcher events can be deleted")
            db.delete(row)
            applied.append({"operation": kind, "event_id": operation.event_id})
        elif kind == "set_weekly_availability":
            weekday = WEEKDAYS[operation.weekday]
            db.execute(
                delete(StudyAvailabilityRule).where(StudyAvailabilityRule.weekday == weekday)
            )
            for interval in operation.intervals:
                db.add(
                    StudyAvailabilityRule(
                        weekday=weekday,
                        start_local_time=interval.start,
                        end_local_time=interval.end,
                        enabled=True,
                    )
                )
            applied.append({"operation": kind, "weekday": operation.weekday})
        elif kind == "set_date_override":
            row = db.scalar(
                select(StudyAvailabilityOverride).where(
                    StudyAvailabilityOverride.date == operation.date
                )
            )
            if row is None:
                row = StudyAvailabilityOverride(date=operation.date, available_intervals=[])
                db.add(row)
            row.available_intervals = [item.model_dump() for item in operation.intervals]
            applied.append({"operation": kind, "date": operation.date.isoformat()})
        elif kind == "remove_date_override":
            db.execute(
                delete(StudyAvailabilityOverride).where(
                    StudyAvailabilityOverride.date == operation.date
                )
            )
            applied.append({"operation": kind, "date": operation.date.isoformat()})
        elif kind == "set_course_commute":
            course = db.get(Course, operation.course_id)
            if course is None or course.lifecycle_state != "ACTIVE":
                raise ValueError("Active course was not found")
            course.commute_minutes = operation.minutes
            applied.append({"operation": kind, "course_id": course.id})
        else:  # pragma: no cover - Pydantic rejects this before execution.
            raise ValueError("Unsupported calendar operation")
    db.commit()
    return {"applied": applied, "summary": plan.summary}


async def safe_propose_calendar_mutation(db: Session, message: str) -> dict:
    try:
        plan = await propose_calendar_mutation(db, message)
        return {"available": True, "plan": plan.model_dump(mode="json"), "message": None}
    except Exception as error:
        return {"available": False, "plan": None, "message": sanitized_error(error)}
