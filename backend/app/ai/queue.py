import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai.service import AIService
from app.config import Settings
from app.db import (
    AnalysisJob,
    ChangeAnalysis,
    ChangeEvent,
    Course,
    SourceItem,
    SourceSnapshot,
    Task,
    TaskAnalysis,
    TaskSourceLink,
)
from app.services.change_review import prefilter
from app.services.notifier import NtfyNotifier

_worker_lock = Lock()


def _hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def task_input_hash(task: Task, source_text: str) -> str:
    return _hash(
        {
            "course_id": task.course_id,
            "task_type": task.task_type,
            "title": task.title,
            "description": task.description,
            "due_at": task.due_at,
            "due_date_local": task.due_date_local,
            "points_possible": task.points_possible,
            "grading_category": task.grading_category,
            "source_text": source_text,
        }
    )


def enqueue_task_analysis(
    db: Session, task: Task, source_text: str, *, force: bool = False
) -> AnalysisJob | None:
    input_hash = task_input_hash(task, source_text)
    analysis = db.scalar(select(TaskAnalysis).where(TaskAnalysis.task_id == task.id))
    if analysis is None:
        analysis = TaskAnalysis(
            task_id=task.id,
            classification=task.task_type,
            analysis_status="PENDING",
            input_hash=input_hash,
        )
        db.add(analysis)
    elif not force and analysis.input_hash == input_hash and analysis.analysis_status in {
        "FAILED",
        "PENDING",
        "READY",
    }:
        return None
    else:
        analysis.analysis_status = "PENDING"
        analysis.input_hash = input_hash
    job = db.scalar(
        select(AnalysisJob).where(
            AnalysisJob.kind == "task_effort",
            AnalysisJob.entity_id == task.id,
            AnalysisJob.input_hash == input_hash,
        )
    )
    if job is None:
        job = AnalysisJob(
            kind="task_effort", entity_id=task.id, input_hash=input_hash, state="PENDING"
        )
        db.add(job)
    elif force:
        job.state = "PENDING"
        job.attempts = 0
        job.last_error = None
    db.flush()
    return job


def change_input_hash(event: ChangeEvent, old: SourceSnapshot | None, new: SourceSnapshot | None) -> str:
    return _hash(
        {
            "source_item_id": event.source_item_id,
            "change_type": event.change_type,
            "old_hash": old.content_hash if old else None,
            "new_hash": new.content_hash if new else None,
            "summary": event.summary,
        }
    )


def enqueue_change_analysis(db: Session, event: ChangeEvent) -> AnalysisJob | None:
    outcome, reason, context = prefilter(db, event)
    old = db.get(SourceSnapshot, event.old_snapshot_id) if event.old_snapshot_id else None
    new = db.get(SourceSnapshot, event.new_snapshot_id) if event.new_snapshot_id else None
    input_hash = change_input_hash(event, old, new)
    analysis = db.scalar(
        select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id)
    )
    if analysis and analysis.input_hash == input_hash:
        return None
    if analysis is None:
        analysis = ChangeAnalysis(
            change_event_id=event.id,
            input_hash=input_hash,
            analysis_status="PENDING",
        )
        db.add(analysis)
    else:
        analysis.input_hash = input_hash
        analysis.analysis_status = "PENDING"
    analysis.analysis_json = {"review_version": "0.7.1", "stage1": outcome,
        "stage1_reason": reason, "context": context,
        "matched_rule": context.get("matched_rule"),
        "review_status": "SUPPRESSED" if outcome == "DROP" else "PENDING_AI", "notify_user": False}
    if outcome == "DROP":
        analysis.analysis_status = "SUPPRESSED"
        db.flush()
        return None
    job = db.scalar(
        select(AnalysisJob).where(
            AnalysisJob.kind == "change_importance",
            AnalysisJob.entity_id == event.id,
            AnalysisJob.input_hash == input_hash,
        )
    )
    if job is None:
        job = AnalysisJob(
            kind="change_importance",
            entity_id=event.id,
            input_hash=input_hash,
            state="PENDING",
        )
        db.add(job)
    db.flush()
    return job


class AnalysisWorker:
    def __init__(self, settings: Settings, max_attempts: int = 2):
        self.settings = settings
        self.max_attempts = max_attempts
        self.service = AIService(settings)
        self.notifier = NtfyNotifier(settings.ntfy_url, settings.ntfy_topic)

    async def run_pending(self, db: Session, limit: int = 10) -> int:
        # Scheduler and HTTP trigger can run in separate threads/event loops.
        if not _worker_lock.acquire(blocking=False):
            return 0
        try:
            return await self._run_pending(db, min(max(limit, 1), 10))
        finally:
            _worker_lock.release()

    async def _run_pending(self, db: Session, limit: int) -> int:
        # A crash must not strand a claimed candidate forever.
        for stale in db.scalars(select(AnalysisJob).where(AnalysisJob.state == "REVIEWING",
                AnalysisJob.updated_at < datetime.now(UTC) - timedelta(minutes=5))):
            stale.state = "FAILED" if stale.attempts >= self.max_attempts else "PENDING"
            if stale.state == "FAILED":
                self._mark_failed(db, stale, "interrupted_review")
        db.commit()
        from app.services.ai_settings import get_ai_probe_settings
        ready = self.service.enabled or get_ai_probe_settings(db)["chat_ready"]
        if ready:
            db.execute(update(AnalysisJob).where(AnalysisJob.kind == "change_importance",
                AnalysisJob.state == "WAITING_AI").values(state="PENDING", last_error=None))
        else:
            # Park unavailable reviews before applying the work limit.
            db.execute(update(AnalysisJob).where(AnalysisJob.kind == "change_importance",
                AnalysisJob.state == "PENDING").values(state="WAITING_AI", last_error="provider_not_configured"))
        db.commit()
        jobs = list(
            db.scalars(
                select(AnalysisJob)
                .where(AnalysisJob.state == "PENDING")
                .order_by(AnalysisJob.created_at, AnalysisJob.id)
                .limit(limit)
            )
        )
        completed = 0
        for job in jobs:
            try:
                if job.kind == "change_importance":
                    if not self.service.enabled and not get_ai_probe_settings(db)["chat_ready"]:
                        job.state = "WAITING_AI"
                        job.last_error = "provider_not_configured"
                        db.commit()
                        continue
                elif not self.service.enabled:
                    raise RuntimeError("provider_disabled")
                job.state = "REVIEWING"
                job.attempts += 1
                db.commit()
                if job.kind == "task_effort":
                    await self._task(db, job)
                elif job.kind == "change_importance":
                    await self._change(db, job)
                else:
                    raise RuntimeError("unsupported_analysis_kind")
                job.state = "READY"
                job.last_error = None
                completed += 1
            except Exception as exc:
                # A failed flush leaves the transaction unusable; the claim and
                # attempt count were committed before invoking the provider.
                db.rollback()
                job.last_error = type(exc).__name__
                terminal = job.attempts >= self.max_attempts or (job.kind != "change_importance" and not self.service.enabled)
                job.state = "FAILED" if terminal else "PENDING"
                if terminal:
                    self._mark_failed(db, job, type(exc).__name__)
                elif job.kind == "change_importance":
                    self._mark_failed(db, job, type(exc).__name__)
            db.commit()
        return completed

    async def _task(self, db: Session, job: AnalysisJob) -> None:
        task = db.get(Task, job.entity_id)
        if task is None:
            raise RuntimeError("task_missing")
        course = db.get(Course, task.course_id)
        link = db.scalar(
            select(TaskSourceLink)
            .where(TaskSourceLink.task_id == task.id)
            .order_by(TaskSourceLink.id)
            .limit(1)
        )
        item = db.get(SourceItem, link.source_item_id) if link else None
        snapshot = (
            db.scalar(
                select(SourceSnapshot)
                .where(SourceSnapshot.source_item_id == item.id)
                .order_by(SourceSnapshot.captured_at.desc())
                .limit(1)
            )
            if item
            else None
        )
        await self.service.analyze_effort(
            db,
            course,
            task,
            snapshot.normalized_text if snapshot else task.description,
            input_hash=job.input_hash,
        )

    async def _change(self, db: Session, job: AnalysisJob) -> None:
        event = db.get(ChangeEvent, job.entity_id)
        if event is None:
            raise RuntimeError("change_missing")
        item = db.get(SourceItem, event.source_item_id)
        course = db.get(Course, item.course_id)
        if course.lifecycle_state != "ACTIVE":
            raise RuntimeError("course_inactive")
        outcome, reason, context = prefilter(db, event)
        analysis = db.scalar(select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id))
        if outcome == "DROP":
            analysis.analysis_status = "SUPPRESSED"
            analysis.analysis_json = {**(analysis.analysis_json or {}), "stage1": outcome,
                "stage1_reason": reason, "matched_rule": context.get("matched_rule"), "review_status": "SUPPRESSED", "notify_user": False}
            return
        analysis.analysis_status = "REVIEWING"
        analysis.analysis_json = {**(analysis.analysis_json or {}), "review_status": "REVIEWING"}
        recent = db.scalars(select(ChangeAnalysis).where(ChangeAnalysis.analysis_status == "READY")
            .order_by(ChangeAnalysis.id.desc()).limit(100)).all()
        context["recent_rejected"] = [{"fields": a.analysis_json.get("context", {}).get("fields"),
            "after": a.analysis_json.get("context", {}).get("after"), "reason": a.reason[:200]}
            for a in recent if a.analysis_json.get("notify_user") is False
            and all(a.analysis_json.get("context", {}).get(k) == context.get(k)
                    for k in ("course_id", "source_type", "source_name", "change_type"))][:3]
        from app.db import ChangeFilterRule
        context["existing_rules"] = [{"id": row.id, "rule": row.rule_json} for row in
            db.scalars(select(ChangeFilterRule).where(ChangeFilterRule.enabled.is_(True)).limit(100))
            if row.rule_json.get("course_id") == context["course_id"] and row.rule_json.get("source_name") == context["source_name"]][:5]
        db.commit()
        await self.service.analyze_change(
            db, course, event, json.dumps(context, ensure_ascii=False), input_hash=job.input_hash
        )
        await self._notify_assignment(db, event.id)

    async def _notify_assignment(self, db: Session, event_id: int) -> None:
        event = db.get(ChangeEvent, event_id)
        if event is None:
            return
        item = db.get(SourceItem, event.source_item_id)
        if item is None:
            return
        await self.notifier.notify_change(db, event, item.title)

    def _mark_failed(self, db: Session, job: AnalysisJob, error: str) -> None:
        if job.kind == "task_effort":
            analysis = db.scalar(
                select(TaskAnalysis).where(TaskAnalysis.task_id == job.entity_id)
            )
        else:
            analysis = db.scalar(
                select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == job.entity_id)
            )
        if analysis is not None:
            analysis.analysis_status = "FAILED"
            if job.kind == "change_importance":
                analysis.analysis_json = {
                    **(analysis.analysis_json or {}),
                    "error": error,
                    "review_version": "0.7.1", "notify_user": False,
                    "review_status": "AI_FAILED_FINAL" if job.state == "FAILED" else "AI_FAILED_RETRYABLE",
                }
                analysis.requires_action = False
                analysis.reason = "AI review unavailable; no notification was approved."
                analysis.recommended_action = None
            else:
                analysis.analysis_json = {"error": error}
