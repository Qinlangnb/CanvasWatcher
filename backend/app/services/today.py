import math
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    ChangeAnalysis,
    ChangeEvent,
    Course,
    SourceItem,
    Task,
    TaskAnalysis,
    TaskProgress,
    TaskSourceLink,
)
from app.services.assignment_summaries import SummaryReader
from app.services.calendar_capacity import CapacityReader
from app.services.change_policy import (
    analysis_should_notify_user,
    is_user_facing_event,
    reportable_change_filter,
)
from app.services.course_terms import normalize_course_display
from app.services.task_attachments import task_attachments
from app.services.task_links import task_source_links
from app.services.work_tracking import task_work_summaries
from app.timezone import UIUC_TIMEZONE, as_uiuc, as_utc, planning_cutoff, uiuc_day_bounds

DEFAULT_DAILY_CAPACITY_MINUTES = 180
FALLBACK_MINUTES = {
    "homework": 120,
    "assignment": 120,
    "reading": 45,
    "project": 240,
    "exam": 180,
    "exam_preparation": 180,
    "discussion": 30,
    "discussion_preparation": 30,
    "other": 60,
}
TODAY_WEIGHTS = {
    "urgency": 0.30,
    "importance": 0.22,
    "schedule_pressure": 0.24,
    "dependency": 0.08,
    "change_impact": 0.10,
    "quick_finish": 0.06,
}
COMPLETE_STATUSES = {"SUBMITTED", "GRADED", "CANCELLED"}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _bucket(score: float) -> str:
    if score >= 0.90:
        return "Critical"
    if score >= 0.72:
        return "High"
    if score >= 0.45:
        return "Medium"
    return "Low"


def _severity_score(value: str) -> float:
    return {"minor": 0.2, "low": 0.3, "important": 0.8, "high": 0.85, "critical": 1.0}.get(
        value.lower(), 0.5
    )


def round_remaining_minutes(value: float, *, complete: bool = False) -> int:
    if complete:
        return 0
    if value <= 0:
        return 0
    return max(5, int(5 * round(value / 5)))


class TodayEngine:
    def __init__(self, daily_capacity_minutes: int = DEFAULT_DAILY_CAPACITY_MINUTES):
        self.daily_capacity_minutes = max(30, daily_capacity_minutes)

    def build(self, db: Session, now: datetime | None = None, *, include_details: bool = True) -> dict:
        now_utc = as_utc(now or datetime.now(UTC))
        today = as_uiuc(now_utc).date()
        rows = db.execute(
            select(Task, Course, TaskAnalysis)
            .join(Course, Course.id == Task.course_id)
            .join(TaskAnalysis, TaskAnalysis.task_id == Task.id, isouter=True)
            .where(
                Course.lifecycle_state == "ACTIVE",
                Task.ignored_at.is_(None),
            )
        ).all()
        work = []
        completed = []
        tasks = [task for task, _course, _analysis in rows]
        task_ids = [task.id for task in tasks]
        progress_by_task = self._latest_progress(db, task_ids)
        usage_by_task = task_work_summaries(db, tasks, now_utc)
        change_impacts = self._task_change_impacts(db, task_ids) if include_details else {}
        _day_start, day_end = uiuc_day_bounds(today)
        deadlines = [as_utc(task.due_at or planning_cutoff(task.due_date_local))
                     for task in tasks if task.due_at or task.due_date_local]
        capacity = CapacityReader(db, now_utc, max([day_end, *deadlines]))
        for task, course, analysis in rows:
            complete = task.local_completed or task.status in COMPLETE_STATUSES
            latest_progress = progress_by_task.get(task.id)
            progress = float(latest_progress.progress_percent or 0) if latest_progress else 0.0
            total, source, confidence, status = self._estimate(task, analysis)
            usage = usage_by_task[task.id]
            used = usage["effective_used_minutes"]
            if progress >= 100:
                remaining = 0
                source = "observed_ratio"
            elif used > 0 and progress > 0:
                fraction = progress / 100
                total = round_remaining_minutes(used / fraction)
                remaining = round_remaining_minutes(used * (1 - fraction) / fraction)
                source = "observed_ratio"
                confidence = 1.0
            elif latest_progress and latest_progress.remaining_minutes_estimate is not None:
                remaining = round_remaining_minutes(latest_progress.remaining_minutes_estimate)
            else:
                remaining = round_remaining_minutes(total * (1 - progress / 100))
            remaining = max(0, remaining)
            deadline = task.due_at or (
                planning_cutoff(task.due_date_local) if task.due_date_local else None
            )
            deadline_utc = as_utc(deadline) if deadline else None
            deadline_day = as_uiuc(deadline_utc).date() if deadline_utc else None
            minutes_until = (
                (deadline_utc - now_utc).total_seconds() / 60 if deadline_utc else None
            )
            calendar_available, _capacity_source = (
                capacity.between(now_utc, deadline_utc)
                if deadline_utc
                else (None, "fallback")
            )
            available = calendar_available
            if available is None and minutes_until is not None:
                available = max(0.0, minutes_until / 1440) * self.daily_capacity_minutes
            slack = round(available - remaining) if available is not None else None
            overdue = bool(not complete and minutes_until is not None and minutes_until < 0)
            due_today = deadline_day == today and not overdue
            completed_today = bool(task.local_completed_at and as_uiuc(task.local_completed_at).date() == today)
            if complete and not (due_today or completed_today):
                continue
            if complete:
                remaining = 0
                progress = 100
            near_term = bool(minutes_until is not None and minutes_until <= 7 * 1440)
            quick_finish = remaining <= 30 and near_term and (progress >= 70 or remaining > 0)
            schedule_pressure = (
                0.35
                if slack is None
                else 1.0
                if slack <= 0
                else _clamp(1 - slack / (2 * self.daily_capacity_minutes))
            )
            urgency = (
                1.0
                if overdue
                else 0.96
                if due_today
                else _clamp(math.exp(-(minutes_until or 10080) / (3 * 1440)))
            )
            importance = _clamp((task.points_possible or 40) / 100)
            factors = (analysis.analysis_json or {}).get("importance", {}) if analysis else {}
            importance = max(importance, float(factors.get("academic_importance", 0)))
            dependency = float(factors.get("dependency", 0))
            change_impact = change_impacts.get(task.id, 0)
            score = round(
                sum(
                    TODAY_WEIGHTS[key] * value
                    for key, value in {
                        "urgency": urgency,
                        "importance": importance,
                        "schedule_pressure": schedule_pressure,
                        "dependency": dependency,
                        "change_impact": change_impact,
                        "quick_finish": 1.0 if quick_finish else 0.0,
                    }.items()
                ),
                4,
            )
            start_today = bool(deadline and slack is not None and slack <= self.daily_capacity_minutes)
            if not (complete or overdue or due_today or start_today or quick_finish):
                continue
            reason = (
                "Overdue"
                if overdue
                else "Due today"
                if due_today
                else f"Quick finish · ~{remaining}m remaining"
                if quick_finish
                else "Start today · low schedule slack"
            )
            (completed if complete else work).append(
                {
                    "task_id": task.id,
                    "local_completed": bool(task.local_completed),
                    "submission_state": task.submission_state,
                    "local_completed_at": task.local_completed_at,
                    "state_revision": task.state_revision or 0,
                    "course_id": course.id,
                    "course_code": normalize_course_display(course.course_code, course.name, course.term_name or course.term).course_code,
                    "title": task.title,
                    "deadline": deadline_utc,
                    "deadline_precision": task.deadline_precision,
                    "due_date_local": task.due_date_local,
                    "progress": progress,
                    "effective_total_minutes": total,
                    "remaining_minutes": remaining,
                    "estimate_source": source,
                    "estimate_confidence": confidence,
                    "analysis_status": status,
                    "priority_score": score,
                    "priority_bucket": _bucket(score),
                    "today_reason": ("Completed · Due today" if due_today else "Completed today") if complete else reason,
                    "slack_minutes": slack,
                    "slack_bucket": self._slack_bucket(slack),
                    **{key: usage[key] for key in (
                        "tracked_minutes",
                        "manual_adjustment_minutes",
                        "effective_used_minutes",
                        "active_started_at",
                        "active_elapsed_seconds",
                    )},
                    "is_working": usage["active_session_id"] is not None,
                }
            )
        work.sort(
            key=lambda row: (
                -row["priority_score"],
                row["deadline"] or datetime.max.replace(tzinfo=UTC),
                row["task_id"],
            )
        )
        if include_details:
            visible = [task["task_id"] for task in work + completed]
            source_links = task_source_links(db, visible)
            attachments = task_attachments(db, visible)
            facts = SummaryReader(db, visible).facts
            for task in work + completed:
                task_id = task["task_id"]
                task.update(source_links[task_id], attachments=attachments[task_id],
                            provider_facts=facts[task_id])
        current_capacity, capacity_source = capacity.between(now_utc, day_end)
        return {
            "date": today,
            "timezone": UIUC_TIMEZONE,
            "work": work,
            "completed": completed,
            "attention": self._attention(db) if include_details else [],
            "capacity_minutes": current_capacity if current_capacity is not None else self.daily_capacity_minutes,
            "capacity_source": capacity_source,
        }

    def _estimate(self, task: Task, analysis: TaskAnalysis | None) -> tuple[int, str, float, str]:
        legacy_fallback = bool(
            analysis
            and (analysis.analysis_json or {}).get("source") == "deterministic_fallback"
        )
        if (
            analysis
            and not legacy_fallback
            and analysis.analysis_status == "READY"
            and analysis.ai_estimated_minutes
        ):
            factor = analysis.calibration_factor or 1.0
            effective = analysis.effective_estimated_minutes or round(
                analysis.ai_estimated_minutes * factor
            )
            calibrated = factor != 1.0 or effective != analysis.ai_estimated_minutes
            return max(1, effective), "ai_calibrated" if calibrated else "ai", analysis.confidence, "READY"
        fallback = FALLBACK_MINUTES.get(task.task_type.lower(), FALLBACK_MINUTES["other"])
        status = "FAILED" if legacy_fallback else analysis.analysis_status if analysis else "PENDING"
        return fallback, "fallback", 0.0, status if status in {"PENDING", "FAILED"} else "PENDING"

    @staticmethod
    def _latest_progress(db: Session, task_ids: list[int]) -> dict[int, TaskProgress]:
        result = {}
        for offset in range(0, len(task_ids), 500):
            ranked = select(TaskProgress.id, func.row_number().over(
                partition_by=TaskProgress.task_id,
                order_by=(TaskProgress.recorded_at.desc(), TaskProgress.id.desc()),
            ).label("position")).where(TaskProgress.task_id.in_(task_ids[offset:offset + 500])).subquery()
            for row in db.scalars(select(TaskProgress).join(ranked, ranked.c.id == TaskProgress.id)
                                  .where(ranked.c.position == 1)):
                result[row.task_id] = row
        return result

    @staticmethod
    def _task_change_impacts(db: Session, task_ids: list[int]) -> dict[int, float]:
        result = {}
        for offset in range(0, len(task_ids), 500):
            ranked = select(TaskSourceLink.task_id, ChangeAnalysis.importance_score,
                func.row_number().over(partition_by=TaskSourceLink.task_id,
                    order_by=ChangeEvent.detected_at.desc()).label("position"))\
                .join(ChangeEvent, ChangeEvent.id == ChangeAnalysis.change_event_id)\
                .join(TaskSourceLink, TaskSourceLink.source_item_id == ChangeEvent.source_item_id)\
                .where(TaskSourceLink.task_id.in_(task_ids[offset:offset + 500]),
                       ChangeAnalysis.requires_action.is_(True)).subquery()
            for task_id, value in db.execute(select(ranked.c.task_id, ranked.c.importance_score)
                                             .where(ranked.c.position == 1)):
                result[task_id] = float(value or 0)
        return result

    @staticmethod
    def _slack_bucket(slack: int | None) -> str:
        if slack is None:
            return "unknown"
        if slack <= 0:
            return "negative"
        if slack <= DEFAULT_DAILY_CAPACITY_MINUTES:
            return "low"
        return "comfortable"

    @staticmethod
    def _attention(db: Session) -> list[dict]:
        rows = db.execute(
            select(ChangeEvent, SourceItem, Course, ChangeAnalysis)
            .join(SourceItem, SourceItem.id == ChangeEvent.source_item_id)
            .join(Course, Course.id == SourceItem.course_id)
            .join(ChangeAnalysis, ChangeAnalysis.change_event_id == ChangeEvent.id, isouter=True)
            .where(
                Course.lifecycle_state == "ACTIVE",
                ChangeEvent.read_at.is_(None),
                reportable_change_filter(),
            )
            .order_by(ChangeEvent.detected_at.desc())
            .limit(100)
        ).all()
        output = []
        for event, _item, course, analysis in rows:
            if not is_user_facing_event(db, event):
                continue
            if not analysis_should_notify_user(
                event, analysis
            ):
                continue
            score = analysis.importance_score if analysis else _severity_score(event.importance)
            output.append(
                {
                    "change_id": event.id,
                    "course_id": course.id,
                    "course_code": normalize_course_display(course.course_code, course.name, course.term_name or course.term).course_code,
                    "summary": analysis.summary if analysis and analysis.summary else event.ai_summary or event.summary,
                    "severity": analysis.severity if analysis else event.importance,
                    "importance": score,
                    "requires_action": True,
                    "recommended_action": analysis.recommended_action if analysis else "Review this change.",
                    "affected_task_ids": analysis.affected_task_ids if analysis else [],
                    "detected_at": event.detected_at,
                    "analysis_status": analysis.analysis_status if analysis else "PENDING",
                    "ai_reviewed": True,
                    "review_reason": analysis.reason,
                    "reviewed_at": (analysis.analysis_json or {}).get("reviewed_at"),
                }
            )
        return output
