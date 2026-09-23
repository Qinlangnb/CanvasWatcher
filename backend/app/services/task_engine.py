import re
from datetime import UTC, date, datetime

from dateutil.parser import isoparse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    ChangeEvent,
    Deadline,
    SourceItem,
    Task,
    TaskAnalysis,
    TaskSourceLink,
)
from app.services.priority import calculate_priority, urgency_score
from app.timezone import UIUC_TIMEZONE, planning_cutoff


def semantic_task_key(title: str) -> str | None:
    match = re.search(r"\b(?:HW|Homework)\s*0*(\d+)\b", title, re.IGNORECASE)
    return f"homework:{int(match.group(1))}" if match else None


def _local_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _same_deadline(task: Task, due_at: datetime | None, due_date: date | None) -> bool:
    if due_at is not None:
        return task.due_at == due_at
    return task.due_date_local == due_date


def _record_conflict(
    db: Session,
    source_item: SourceItem,
    task: Task,
    due_at: datetime | None,
    due_date: date | None,
) -> None:
    current = task.due_at.isoformat() if task.due_at else str(task.due_date_local)
    incoming = due_at.isoformat() if due_at else str(due_date)
    summary = f"Deadline conflict for {task.title}: kept {current}; incoming source says {incoming}."
    existing = db.scalar(
        select(ChangeEvent).where(
            ChangeEvent.source_item_id == source_item.id,
            ChangeEvent.change_type == "deadline_conflict",
            ChangeEvent.summary == summary,
        )
    )
    if existing is None:
        db.add(
            ChangeEvent(
                source_item_id=source_item.id,
                change_type="deadline_conflict",
                importance="important",
                requires_action=True,
                summary=summary,
            )
        )


def task_from_assignment(db: Session, source_item: SourceItem, structured: dict) -> Task:
    link = db.scalar(
        select(TaskSourceLink).where(TaskSourceLink.source_item_id == source_item.id)
    )
    task = db.get(Task, link.task_id) if link else None
    from app.services.task_providers import strongly_bound_task, task_provider_facts, terminal_state
    if task is None:
        task = strongly_bound_task(db, source_item, structured)
    if task is None and source_item.source_type == "gradescope":
        from app.services.task_providers import weak_task_candidates
        confirmed = structured.get("confirmed_task_id")
        if confirmed:
            candidate = db.get(Task, int(confirmed))
            if candidate is None or candidate.course_id != source_item.course_id:
                raise ValueError("PROVIDER_TASK_MAPPING_INVALID")
            task = candidate
        elif not structured.get("confirmed_separate") and weak_task_candidates(db, source_item.course_id, source_item.title, structured.get("due_at")):
            raise ValueError("PROVIDER_TASK_MAPPING_REQUIRED")
    source_key = structured.get("source_key") or semantic_task_key(source_item.title)
    if task is None and source_key:
        task = db.scalar(
            select(Task).where(
                Task.course_id == source_item.course_id,
                Task.source_key == source_key,
            )
        )
    due_at = isoparse(structured["due_at"]) if structured.get("due_at") else None
    due_date = _local_date(structured.get("due_date_local"))
    assigned_date = _local_date(structured.get("assigned_date_local"))
    precision = structured.get("deadline_precision") or (
        "EXACT_DATETIME" if due_at else "DATE_ONLY" if due_date else None
    )
    timezone = structured.get("deadline_timezone") or UIUC_TIMEZONE
    source_rank = int(
        structured.get(
            "deadline_source_rank",
            2 if source_item.source_type == "canvas" else 4,
        )
    )
    if task is None:
        task = Task(
            course_id=source_item.course_id,
            title=source_item.title,
            source_key=source_key,
        )
        db.add(task)
        db.flush()
    if link is None:
        db.add(
            TaskSourceLink(
                task_id=task.id,
                source_item_id=source_item.id,
                relationship_type="primary",
            )
        )

    task.title = source_item.title
    task.task_type = structured.get("task_type") or "assignment"
    task.description = (structured.get("description") or "") if structured.get("description_state") == "known" else structured.get("description") or task.description or ""
    task.assigned_date_local = assigned_date or task.assigned_date_local
    incoming_has_deadline = due_at is not None or due_date is not None
    current_rank = task.deadline_source_rank
    if incoming_has_deadline and (current_rank is None or source_rank <= current_rank):
        task.due_at = due_at
        task.due_date_local = due_date
        task.deadline_precision = precision
        task.deadline_timezone = timezone
        task.source_deadline_text = structured.get("source_deadline_text")
        task.deadline_source_rank = source_rank
    elif incoming_has_deadline and not _same_deadline(task, due_at, due_date):
        _record_conflict(db, source_item, task, due_at, due_date)

    task.points_possible = structured.get("points_possible")
    task.grading_category = (
        structured.get("assignment_group_name") or task.grading_category
    )
    task.available_at = (
        isoparse(structured["unlock_at"]) if structured.get("unlock_at") else None
    )
    task.lock_at = (
        isoparse(structured["lock_at"]) if structured.get("lock_at") else None
    )
    submission = structured.get("submission") or {}
    workflow_state = submission.get("workflow_state")
    if submission.get("graded_at") or submission.get("grade") is not None:
        workflow_state = "graded"
    elif submission.get("submitted_at") or workflow_state in {
        "submitted",
        "pending_review",
    }:
        workflow_state = "submitted"
    if "submission" in structured:
        task.submission_state = workflow_state
    # Canvas is authoritative for submitted/graded state. A Canvas "unsubmitted"
    # value does not erase richer local progress such as IN_PROGRESS or BLOCKED.
    if workflow_state in {"submitted", "graded"}:
        task.status = workflow_state.upper()

    deadline = db.scalar(
        select(Deadline).where(
            Deadline.task_id == task.id,
            Deadline.source_item_id == source_item.id,
        )
    )
    if incoming_has_deadline and deadline is None:
        deadline = Deadline(task_id=task.id, source_item_id=source_item.id)
        db.add(deadline)
    if deadline is not None and incoming_has_deadline:
        deadline.due_at = due_at
        deadline.due_date_local = due_date
        deadline.deadline_precision = precision
        deadline.deadline_timezone = timezone
        deadline.source_deadline_text = structured.get("source_deadline_text")
        deadline.source_rank = source_rank
        deadline.authoritative = source_rank <= 3
    elif deadline is not None and structured.get("due_at_state") == "removed":
        # Only this source's explicit removal may clear its evidence. Other
        # sources remain independent and retain their own known deadlines.
        from app.timezone import as_utc
        owned = ((task.due_at is not None and deadline.due_at is not None and
                  as_utc(task.due_at) == as_utc(deadline.due_at)) or
                 (task.due_date_local is not None and task.due_date_local == deadline.due_date_local))
        deadline.due_at = deadline.due_date_local = deadline.deadline_precision = None
        deadline.source_deadline_text = None
        if owned and (current_rank is None or source_rank <= current_rank):
            alternate = db.scalar(select(Deadline).where(
                Deadline.task_id == task.id, Deadline.source_item_id != source_item.id,
                (Deadline.due_at.is_not(None)) | (Deadline.due_date_local.is_not(None))
            ).order_by(Deadline.source_rank).limit(1))
            task.due_at = alternate.due_at if alternate else None
            task.due_date_local = alternate.due_date_local if alternate else None
            task.deadline_precision = alternate.deadline_precision if alternate else None
            task.deadline_source_rank = alternate.source_rank if alternate else None
            task.source_deadline_text = alternate.source_deadline_text if alternate else None

    effective_due = due_at or (planning_cutoff(due_date) if due_date else None)
    factors = {
        "grade_impact": min(1.0, float(task.points_possible or 0) / 100),
        "urgency": urgency_score(effective_due, datetime.now(UTC)),
        "dependency": 0.0,
        "academic_importance": 0.5,
        "failure_risk": 0.35,
    }
    analysis = db.scalar(select(TaskAnalysis).where(TaskAnalysis.task_id == task.id))
    if analysis is None:
        analysis = TaskAnalysis(
            task_id=task.id,
            classification=task.task_type,
            estimated_effort_hours=1.0,
            remaining_effort_hours=1.0,
        )
        db.add(analysis)
    analysis.analysis_json = {"importance": factors, "source": "deterministic_fallback"}
    analysis.priority_score = calculate_priority(factors)
    analysis.rationale = (
        "Priority is based on the authoritative deadline and points; DATE_ONLY tasks use "
        "a private local-day planning cutoff without presenting it as a source time."
    )
    if task.local_completed:
        analysis.remaining_effort_hours = 0
    # SessionLocal intentionally uses autoflush=False. Downstream AI enqueue
    # queries must see this single analysis and link before checking existence.
    db.flush()
    provider_terminal = terminal_state(task_provider_facts(db, task.id))
    if provider_terminal:
        task.submission_state = provider_terminal
        task.status = provider_terminal.upper()
        db.flush()
    return task
