from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from app.ai.queue import AnalysisWorker, enqueue_task_analysis
from app.auth.canvas_session import CanvasSessionImportError, parse_canvas_session_import
from app.config import Settings, get_settings
from app.db import (
    AppSetting,
    CalendarEvent,
    ChangeAnalysis,
    ChangeEvent,
    Course,
    CoursePolicy,
    CourseSource,
    CredentialProfile,
    DownloadedFile,
    Notification,
    PendingResource,
    Plan,
    PlanBlock,
    SourceConnection,
    SourceItem,
    SourceSnapshot,
    StudyAvailabilityOverride,
    StudyAvailabilityRule,
    Task,
    TaskAnalysis,
    TaskSourceLink,
    get_db,
    utcnow,
)
from app.schemas import (
    AIChatConfirmationIn,
    AIChatRequest,
    AIProbeCredentialIn,
    AIProbeSettingsIn,
    AvailabilityOverrideIn,
    AvailabilityOverrideOut,
    AvailabilityRuleIn,
    AvailabilityRuleOut,
    CalendarAIRequest,
    CalendarEventIn,
    CalendarEventOut,
    CalendarMutationExecuteIn,
    CanvasAuthStatusOut,
    CanvasBrowserSessionIn,
    CanvasCredentialIn,
    CanvasSourceCreateIn,
    ChangeOut,
    CourseCommuteIn,
    CourseOut,
    CourseSourceOut,
    CourseTermOut,
    CredentialProfileOut,
    CredentialVerificationOut,
    DownloadedFileOut,
    FileClassificationIn,
    GeneralSettingsIn,
    GoogleCalendarSelectionIn,
    NtlmCredentialIn,
    ProgressIn,
    SourceConnectionOut,
    SourceConnectionUpdateIn,
    SourceEnabledIn,
    TaskOut,
    TaskWorkSummaryOut,
    TimeUsedIn,
    TodayOut,
    WebsiteSourceCreateIn,
    WorkHeartbeatIn,
)
from app.services.ai_chat import chat as run_ai_chat
from app.services.ai_chat import history as ai_chat_history
from app.services.ai_chat import resolve_confirmation
from app.services.ai_settings import (
    ai_probe_secrets,
    get_ai_probe_settings,
    list_probe_models,
    probe_selected_model,
    set_ai_credential,
    set_ai_probe_settings,
)
from app.services.auth import AuthService, CanvasCredentialReplacementError
from app.services.calendar_ai import execute_calendar_mutation, safe_propose_calendar_mutation
from app.services.calendar_capacity import (
    ManualCalendarProvider,
    free_capacity_between,
)
from app.services.change_policy import (
    is_system_change_type,
    is_user_facing_event,
    meaningful_value,
)
from app.services.course_terms import (
    choose_default_term,
    normalize_course_display,
    normalize_term,
)
from app.services.downloader import effective_file_type
from app.services.google_calendar import GoogleCalendarError, GoogleCalendarService
from app.services.ics_calendar import (
    ICSRemovalConfirmationRequired,
    ICSValidationError,
    import_ics,
    list_ics_sources,
    preview_ics,
    remove_ics_source,
)
from app.services.planner import Planner
from app.services.source_connections import (
    create_canvas_connection,
    ensure_existing_connections,
    update_source_connection,
)
from app.services.sync import SyncService, sync_state
from app.services.today import TodayEngine
from app.services.work_tracking import (
    active_session,
    record_work_heartbeat,
    set_effective_used_minutes,
    start_task_work,
    stop_task_work,
    task_work_summary,
)
from app.timezone import UIUC_TZ, as_uiuc, as_utc, uiuc_day_bounds

router = APIRouter(prefix="/api")

NOTIFICATION_BANNER_LIMIT = 3
NOTIFICATION_CANDIDATE_LIMIT = 100
NOTIFICATION_BANNER_LEVELS = {"important", "critical"}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.7.1"}


def _course_label_fields(value: Course, *, name_key: str = "course_name") -> dict[str, str]:
    # Derive presentation on direct detail reads too: callers need not visit
    # /courses first to repair an absent or stale display cache.
    display = normalize_course_display(value.course_code, value.name, value.term_name or value.term)
    return {"course_code": display.course_code, name_key: display.name}


def _normalize_course_metadata(value: Course) -> None:
    term = normalize_term(
        value.term_name or value.term,
        term_id=value.term_id,
        start_at=value.term_start_at or value.start_at,
        end_at=value.term_end_at or value.end_at,
    )
    display = normalize_course_display(
        value.course_code, value.name, term.display_name
    )
    value.term_id = term.term_id
    value.term_name = term.display_name
    value.term_start_at = term.start_at
    value.term_end_at = term.end_at
    value.term_sort_key = term.sort_key
    value.display_course_code = display.course_code
    value.display_name = display.name
    value.section = display.section


@router.get("/courses", response_model=list[CourseOut])
def courses(
    term_id: str | None = None,
    archived: bool = False,
    db: Session = Depends(get_db),
):
    query = select(Course).where(
        Course.lifecycle_state == ("ARCHIVED" if archived else "ACTIVE")
    )
    if term_id is not None:
        query = query.where(Course.term_id == term_id)
    values = list(
        db.scalars(query.order_by(Course.term_sort_key.desc(), Course.course_code))
    )
    for value in values:
        _normalize_course_metadata(value)
    db.commit()
    return values


@router.get("/courses/terms", response_model=list[CourseTermOut])
def course_terms(db: Session = Depends(get_db)):
    values = list(db.scalars(select(Course)))
    for value in values:
        _normalize_course_metadata(value)
    grouped: dict[str, dict] = {}
    for value in values:
        if not value.term_id:
            continue
        row = grouped.setdefault(
            value.term_id,
            {
                "term_id": value.term_id,
                "display_name": value.term_name or value.term or "Unknown term",
                "start_at": value.term_start_at,
                "end_at": value.term_end_at,
                "sort_key": value.term_sort_key,
                "active_course_count": 0,
                "archived_course_count": 0,
                "is_default": False,
            },
        )
        key = (
            "archived_course_count"
            if value.lifecycle_state == "ARCHIVED"
            else "active_course_count"
        )
        row[key] += 1
    normalized = [
        normalize_term(
            row["display_name"],
            term_id=row["term_id"],
            start_at=row["start_at"],
            end_at=row["end_at"],
        )
        for row in grouped.values()
        if row["active_course_count"]
    ]
    default_id = choose_default_term(normalized)
    for row in grouped.values():
        row["is_default"] = row["term_id"] == default_id
    db.commit()
    return sorted(grouped.values(), key=lambda row: row["sort_key"], reverse=True)


@router.post("/courses/{course_id}/archive", response_model=CourseOut)
def archive_course(
    course_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    value = db.get(Course, course_id)
    if value is None:
        raise HTTPException(404, "Course not found")
    value.lifecycle_state = "ARCHIVED"
    value.active = False
    value.archived_at = utcnow()
    db.commit()
    Planner(settings.availability_config).rebuild(db)
    db.refresh(value)
    return value


@router.post("/courses/{course_id}/restore", response_model=CourseOut)
def restore_course(
    course_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    value = db.get(Course, course_id)
    if value is None:
        raise HTTPException(404, "Course not found")
    if value.lifecycle_state != "ARCHIVED":
        return value
    value.lifecycle_state = "ACTIVE"
    value.active = True
    value.archived_at = None
    for source in db.scalars(
        select(CourseSource).where(CourseSource.course_id == course_id)
    ):
        if source.enabled:
            source.state = "PENDING_RECONCILIATION"
            source.last_error = None
    db.commit()
    try:
        Planner(settings.availability_config).rebuild(db)
    except Exception:
        db.rollback()
    db.refresh(value)
    return value


@router.get("/courses/{course_id}", response_model=CourseOut)
def course(course_id: int, db: Session = Depends(get_db)):
    value = db.get(Course, course_id)
    if not value:
        raise HTTPException(404, "Course not found")
    return value


@router.get("/sources", response_model=list[CourseSourceOut])
def course_sources(db: Session = Depends(get_db)):
    from app.services.source_health import source_health_view
    return [CourseSourceOut.model_validate(row).model_copy(update=source_health_view(row)) for row in db.scalars(
        select(CourseSource).order_by(CourseSource.source_type, CourseSource.name)
    )]


@router.patch("/sources/{source_id}", response_model=CourseSourceOut)
def update_course_source(
    source_id: int,
    payload: SourceEnabledIn,
    db: Session = Depends(get_db),
):
    source = db.get(CourseSource, source_id)
    if source is None:
        raise HTTPException(404, "Course source not found")
    source.enabled = payload.enabled
    source.state = "DISABLED" if not payload.enabled else "READY"
    db.commit()
    db.refresh(source)
    return source


@router.get("/courses/{course_id}/policy")
def course_policy(course_id: int, db: Session = Depends(get_db)):
    value = db.scalar(select(CoursePolicy).where(CoursePolicy.course_id == course_id))
    if not value:
        raise HTTPException(404, "Course policy not available")
    return {**value.policy_json, "evidence": value.evidence_json, "confidence": value.confidence}


@router.get("/tasks", response_model=list[TaskOut])
def tasks(
    active_only: bool = True,
    course_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
):
    query = select(Task).join(Course, Course.id == Task.course_id)
    if active_only:
        query = query.where(
            Task.status.not_in(["SUBMITTED", "GRADED", "CANCELLED"]),
            Task.local_completed.is_(False),
            Task.ignored_at.is_(None),
            Course.lifecycle_state == "ACTIVE",
        )
    if course_id is not None:
        query = query.where(Task.course_id == course_id)
    if date_from is not None:
        query = query.where(Task.due_at >= date_from)
    if date_to is not None:
        query = query.where(Task.due_at < date_to)
    from app.services.task_links import task_source_links
    rows = db.scalars(query.order_by(Task.due_at.asc().nullslast())).all()
    links = task_source_links(db, [row.id for row in rows])
    return [TaskOut.model_validate(row).model_copy(update=links[row.id]) for row in rows]


@router.get("/tasks/{task_id}")
def task(task_id: int, db: Session = Depends(get_db)):
    value = db.get(Task, task_id)
    if not value:
        raise HTTPException(404, "Task not found")
    analysis = db.scalar(select(TaskAnalysis).where(TaskAnalysis.task_id == task_id))
    from app.services.task_links import task_source_link
    return {"task": TaskOut.model_validate(value).model_copy(update=task_source_link(db, value.id)), "analysis": analysis.analysis_json if analysis else None, "priority": analysis.priority_score if analysis else None, "remaining_effort_hours": analysis.remaining_effort_hours if analysis else None, "rationale": analysis.rationale if analysis else None}


@router.post("/tasks/{task_id}/progress")
def update_progress(task_id: int, payload: ProgressIn, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    from app.services.completion import apply_progress

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if payload.progress_percent is not None:
        try:
            progress, changed = apply_progress(
                db, task, payload.progress_percent, expected_revision=payload.expected_revision,
                action_at=payload.action_at, reopen=payload.reopen,
            )
        except ValueError as error:
            db.rollback()
            raise HTTPException(422, str(error)) from error
        db.commit()
        plan = Planner(settings.availability_config).rebuild(db) if changed else None
        return {"progress_id": progress.id if progress else None, "plan_id": plan.id if plan else None,
                "state_revision": task.state_revision, "applied": changed}
    raise HTTPException(422, "Local progress requires progress_percent; submission status is source-owned")


@router.post("/tasks/{task_id}/ignore", response_model=TaskOut)
def ignore_task(
    task_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task.ignored_at is None:
        opened = active_session(db)
        if opened and opened.task_id == task_id:
            stop_task_work(db, task_id)
        task.ignored_at = utcnow()
        db.commit()
        Planner(settings.availability_config).rebuild(db)
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/unignore", response_model=TaskOut)
def unignore_task(
    task_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task.ignored_at is not None:
        task.ignored_at = None
        db.commit()
        Planner(settings.availability_config).rebuild(db)
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/reestimate", status_code=202)
def reestimate_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    link = db.scalar(
        select(TaskSourceLink)
        .where(TaskSourceLink.task_id == task_id)
        .order_by(TaskSourceLink.id)
        .limit(1)
    )
    snapshot = (
        db.scalar(
            select(SourceSnapshot)
            .where(SourceSnapshot.source_item_id == link.source_item_id)
            .order_by(SourceSnapshot.captured_at.desc())
            .limit(1)
        )
        if link
        else None
    )
    job = enqueue_task_analysis(
        db, task, snapshot.normalized_text if snapshot else task.description, force=True
    )
    db.commit()
    return {"state": "PENDING", "job_id": job.id if job else None}


def _change_semantics(change_type: str) -> tuple[str, str | None]:
    value = change_type.upper()
    if "DEADLINE" in value:
        return "deadline", "due_at"
    if "FILE" in value:
        return "file", "content / sha256"
    if "ANNOUNCEMENT" in value:
        return "announcement", "announcement"
    if "SYLLABUS" in value:
        return "syllabus", "syllabus"
    if "MODULE" in value:
        return "module", "module"
    if is_system_change_type(value):
        return "authentication", "credential"
    if "ASSIGNMENT" in value or "POINT" in value:
        return "assignment", "assignment"
    if "PAGE" in value or "CONTENT" in value or value in {"UPDATED", "PUBLISHED"}:
        return "content", "content"
    return "content", None


def _event_label(change_type: str) -> str:
    value = change_type.upper()
    if "ANNOUNCEMENT" in value:
        if "CREATED" in value:
            return "New source item detected"
        if "REMOVED" in value:
            return "Announcement removed"
        return "Announcement updated"
    if "ASSIGNMENT" in value or "DEADLINE" in value or "POINT" in value:
        if "DEADLINE" in value:
            return "Deadline changed"
        if "PUBLISHED" in value:
            return "Assignment published"
        if "CREATED" in value:
            return "New assignment"
        if "UNPUBLISHED" in value:
            return "Assignment unpublished"
        if "REMOVED" in value:
            return "Assignment removed"
        return "Assignment updated"
    if "FILE" in value:
        if "INVALID" in value:
            return "File validation failed"
        if "RENAMED" in value:
            return "File renamed"
        return "File content updated"
    if "SYLLABUS" in value:
        return "Syllabus updated"
    return "Content updated"


def _generic_summary(value: str | None) -> bool:
    normalized = (value or "").strip().lower().rstrip(".")
    return not normalized or normalized in {
        "new source item detected",
        "source item is no longer present",
        "canvas item is no longer present after full reconciliation",
    } or normalized.startswith("changed fields:")


def _first_sentence(value: str | None) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    if not compact:
        return None
    for separator in (". ", "! ", "? "):
        if separator in compact:
            compact = compact.split(separator, 1)[0] + separator.strip()
            break
    return compact[:240]


def _brief_deadline(value) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return as_uiuc(parsed).strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(value)


def _change_out(db: Session, value: ChangeEvent) -> dict:
    category, field = _change_semantics(value.change_type)
    item = db.get(SourceItem, value.source_item_id)
    course = db.get(Course, item.course_id) if item else None
    old_snapshot = db.get(SourceSnapshot, value.old_snapshot_id) if value.old_snapshot_id else None
    new_snapshot = db.get(SourceSnapshot, value.new_snapshot_id) if value.new_snapshot_id else None
    old_data = old_snapshot.structured_json if old_snapshot else None
    new_data = new_snapshot.structured_json if new_snapshot else None
    code = (
        _course_label_fields(course)["course_code"]
        if course
        else "Unknown course"
    )
    title = item.title if item else None
    change_type = value.change_type.upper()
    candidate = value.ai_summary if not _generic_summary(value.ai_summary) else None

    if "ANNOUNCEMENT" in change_type:
        sentence = _first_sentence(new_snapshot.normalized_text if new_snapshot else None)
        if title and sentence and sentence.lower() not in title.lower() and title.lower() not in sentence.lower():
            candidate = f"{title} — {sentence}"
        else:
            candidate = title or sentence or candidate
    elif "ASSIGNMENT" in change_type or "DEADLINE" in change_type or "POINT" in change_type:
        if "DEADLINE" in change_type:
            due = _brief_deadline((new_data or {}).get("due_at") or (new_data or {}).get("due_date_local"))
            candidate = f"{title or 'Assignment'} deadline changed" + (f" to {due}" if due else "")
        elif "PUBLISHED" in change_type:
            candidate = f"{title or 'Assignment'} was published"
        elif "CREATED" in change_type:
            candidate = f"New assignment: {title}" if title else "New assignment"
        elif "UNPUBLISHED" in change_type or "REMOVED" in change_type:
            candidate = f"{title or 'Assignment'} is no longer available"
        else:
            candidate = candidate or f"{title or 'Assignment'} updated"
    elif "FILE" in change_type and title:
        candidate = candidate or f"{title} content updated"
    else:
        candidate = candidate or title

    if not candidate and not _generic_summary(value.summary):
        candidate = value.summary
    candidate = candidate or _event_label(value.change_type)
    prefix = f"{code}: "
    primary = candidate if candidate.lower().startswith(prefix.lower()) else prefix + candidate
    return {
        **value.__dict__,
        "change_type": (
            "FILE_CHANGE"
            if value.change_type.upper() in {"CANVAS_FILE_UPDATED", "FILE_CHANGED"}
            else value.change_type
        ),
        "category": category,
        "change_field": field,
        "course_code": code,
        "title": title,
        "primary_text": primary,
        "event_label": _event_label(value.change_type),
        "has_meaningful_diff": meaningful_value(old_data) and meaningful_value(new_data),
    }


def _visible_change_prefilter():
    # Snapshot-bearing metadata churn is hidden by is_user_facing_event.
    # Preserve legacy/synthetic events without snapshots, which that policy
    # intentionally keeps visible even if their type contains FILE_METADATA.
    return ~ChangeEvent.change_type.ilike("%FILE_METADATA%") | (
        ChangeEvent.old_snapshot_id.is_(None) & ChangeEvent.new_snapshot_id.is_(None)
    )


@router.get("/changes/unread-count")
def unread_change_count(
    include_archived: bool = False, db: Session = Depends(get_db)
):
    query = (
        select(ChangeEvent)
        .join(SourceItem, SourceItem.id == ChangeEvent.source_item_id)
        .join(Course, Course.id == SourceItem.course_id)
        .where(ChangeEvent.read_at.is_(None))
        .where(_visible_change_prefilter())
    )
    if not include_archived:
        query = query.where(Course.lifecycle_state == "ACTIVE")
    return {"count": sum(1 for value in db.scalars(query) if is_user_facing_event(db, value))}


@router.post("/changes/read-all")
def read_all_changes(
    include_archived: bool = False, db: Session = Depends(get_db)
):
    query = (
        select(ChangeEvent)
        .join(SourceItem, SourceItem.id == ChangeEvent.source_item_id)
        .join(Course, Course.id == SourceItem.course_id)
        .where(ChangeEvent.read_at.is_(None))
        .where(_visible_change_prefilter())
    )
    if not include_archived:
        query = query.where(Course.lifecycle_state == "ACTIVE")
    values = [value for value in db.scalars(query) if is_user_facing_event(db, value)]
    timestamp = utcnow()
    for value in values:
        value.read_at = timestamp
    db.commit()
    return {"updated": len(values)}


@router.post("/changes/{change_id}/read", response_model=ChangeOut)
def read_change(change_id: int, db: Session = Depends(get_db)):
    value = db.get(ChangeEvent, change_id)
    if value is None:
        raise HTTPException(404, "Change not found")
    value.read_at = value.read_at or utcnow()
    db.commit()
    db.refresh(value)
    return _change_out(db, value)


@router.post("/changes/{change_id}/unread", response_model=ChangeOut)
def unread_change(change_id: int, db: Session = Depends(get_db)):
    value = db.get(ChangeEvent, change_id)
    if value is None:
        raise HTTPException(404, "Change not found")
    value.read_at = None
    db.commit()
    db.refresh(value)
    return _change_out(db, value)


@router.get("/changes", response_model=list[ChangeOut])
def changes(
    limit: int = Query(default=100, ge=1, le=500),
    course_id: int | None = None,
    include_archived: bool = False,
    db: Session = Depends(get_db),
):
    query = (
        select(ChangeEvent)
        .join(SourceItem, SourceItem.id == ChangeEvent.source_item_id)
        .join(Course, Course.id == SourceItem.course_id)
        .where(_visible_change_prefilter())
    )
    if course_id is not None:
        query = query.where(SourceItem.course_id == course_id)
    elif not include_archived:
        query = query.where(Course.lifecycle_state == "ACTIVE")
    query = query.order_by(ChangeEvent.detected_at.desc())
    output = []
    for value in db.scalars(query):
        if not is_user_facing_event(db, value):
            continue
        output.append(_change_out(db, value))
        if len(output) >= limit:
            break
    return output


@router.get("/changes/{change_id}")
def change_detail(change_id: int, db: Session = Depends(get_db)):
    change = db.get(ChangeEvent, change_id)
    if not change:
        raise HTTPException(404, "Change not found")
    change.read_at = change.read_at or utcnow()
    db.commit()
    item = db.get(SourceItem, change.source_item_id)
    course = db.get(Course, item.course_id) if item else None
    old_snapshot = db.get(SourceSnapshot, change.old_snapshot_id) if change.old_snapshot_id else None
    new_snapshot = db.get(SourceSnapshot, change.new_snapshot_id) if change.new_snapshot_id else None
    task_link = db.scalar(
        select(TaskSourceLink).where(TaskSourceLink.source_item_id == change.source_item_id)
    )
    related_task = db.get(Task, task_link.task_id) if task_link else None
    file_rows = list(
        db.scalars(
            select(DownloadedFile)
            .where(DownloadedFile.source_item_id == change.source_item_id)
            .order_by(DownloadedFile.version_number.desc())
            .limit(2)
        )
    )

    def snapshot(value: SourceSnapshot | None):
        if value is None:
            return None
        return {
            "id": value.id,
            "captured_at": as_utc(value.captured_at),
            "structured": value.structured_json,
            "normalized_text": value.normalized_text,
        }

    def file_value(value: DownloadedFile | None):
        if value is None:
            return None
        return {
            "id": value.id,
            "filename": value.original_filename,
            "sha256": value.sha256,
            "size_bytes": value.size_bytes,
            "integrity_status": value.integrity_status,
            "source_url": value.source_url,
        }

    category, change_field = _change_semantics(change.change_type)
    presentation = _change_out(db, change)
    show_diff = bool(
        old_snapshot
        and new_snapshot
        and meaningful_value(old_snapshot.structured_json)
        and meaningful_value(new_snapshot.structured_json)
    )
    return {
        "id": change.id,
        "course": {
            "id": course.id,
            **_course_label_fields(course, name_key="name"),
        }
        if course
        else None,
        "title": item.title if item else None,
        "change_type": change.change_type,
        "detected_at": as_utc(change.detected_at),
        "source_type": item.source_type if item else None,
        "source_url": item.url if item else None,
        "resource_url": item.external_id if item else None,
        "importance": change.importance,
        "requires_action": change.requires_action,
        "summary": change.summary,
        "ai_summary": change.ai_summary,
        "primary_text": presentation["primary_text"],
        "event_label": presentation["event_label"],
        "has_meaningful_diff": show_diff,
        "read_at": as_utc(change.read_at),
        "category": category,
        "change_field": change_field,
        "old": snapshot(old_snapshot) if show_diff else None,
        "new": snapshot(new_snapshot) if show_diff else None,
        "old_file": file_value(file_rows[1] if len(file_rows) > 1 else None),
        "new_file": file_value(file_rows[0] if file_rows else None),
        "related_task": TaskOut.model_validate(related_task) if related_task else None,
        "internal_metadata": {
            "change_event_id": change.id,
            "source_item_id": change.source_item_id,
            "old_snapshot_id": change.old_snapshot_id,
            "new_snapshot_id": change.new_snapshot_id,
            "semantic_key": change.semantic_key,
        },
    }


@router.get("/files", response_model=list[DownloadedFileOut])
def files(
    course_id: int | None = None,
    type: Literal[
        "lecture", "homework", "discussion", "reading", "exam", "solution", "other"
    ]
    | None = Query(default=None),
    latest_only: bool = True,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = (
        select(DownloadedFile, Course, SourceItem)
        .join(Course, Course.id == DownloadedFile.course_id)
        .join(SourceItem, SourceItem.id == DownloadedFile.source_item_id)
    )
    if course_id is None:
        query = query.where(Course.lifecycle_state == "ACTIVE")
    if latest_only:
        latest = (
            select(
                DownloadedFile.source_item_id.label("source_item_id"),
                func.max(DownloadedFile.version_number).label("version_number"),
            )
            .group_by(DownloadedFile.source_item_id)
            .subquery()
        )
        query = query.join(
            latest,
            (latest.c.source_item_id == DownloadedFile.source_item_id)
            & (latest.c.version_number == DownloadedFile.version_number),
        )
    if course_id is not None:
        query = query.where(DownloadedFile.course_id == course_id)
    rows = db.execute(query.order_by(Course.course_code, DownloadedFile.downloaded_at.desc()).limit(limit)).all()
    output: list[dict] = []
    seen_content: set[tuple[int, str]] = set()
    for file, course, source_item in rows:
        identity = (course.id, file.sha256)
        if identity in seen_content:
            continue
        seen_content.add(identity)
        effective_type = effective_file_type(file, get_settings().file_rules_config)
        if type is not None and effective_type != type:
            continue
        output.append(
            DownloadedFileOut.model_validate(
                {
                    **file.__dict__,
                    **_course_label_fields(course),
                    "source_deleted": source_item.is_deleted,
                    "effective_type": effective_type,
                }
            ).model_dump()
        )
    return output


@router.patch("/files/{file_id}/classification", response_model=DownloadedFileOut)
def classify_downloaded_file(
    file_id: int,
    payload: FileClassificationIn,
    db: Session = Depends(get_db),
):
    value = db.get(DownloadedFile, file_id)
    if value is None:
        raise HTTPException(404, "File not found")
    value.user_override_type = payload.type
    value.category = payload.type
    course = db.get(Course, value.course_id)
    item = db.get(SourceItem, value.source_item_id)
    db.commit()
    db.refresh(value)
    return {
        **value.__dict__,
        "effective_type": payload.type,
        **_course_label_fields(course),
        "source_deleted": bool(item and item.is_deleted),
    }


@router.get("/files/{file_id}", response_model=DownloadedFileOut)
def file(file_id: int, db: Session = Depends(get_db)):
    value = db.get(DownloadedFile, file_id)
    if not value:
        raise HTTPException(404, "File not found")
    course = db.get(Course, value.course_id)
    source_item = db.get(SourceItem, value.source_item_id)
    return {
        **value.__dict__,
        "effective_type": effective_file_type(value, get_settings().file_rules_config),
        **_course_label_fields(course),
        "source_deleted": bool(source_item and source_item.is_deleted),
    }


@router.get("/files/{file_id}/content")
def file_content(file_id: int, db: Session = Depends(get_db)):
    value = db.get(DownloadedFile, file_id)
    if not value or value.integrity_status != "VALID" or not value.local_path:
        raise HTTPException(404, "Validated file not available")
    path = Path(value.local_path)
    if not path.is_file():
        raise HTTPException(404, "Managed file is missing")
    return FileResponse(path, media_type=value.mime_type, filename=value.original_filename)


@router.get("/plan/today")
def today_plan(db: Session = Depends(get_db)):
    plan = db.scalar(select(Plan).where(Plan.status == "active").order_by(Plan.generated_at.desc()).limit(1))
    if not plan:
        return {"plan": None, "blocks": []}
    start, end = uiuc_day_bounds(datetime.now(UIUC_TZ).date())
    blocks = db.execute(select(PlanBlock, Task).join(Task, Task.id == PlanBlock.task_id).join(Course, Course.id == Task.course_id).where(PlanBlock.plan_id == plan.id, PlanBlock.start_at >= start, PlanBlock.start_at < end, Course.lifecycle_state == "ACTIVE", Task.ignored_at.is_(None), Task.local_completed.is_(False), Task.status.not_in(["SUBMITTED", "GRADED", "CANCELLED"])).order_by(PlanBlock.start_at)).all()
    return {"plan": {"id": plan.id, "generated_at": plan.generated_at}, "blocks": [{"id": block.id, "task_id": task.id, "task_title": task.title, "start_at": block.start_at, "end_at": block.end_at, "objective": block.objective, "status": block.status} for block, task in blocks]}


@router.get("/today", response_model=TodayOut)
def today(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return TodayEngine(settings.daily_study_capacity_minutes).build(db)


@router.get("/tasks/{task_id}/work", response_model=TaskWorkSummaryOut)
def task_work(task_id: int, db: Session = Depends(get_db)):
    try:
        return task_work_summary(db, task_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/tasks/{task_id}/work/start", response_model=TaskWorkSummaryOut)
def start_work(task_id: int, db: Session = Depends(get_db)):
    try:
        result, switched = start_task_work(db, task_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    result["switched_from_task_id"] = switched
    return result


@router.post("/tasks/{task_id}/work/stop", response_model=TaskWorkSummaryOut)
def stop_work(task_id: int, db: Session = Depends(get_db)):
    try:
        return stop_task_work(db, task_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.patch("/tasks/{task_id}/time-used", response_model=TaskWorkSummaryOut)
def update_time_used(task_id: int, payload: TimeUsedIn, db: Session = Depends(get_db)):
    try:
        return set_effective_used_minutes(db, task_id, payload.total_minutes)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/tasks/{task_id}/work/heartbeat", response_model=TaskWorkSummaryOut)
def work_heartbeat(
    task_id: int,
    payload: WorkHeartbeatIn,
    db: Session = Depends(get_db),
):
    try:
        return record_work_heartbeat(
            db,
            task_id,
            progress_percent=payload.progress_percent,
            manual_adjustment_minutes=payload.manual_adjustment_minutes,
            remaining_minutes=payload.remaining_minutes,
            client_timestamp=payload.client_timestamp,
            expected_revision=payload.expected_revision,
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error


@router.get("/work/active")
def active_work(db: Session = Depends(get_db)):
    opened = active_session(db)
    if opened is None:
        return {"active": None}
    result = task_work_summary(db, opened.task_id)
    task_value = db.get(Task, opened.task_id)
    return {
        "active": {
            **result,
            "task_title": task_value.title if task_value else None,
        }
    }


@router.patch("/courses/{course_id}/commute", response_model=CourseOut)
def update_course_commute(
    course_id: int, payload: CourseCommuteIn, db: Session = Depends(get_db)
):
    value = db.get(Course, course_id)
    if value is None:
        raise HTTPException(404, "Course not found")
    value.commute_minutes = payload.commute_minutes
    db.commit()
    db.refresh(value)
    return value


@router.get("/calendar/providers")
def calendar_providers(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    google = GoogleCalendarService(settings).status(db)
    return [
        ManualCalendarProvider(db).health(),
        {"provider": "google", "status": google["state"], "read_only": True},
        {"provider": "ics", "status": "ACTIVE", "read_only": True},
    ]


@router.get("/calendar/view")
def graphical_calendar(start: datetime, end: datetime, db: Session = Depends(get_db)):
    from app.services.calendar_view import calendar_view

    try:
        return calendar_view(db, start, end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/calendar/events", response_model=list[CalendarEventOut])
def calendar_events(db: Session = Depends(get_db), start: datetime | None = None, end: datetime | None = None):
    if start is not None or end is not None:
        from app.services.calendar_view import calendar_view

        if start is None or end is None:
            raise HTTPException(422, "Both range endpoints are required")
        try:
            projection = calendar_view(db, start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return list({row["event"]["id"]: row["event"] for row in projection["instances"]}.values())
    return list(
        db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.status != "cancelled")
            .order_by(CalendarEvent.start_at, CalendarEvent.id)
        )
    )


@router.post("/calendar/events", response_model=CalendarEventOut, status_code=201)
def create_calendar_event(payload: CalendarEventIn, db: Session = Depends(get_db)):
    if payload.course_id is not None and db.get(Course, payload.course_id) is None:
        raise HTTPException(404, "Course not found")
    event_data = payload.model_dump()
    # SQLite does not preserve timezone offsets on DateTime columns. Normalize
    # before persistence so every stored naive value keeps the established UTC
    # database convention.
    event_data["start_at"] = as_utc(payload.start_at)
    event_data["end_at"] = as_utc(payload.end_at)
    value = CalendarEvent(
        calendar_id="manual",
        source="manual",
        external_id=None,
        recurring_event_id=None,
        status="confirmed",
        **event_data,
    )
    db.add(value)
    db.commit()
    db.refresh(value)
    return value


@router.put("/calendar/events/{event_id}", response_model=CalendarEventOut)
def update_calendar_event(
    event_id: int, payload: CalendarEventIn, db: Session = Depends(get_db)
):
    value = db.get(CalendarEvent, event_id)
    if value is None:
        raise HTTPException(404, "Calendar event not found")
    if value.source != "manual" or value.read_only:
        raise HTTPException(409, "Imported events are read-only")
    if payload.course_id is not None and db.get(Course, payload.course_id) is None:
        raise HTTPException(404, "Course not found")
    event_data = payload.model_dump()
    event_data["start_at"] = as_utc(payload.start_at)
    event_data["end_at"] = as_utc(payload.end_at)
    for key, item in event_data.items():
        setattr(value, key, item)
    db.commit()
    db.refresh(value)
    return value


@router.delete("/calendar/events/{event_id}", status_code=204)
def delete_calendar_event(event_id: int, db: Session = Depends(get_db)):
    value = db.get(CalendarEvent, event_id)
    if value is None:
        raise HTTPException(404, "Calendar event not found")
    if value.source != "manual" or value.read_only:
        raise HTTPException(409, "Imported events are read-only")
    db.delete(value)
    db.commit()


@router.get("/calendar/availability", response_model=list[AvailabilityRuleOut])
def availability_rules(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(StudyAvailabilityRule).order_by(
                StudyAvailabilityRule.weekday, StudyAvailabilityRule.start_local_time
            )
        )
    )


@router.put("/calendar/availability", response_model=list[AvailabilityRuleOut])
def replace_availability_rules(
    payload: list[AvailabilityRuleIn], db: Session = Depends(get_db)
):
    db.execute(delete(StudyAvailabilityRule))
    values = [StudyAvailabilityRule(**row.model_dump()) for row in payload]
    db.add_all(values)
    db.commit()
    return values


@router.get("/calendar/availability/overrides", response_model=list[AvailabilityOverrideOut])
def availability_overrides(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(StudyAvailabilityOverride).order_by(StudyAvailabilityOverride.date)
        )
    )


@router.put("/calendar/availability/overrides/{day}", response_model=AvailabilityOverrideOut)
def set_availability_override(
    day: date, payload: AvailabilityOverrideIn, db: Session = Depends(get_db)
):
    if payload.date != day:
        raise HTTPException(422, "Path date and payload date must match")
    value = db.scalar(
        select(StudyAvailabilityOverride).where(StudyAvailabilityOverride.date == day)
    )
    intervals = [row.model_dump() for row in payload.available_intervals]
    if value is None:
        value = StudyAvailabilityOverride(date=day, available_intervals=intervals)
        db.add(value)
    else:
        value.available_intervals = intervals
    db.commit()
    db.refresh(value)
    return value


@router.delete("/calendar/availability/overrides/{day}", status_code=204)
def delete_availability_override(day: date, db: Session = Depends(get_db)):
    value = db.scalar(
        select(StudyAvailabilityOverride).where(StudyAvailabilityOverride.date == day)
    )
    if value is not None:
        db.delete(value)
        db.commit()


@router.get("/calendar/capacity")
def calendar_capacity(
    time_min: datetime = Query(...),
    time_max: datetime = Query(...),
    db: Session = Depends(get_db),
):
    minutes, source = free_capacity_between(db, time_min, time_max)
    return {"minutes": minutes, "source": source, "timezone": "America/Chicago"}


def _google_http_error(error: GoogleCalendarError) -> HTTPException:
    return HTTPException(error.status_code, str(error))


@router.get("/integrations/google-calendar/status")
def google_calendar_status(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    return GoogleCalendarService(settings).status(db)


@router.post("/integrations/google-calendar/connect")
def connect_google_calendar(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    try:
        return {"authorization_url": GoogleCalendarService(settings).begin_oauth(db)}
    except GoogleCalendarError as error:
        raise _google_http_error(error) from error


@router.get("/integrations/google-calendar/oauth/callback")
async def google_calendar_callback(
    code: str | None = None,
    state: str = "",
    error: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        await GoogleCalendarService(settings).finish_oauth(
            db, code=code, state=state, denied_error=error
        )
    except GoogleCalendarError as failure:
        raise _google_http_error(failure) from failure
    return RedirectResponse(
        f"{settings.frontend_url.rstrip('/')}/settings/calendar?google=connected",
        status_code=303,
    )


@router.post("/integrations/google-calendar/calendars/refresh")
async def refresh_google_calendars(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    try:
        return await GoogleCalendarService(settings).refresh_calendars(db)
    except GoogleCalendarError as error:
        raise _google_http_error(error) from error


@router.put("/integrations/google-calendar/calendars")
async def select_google_calendars(
    payload: GoogleCalendarSelectionIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        return await GoogleCalendarService(settings).select_calendars(
            db, payload.calendar_ids
        )
    except GoogleCalendarError as error:
        raise _google_http_error(error) from error


@router.post("/integrations/google-calendar/sync")
async def sync_google_calendars(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    try:
        return await GoogleCalendarService(settings).sync(db)
    except GoogleCalendarError as error:
        raise _google_http_error(error) from error


@router.delete("/integrations/google-calendar", status_code=204)
def disconnect_google_calendar(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    GoogleCalendarService(settings).disconnect(db)


async def _calendar_upload(file: UploadFile, settings: Settings) -> tuple[bytes, str]:
    data = await file.read(settings.ics_max_file_bytes + 1)
    filename = Path(file.filename or "calendar.ics").name
    return data, filename


@router.get("/calendar/ics/imports")
def ics_import_sources(db: Session = Depends(get_db)):
    return list_ics_sources(db)


@router.post("/calendar/ics/preview")
async def preview_calendar_file(
    file: UploadFile = File(...),
    source_id: int | None = Form(default=None),
    timezone_override: str | None = Form(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    data, filename = await _calendar_upload(file, settings)
    try:
        return preview_ics(
            db,
            data,
            filename,
            settings,
            source_id=source_id,
            timezone_override=timezone_override,
        )
    except ICSValidationError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/calendar/ics/import")
async def import_calendar_file(
    file: UploadFile = File(...),
    source_id: int | None = Form(default=None),
    timezone_override: str | None = Form(default=None),
    confirm_removals: bool = Form(default=False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    data, filename = await _calendar_upload(file, settings)
    try:
        return import_ics(
            db,
            data,
            filename,
            settings,
            source_id=source_id,
            timezone_override=timezone_override,
            confirm_removals=confirm_removals,
        )
    except ICSRemovalConfirmationRequired as error:
        raise HTTPException(
            409,
            {
                "message": str(error),
                "reconciliation": error.reconciliation,
                "requires_confirmation": True,
            },
        ) from error
    except ICSValidationError as error:
        raise HTTPException(422, str(error)) from error


@router.delete("/calendar/ics/imports/{source_id}", status_code=204)
def delete_calendar_import(source_id: int, db: Session = Depends(get_db)):
    if not remove_ics_source(db, source_id):
        raise HTTPException(404, "Calendar import source was not found")


@router.post("/calendar/ai/plan")
async def calendar_ai_plan(payload: CalendarAIRequest, db: Session = Depends(get_db)):
    return await safe_propose_calendar_mutation(db, payload.message)


@router.post("/calendar/mutations/execute")
def calendar_mutation_execute(
    payload: CalendarMutationExecuteIn, db: Session = Depends(get_db)
):
    try:
        return execute_calendar_mutation(db, payload.plan)
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error


@router.get("/settings/general")
def general_settings(db: Session = Depends(get_db)):
    value = db.get(AppSetting, "general")
    return {"academic_timezone": (value.value_json if value else {}).get("academic_timezone", "America/Chicago")}


@router.put("/settings/general")
def update_general_settings(payload: GeneralSettingsIn, db: Session = Depends(get_db)):
    value = db.get(AppSetting, "general")
    if value is None:
        value = AppSetting(key="general", value_json={})
        db.add(value)
    value.value_json = payload.model_dump()
    db.commit()
    return value.value_json


@router.get("/settings/ai")
def ai_probe_settings(db: Session = Depends(get_db)):
    return get_ai_probe_settings(db)


@router.put("/settings/ai")
def update_ai_probe_settings(payload: AIProbeSettingsIn, db: Session = Depends(get_db)):
    try:
        return set_ai_probe_settings(db, payload.base_url, payload.model_id, payload.provider)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/settings/ai/credential")
def set_ai_probe_credential(payload: AIProbeCredentialIn, db: Session = Depends(get_db)):
    try:
        return set_ai_credential(db, payload.api_key.get_secret_value(), payload.provider)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.delete("/settings/ai/credential", status_code=204)
def clear_ai_probe_credential():
    ai_probe_secrets.clear()


@router.post("/settings/ai/models")
async def ai_probe_models(db: Session = Depends(get_db)):
    return await list_probe_models(db)


@router.post("/settings/ai/test")
async def test_ai_probe(model_id: str | None = None, db: Session = Depends(get_db)):
    return await probe_selected_model(db, model_id)


@router.post("/ai/chat")
async def ai_chat(
    payload: AIChatRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return await run_ai_chat(db, payload, settings)


@router.get("/ai/chat/conversations/{conversation_id}")
def get_ai_chat_history(conversation_id: str, db: Session = Depends(get_db)):
    try:
        return {"conversation_id": conversation_id, "messages": ai_chat_history(db, conversation_id)}
    except LookupError as error:
        raise HTTPException(404, {"code": "CONVERSATION_NOT_FOUND", "message": str(error)}) from error


@router.post("/ai/chat/confirm")
async def confirm_ai_chat_action(
    payload: AIChatConfirmationIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        return await resolve_confirmation(db, payload.confirmation_id, payload.action, settings)
    except LookupError as error:
        raise HTTPException(404, {"code": "CONFIRMATION_NOT_FOUND", "message": str(error)}) from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, {"code": "CONFIRMATION_ALREADY_RESOLVED", "message": str(error)}) from error


@router.get("/source-connections", response_model=list[SourceConnectionOut])
def source_connections(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    ensure_existing_connections(db, settings)
    values = list(
        db.scalars(select(SourceConnection).order_by(SourceConnection.source_type, SourceConnection.name))
    )
    for value in values:
        if value.credential_id:
            profile = db.scalar(
                select(CredentialProfile).where(CredentialProfile.credential_id == value.credential_id)
            )
            if profile:
                value.state = profile.state
        source_id = (value.config_json or {}).get("course_source_id")
        source = db.get(CourseSource, source_id) if source_id else None
        if source and source.state:
            value.state = source.state.upper()
    db.commit()
    return values


@router.post("/source-connections/canvas", response_model=SourceConnectionOut, status_code=201)
def add_canvas_source(payload: CanvasSourceCreateIn, db: Session = Depends(get_db)):
    try:
        return create_canvas_connection(db, payload.base_url, payload.name)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/source-connections/website", response_model=SourceConnectionOut, status_code=201)
async def add_website_source(payload: WebsiteSourceCreateIn, db: Session = Depends(get_db)):
    from app.services.website_access import save_website

    try:
        return await save_website(db, payload.model_dump(), payload.ntlm)
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    finally:
        if payload.ntlm:
            payload.ntlm.password = type(payload.ntlm.password)("")


@router.post("/source-connections/website/test")
async def test_website_source(payload: WebsiteSourceCreateIn, connection_id: int | None = None,
                              db: Session = Depends(get_db)):
    from app.services.website_access import save_website

    try:
        result = await save_website(db, payload.model_dump(), payload.ntlm,
                                    connection_id=connection_id, test_only=True)
        return {"verified": result.verified, "message": result.message}
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    finally:
        if payload.ntlm:
            payload.ntlm.password = type(payload.ntlm.password)("")


@router.put("/source-connections/{connection_id}", response_model=SourceConnectionOut)
async def edit_source_connection(
    connection_id: int,
    payload: SourceConnectionUpdateIn,
    db: Session = Depends(get_db),
):
    try:
        connection = db.get(SourceConnection, connection_id)
        if connection and connection.source_type == "website":
            from app.services.website_access import save_website

            return await save_website(db, payload.model_dump(exclude_unset=True), payload.ntlm, connection_id=connection_id)
        return await update_source_connection(
            db, connection_id, payload.model_dump()
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from error
    finally:
        if payload.ntlm:
            payload.ntlm.password = type(payload.ntlm.password)("")


@router.get("/notifications")
def notifications(db: Session = Depends(get_db)):
    from app.services.change_policy import analysis_should_notify_user
    candidates = list(
        db.scalars(
            select(Notification)
            .where(Notification.dismissed_at.is_(None))
            .order_by(case((Notification.level == "critical", 0), else_=1), Notification.id.desc())
            .limit(NOTIFICATION_CANDIDATE_LIMIT)
        )
    )
    visible: list[dict[str, object]] = []
    for row in candidates:
        if row.level not in NOTIFICATION_BANNER_LEVELS:
            continue
        if row.change_event_id is not None:
            event = db.get(ChangeEvent, row.change_event_id)
            if event is None or not is_user_facing_event(db, event):
                continue
            analysis = db.scalar(select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id))
            if not analysis_should_notify_user(event, analysis):
                continue
        visible.append({
            "id": row.id,
            "level": row.level,
            "title": row.title,
            "body": row.body,
            "status": row.status,
            "sent_at": row.sent_at,
        })
    # Keep critical approvals in view even when newer lower-priority approvals
    # fill the three slots; retain recency within each banner level.
    visible.sort(key=lambda row: (row["level"] != "critical", -row["id"]))
    return visible[:NOTIFICATION_BANNER_LIMIT]


@router.post("/notifications/{notification_id}/dismiss", status_code=204)
def dismiss_notification(notification_id: int, db: Session = Depends(get_db)):
    value = db.get(Notification, notification_id)
    if value is None:
        raise HTTPException(404, "Notification not found")
    value.dismissed_at = utcnow()
    db.commit()


@router.post("/analysis/run")
async def run_analysis_queue(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    completed = await AnalysisWorker(settings).run_pending(db)
    return {"completed": completed}


@router.post("/plan/rebuild")
def rebuild_plan(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    plan = Planner(settings.availability_config).rebuild(db)
    return {"plan_id": plan.id, "generated_at": plan.generated_at}


@router.post("/sync/run")
async def run_sync(include_canvas: bool = True, include_websites: bool = True, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    service = SyncService(settings)
    state = await service.run(
        db,
        include_canvas=include_canvas,
        include_websites=include_websites,
    )
    return state.__dict__


@router.get("/sync/status")
def sync_status():
    return sync_state.__dict__


def _auth_profile_or_404(
    db: Session, settings: Settings, credential_id: str
) -> tuple[AuthService, CredentialProfile]:
    service = AuthService(settings)
    profile = service.profile(db, credential_id)
    if profile is None:
        raise HTTPException(404, "Credential profile is not configured")
    return service, profile


@router.get("/auth/profiles", response_model=list[CredentialProfileOut])
def auth_profiles(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    return AuthService(settings).ensure_profiles(db)


@router.get("/auth/canvas/status", response_model=CanvasAuthStatusOut)
def canvas_auth_status(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    return AuthService(settings).canvas_status(db)


@router.get("/auth/profiles/{credential_id}", response_model=CredentialProfileOut)
def auth_profile(
    credential_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _, profile = _auth_profile_or_404(db, settings, credential_id)
    return profile


@router.post(
    "/auth/profiles/{credential_id}/ntlm",
    response_model=CredentialVerificationOut,
)
async def set_ntlm_credential(
    credential_id: str,
    payload: NtlmCredentialIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    if profile.auth_type.lower() != "ntlm":
        raise HTTPException(400, "Credential profile is not NTLM")
    password = payload.password.get_secret_value()
    try:
        verified = await service.set_and_verify(
            db, profile, payload.username.strip(), password
        )
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from error
    finally:
        password = ""
        payload.password = type(payload.password)("")
    retried = 0
    if verified:
        retried = await SyncService(settings).retry_pending_resources(
            db, credential_id
        )
    data = CredentialProfileOut.model_validate(profile).model_dump()
    return CredentialVerificationOut(
        **data, verified=verified, pending_retried=retried
    )


@router.post(
    "/auth/profiles/{credential_id}/canvas-token",
    response_model=CredentialVerificationOut,
)
async def set_canvas_credential(
    credential_id: str,
    payload: CanvasCredentialIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    if profile.auth_type.lower() != "canvas_token":
        raise HTTPException(400, "Credential profile is not a Canvas token profile")
    configured, candidate = urlsplit(service.settings.canvas_base_url), urlsplit(payload.base_url)
    if (candidate.scheme, candidate.hostname, candidate.port or 443) != ("https", configured.hostname, configured.port or 443):
        raise HTTPException(400, "PAT is limited to the configured HTTPS Canvas origin")
    token = payload.token.get_secret_value()
    try:
        try:
            verified = await service.set_canvas_and_verify(
                db,
                profile,
                payload.base_url,
                token,
                expiration_date=payload.expiration_date,
            )
        except CanvasCredentialReplacementError as exc:
            raise HTTPException(
                409, {"code": exc.code, "message": str(exc)}
            ) from None
    finally:
        token = ""
        payload.token = type(payload.token)("")
    discovered = items_seen = changes_count = 0
    errors: list[str] = []
    if verified:
        discovered, state = await SyncService(settings).sync_canvas(db)
        items_seen = state.items_seen
        changes_count = state.changes
        errors = state.errors
    data = CredentialProfileOut.model_validate(profile).model_dump()
    return CredentialVerificationOut(
        **data,
        verified=verified,
        discovered_courses=discovered,
        items_seen=items_seen,
        changes=changes_count,
        sync_errors=errors,
    )


@router.post(
    "/auth/profiles/{credential_id}/canvas-browser-session",
    response_model=CredentialVerificationOut,
)
async def set_canvas_browser_session(
    credential_id: str,
    payload: CanvasBrowserSessionIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    if profile.auth_type.lower() != "canvas_token":
        raise HTTPException(400, "Credential profile is not a Canvas profile")
    configured = urlsplit(service.settings.canvas_base_url.rstrip("/"))
    requested = urlsplit(payload.base_url)
    if (requested.scheme, requested.hostname, requested.port or 443) != (
        "https",
        configured.hostname,
        configured.port or 443,
    ):
        raise HTTPException(
            400, "Browser Session import is limited to the configured Canvas origin"
        )

    imported = payload.session_import.get_secret_value()
    cookies: dict[str, str] = {}
    try:
        try:
            cookies = parse_canvas_session_import(imported)
        except CanvasSessionImportError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            verified = await service.set_canvas_session_and_verify(db, profile, payload.base_url, cookies)
        except CanvasCredentialReplacementError as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from None
    finally:
        imported = ""
        payload.session_import = type(payload.session_import)("")
        for name in list(cookies):
            cookies[name] = ""
        cookies.clear()

    discovered = items_seen = changes_count = 0
    errors: list[str] = []
    if verified:
        discovered, state = await SyncService(settings).sync_canvas(db)
        items_seen = state.items_seen
        changes_count = state.changes
        errors = state.errors
    data = CredentialProfileOut.model_validate(profile).model_dump()
    return CredentialVerificationOut(
        **data,
        verified=verified,
        discovered_courses=discovered,
        items_seen=items_seen,
        changes=changes_count,
        sync_errors=errors,
    )


@router.delete("/auth/profiles/{credential_id}/canvas-method/{mode}", status_code=204)
def remove_canvas_method(credential_id: str, mode: Literal["oauth", "pat", "browser_session"],
                         generation: int | None = Query(default=None, ge=0),
                         db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    if profile.auth_type != "canvas_token":
        raise HTTPException(400, "Not a Canvas profile")
    try:
        service.remove_canvas_method(db, profile, mode, generation=generation)
    except CanvasCredentialReplacementError as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from None


@router.post("/auth/profiles/{credential_id}/canvas-method/{mode}/verify")
async def verify_canvas_method(credential_id: str, mode: Literal["oauth", "pat", "browser_session"],
                              db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    if profile.auth_type != "canvas_token":
        raise HTTPException(400, "Not a Canvas profile")
    return {"verified": await service._verify_canvas(db, profile, mode), "method": mode}


@router.delete("/auth/profiles/{credential_id}/credential", status_code=204)
def clear_credential(
    credential_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    service.clear(db, profile)


@router.post(
    "/auth/profiles/{credential_id}/verify",
    response_model=CredentialVerificationOut,
)
async def verify_credential(
    credential_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    service, profile = _auth_profile_or_404(db, settings, credential_id)
    verified = await service.verify(db, profile)
    retried = discovered = items_seen = changes_count = 0
    errors: list[str] = []
    if verified:
        if profile.auth_type == "canvas_token":
            discovered, state = await SyncService(settings).sync_canvas(db)
            items_seen = state.items_seen
            changes_count = state.changes
            errors = state.errors
        else:
            retried = await SyncService(settings).retry_pending_resources(
                db, credential_id
            )
    data = CredentialProfileOut.model_validate(profile).model_dump()
    return CredentialVerificationOut(
        **data,
        verified=verified,
        pending_retried=retried,
        discovered_courses=discovered,
        items_seen=items_seen,
        changes=changes_count,
        sync_errors=errors,
    )


@router.get("/auth/pending")
def pending_resources(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(PendingResource)
        .where(PendingResource.state != "FETCHED")
        .order_by(PendingResource.discovered_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "course_id": row.course_id,
            "source_name": row.source_name,
            "url": row.url,
            "title": row.title,
            "item_type": row.item_type,
            "credential_id": row.credential_id,
            "state": row.state,
            "last_error_code": row.last_error_code,
        }
        for row in rows
    ]
