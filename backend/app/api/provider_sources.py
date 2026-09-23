"""Source configuration only; provider academic endpoints remain read-only."""

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.broker import browser_boundary
from app.auth.provider_sessions import provider_sessions
from app.config import Settings, get_settings
from app.db import Course, CourseSource, CredentialProfile, SourceConnection, Task, get_db
from app.schemas import SourceConnectionOut
from app.sources.gradescope import GradescopeAdapter
from app.sources.gradescope_html import GradescopeParseError
from app.sources.provider_web import ProviderError, provider_base

router = APIRouter(prefix="/api/provider-sources", tags=["provider-sources"])


@router.get("/tasks/{task_id}/facts")
def facts(task_id: int, db: Session = Depends(get_db)):
    from app.services.task_providers import task_provider_facts
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "TASK_NOT_FOUND")
    return {"local_completed": task.local_completed, "effective_submission_state": task.submission_state,
            "effective_due_at": task.due_at, "provider_facts": task_provider_facts(db, task.id)}


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["gradescope", "prairielearn"]
    base_url: str = Field(max_length=512)
    canvas_course_id: int | None = Field(default=None, gt=0)


@router.post("", response_model=SourceConnectionOut, dependencies=[Depends(browser_boundary)])
def create(payload: ProviderCreate, db: Session = Depends(get_db)):
    try:
        base = provider_base(payload.provider, payload.base_url)
    except ProviderError as exc:
        raise HTTPException(422, str(exc)) from None
    key = f"{payload.provider}:{base}"
    existing = db.scalar(select(SourceConnection).where(SourceConnection.external_key == key))
    if existing:
        return existing
    credential_id = f"{payload.provider}-{secrets.token_hex(12)}"
    canvas_source = db.scalar(select(CourseSource).where(CourseSource.course_id == payload.canvas_course_id,
        CourseSource.source_type == "canvas")) if payload.canvas_course_id else None
    if payload.canvas_course_id and (payload.provider != "gradescope" or canvas_source is None):
        raise HTTPException(422, "CANVAS_COURSE_NOT_FOUND")
    name = "Gradescope" if payload.provider == "gradescope" else "PrairieLearn"
    db.add(CredentialProfile(credential_id=credential_id, auth_type="provider_session", display_name=name,
        probe_url=base, state="AUTH_REQUIRED", metadata_json={"provider": payload.provider, "base_url": base,
            "canvas_course_id": canvas_source.external_id if canvas_source else None}))
    connection = SourceConnection(source_type=payload.provider, name=name, external_key=key, base_url=base,
        credential_id=credential_id, state="AUTH_REQUIRED", config_json={"read_only": True})
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def ready(db, connection_id):
    connection = db.get(SourceConnection, connection_id)
    if not connection or connection.source_type not in {"gradescope", "prairielearn"}:
        raise HTTPException(404, "PROVIDER_SOURCE_NOT_FOUND")
    session = provider_sessions.get(connection.credential_id)
    if not session or session.base_url != connection.base_url or session.provider != connection.source_type:
        raise HTTPException(409, "AUTH_REQUIRED")
    if connection.source_type != "gradescope":
        raise HTTPException(409, "PRAIRIELEARN_COURSE_PARSER_NOT_VERIFIED")
    return connection, session


async def discovered_courses(db, connection_id):
    connection, session = ready(db, connection_id)
    try:
        return await GradescopeAdapter(session, "", 0).discover_courses()
    except (ProviderError, GradescopeParseError) as exc:
        connection.state = "AUTH_REQUIRED" if str(exc) == "AUTH_REQUIRED" else "DEGRADED"
        db.commit()
        raise HTTPException(409, str(exc)) from None
    finally:
        session.cookies.clear()


@router.get("/{connection_id}/courses")
async def courses(connection_id: int, db: Session = Depends(get_db)):
    rows = await discovered_courses(db, connection_id)
    for row in rows:
        source = db.scalar(select(CourseSource).where(CourseSource.source_type == "gradescope",
            CourseSource.external_id == f"{connection_id}:{row.external_id}"))
        if source:
            row.metadata["mapped_course_source_id"] = source.id
    return rows


class CourseMapping(BaseModel):
    provider_course_id: str = Field(pattern=r"^\d+$", max_length=80)
    course_id: int = Field(gt=0)


@router.post("/{connection_id}/course-mappings", dependencies=[Depends(browser_boundary)])
async def map_course(connection_id: int, payload: CourseMapping, db: Session = Depends(get_db)):
    rows = await discovered_courses(db, connection_id)
    row = next((row for row in rows if row.external_id == payload.provider_course_id), None)
    course = db.get(Course, payload.course_id)
    if row is None or course is None or course.lifecycle_state != "ACTIVE":
        raise HTTPException(422, "PROVIDER_COURSE_MAPPING_INVALID")
    connection = db.get(SourceConnection, connection_id)
    key = f"{connection_id}:{row.external_id}"
    existing = db.scalar(select(CourseSource).where(CourseSource.source_type == "gradescope", CourseSource.external_id == key))
    if existing and existing.course_id != course.id:
        raise HTTPException(409, "PROVIDER_COURSE_ALREADY_MAPPED")
    if existing is None:
        existing = CourseSource(course_id=course.id, name=f"gradescope:{row.external_id}", source_type="gradescope",
            external_id=key, url=row.metadata["url"], state="UNKNOWN",
            config_json={"provider_course_id": row.external_id, "connection_id": connection_id,
                         "credential_id": connection.credential_id, "mapping_evidence": "user_confirmed"})
        db.add(existing)
        db.commit()
    return {"course_source_id": existing.id, "course_id": course.id, "mapping_evidence": "user_confirmed"}


@router.post("/{connection_id}/sync", dependencies=[Depends(browser_boundary)])
async def sync(connection_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    from app.services.sync import SyncService
    connection, session = ready(db, connection_id)
    sources = list(db.scalars(select(CourseSource).where(CourseSource.source_type == "gradescope", CourseSource.enabled.is_(True))))
    pairs = [(db.get(Course, source.course_id), GradescopeAdapter(session, source.config_json["provider_course_id"], source.id,
              bindings=source.config_json.get("assignment_bindings")))
             for source in sources if source.config_json.get("connection_id") == connection.id]
    if not pairs:
        raise HTTPException(409, "PROVIDER_COURSE_MAPPING_REQUIRED")
    try:
        result = await SyncService(settings).run(db, pairs)
        connection.state = "DEGRADED" if result.errors else "ACTIVE"
        db.commit()
        return {"state": result.state, "items_seen": result.items_seen, "errors": result.errors}
    finally:
        session.cookies.clear()


async def preview_rows(db, connection_id, source_id):
    _connection, session = ready(db, connection_id)
    try:
        source = db.get(CourseSource, source_id)
        if not source or source.source_type != "gradescope" or source.config_json.get("connection_id") != connection_id:
            raise HTTPException(404, "PROVIDER_COURSE_NOT_MAPPED")
        adapter = GradescopeAdapter(session, source.config_json["provider_course_id"], source.id)
        return source, await adapter.fetch_items(db.get(Course, source.course_id))
    except (ProviderError, GradescopeParseError) as exc:
        raise HTTPException(409, str(exc)) from None
    finally:
        session.cookies.clear()


@router.get("/{connection_id}/courses/{source_id}/assignments")
async def assignments(connection_id: int, source_id: int, db: Session = Depends(get_db)):
    from app.services.task_providers import weak_task_candidates
    source, rows = await preview_rows(db, connection_id, source_id)
    return [{"id": row.structured["provider_facts"]["provider_assignment_id"], "title": row.title,
        "due_at": row.structured.get("due_at"),
        "candidates": [{"id": task.id, "title": task.title} for task in weak_task_candidates(db, source.course_id, row.title, row.structured.get("due_at"))],
        "confirmed": (source.config_json.get("assignment_bindings") or {}).get(row.structured["provider_facts"]["provider_assignment_id"])} for row in rows]


class TaskMapping(BaseModel):
    assignment_id: str = Field(pattern=r"^\d+$", max_length=80)
    task_id: int | None = Field(default=None, gt=0)


@router.post("/{connection_id}/courses/{source_id}/task-mappings", dependencies=[Depends(browser_boundary)])
async def map_task(connection_id: int, source_id: int, payload: TaskMapping, db: Session = Depends(get_db)):
    from app.db import SourceItem, TaskSourceLink
    source, rows = await preview_rows(db, connection_id, source_id)
    if not any(row.structured["provider_facts"]["provider_assignment_id"] == payload.assignment_id for row in rows):
        raise HTTPException(422, "PROVIDER_ASSIGNMENT_NOT_VISIBLE")
    task = db.get(Task, payload.task_id) if payload.task_id else None
    if payload.task_id and (not task or task.course_id != source.course_id):
        raise HTTPException(422, "PROVIDER_TASK_MAPPING_INVALID")
    existing = db.scalar(select(TaskSourceLink).join(SourceItem, SourceItem.id == TaskSourceLink.source_item_id)
        .where(SourceItem.course_source_id == source.id, SourceItem.external_id == f"assignment:{payload.assignment_id}"))
    if existing and (payload.task_id is None or existing.task_id != payload.task_id):
        raise HTTPException(409, "PROVIDER_TASK_ALREADY_BOUND")
    source.config_json = {**source.config_json, "assignment_bindings": {
        **(source.config_json.get("assignment_bindings") or {}), payload.assignment_id: payload.task_id or "separate"}}
    db.commit()
    return {"confirmed": True}
