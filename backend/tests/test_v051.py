from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai.queue import AnalysisWorker, enqueue_change_analysis, enqueue_task_analysis
from app.api.routes import (
    changes,
    files,
    read_change,
    restore_course,
    tasks,
    unread_change_count,
)
from app.config import Settings
from app.db import (
    AnalysisJob,
    Base,
    ChangeAnalysis,
    ChangeEvent,
    Course,
    CourseSource,
    DownloadedFile,
    SourceItem,
    SourceSnapshot,
    Task,
    TaskAnalysis,
    TaskProgress,
    TaskSourceLink,
)
from app.schemas import ChangeReviewDecision, EffortAnalysisData, RawSourceItem
from app.services.normalizer import content_hash
from app.services.sync import SyncService
from app.services.today import TodayEngine


@contextmanager
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def make_settings(tmp_path: Path, *, llm_provider: str = "disabled") -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        download_root=tmp_path / "downloads",
        courses_config=tmp_path / "courses.yaml",
        file_rules_config=tmp_path / "file_rules.yaml",
        availability_config=tmp_path / "availability.yaml",
        scheduler_enabled=False,
        llm_provider=llm_provider,
    )


def add_course(db: Session, code: str, lifecycle: str = "ACTIVE") -> tuple[Course, SourceItem]:
    course = Course(
        source="canvas",
        external_id=f"canvas:{code}",
        course_code=code,
        name=code,
        lifecycle_state=lifecycle,
        active=lifecycle == "ACTIVE",
    )
    db.add(course)
    db.flush()
    item = SourceItem(
        course_id=course.id,
        source_type="canvas",
        source_name=f"canvas:{code}",
        external_id="assignment:1",
        item_type="assignment",
        title="Homework",
        current_hash="initial",
    )
    db.add(item)
    db.flush()
    return course, item


def add_file(db: Session, course: Course, item: SourceItem, name: str) -> DownloadedFile:
    value = DownloadedFile(
        course_id=course.id,
        source_item_id=item.id,
        source_url=f"https://example.test/{name}",
        original_filename=name,
        local_path=f"/data/{name}",
        category="reading",
        source_detected_type="reading",
        mime_type="application/pdf",
        size_bytes=10,
        sha256=("a" if course.lifecycle_state == "ACTIVE" else "b") * 64,
        integrity_status="VALID",
    )
    db.add(value)
    db.flush()
    return value


def test_archived_courses_are_isolated_but_history_is_preserved(tmp_path: Path) -> None:
    del tmp_path
    with memory_db() as db:
        active, active_item = add_course(db, "ACTIVE101")
        archived, archived_item = add_course(db, "OLD101", "ARCHIVED")
        active_event = ChangeEvent(source_item_id=active_item.id, change_type="updated", summary="active")
        archived_event = ChangeEvent(source_item_id=archived_item.id, change_type="updated", summary="archived")
        db.add_all([active_event, archived_event])
        active_file = add_file(db, active, active_item, "active.pdf")
        archived_file = add_file(db, archived, archived_item, "archived.pdf")
        db.add_all(
            [
                Task(course_id=active.id, title="Active", task_type="homework", status="NOT_STARTED"),
                Task(course_id=archived.id, title="Archived", task_type="homework", status="NOT_STARTED"),
            ]
        )
        db.commit()

        assert [row["id"] for row in changes(limit=100, course_id=None, include_archived=False, db=db)] == [active_event.id]
        assert unread_change_count(include_archived=False, db=db) == {"count": 1}
        assert unread_change_count(include_archived=True, db=db) == {"count": 2}
        global_files = files(course_id=None, type=None, latest_only=True, limit=200, db=db)
        assert [row["id"] for row in global_files] == [active_file.id]
        combined = files(
            course_id=active.id, type="reading", latest_only=True, limit=200, db=db
        )
        assert [row["id"] for row in combined] == [active_file.id]
        assert files(
            course_id=active.id, type="exam", latest_only=True, limit=200, db=db
        ) == []
        history_files = files(course_id=archived.id, type=None, latest_only=True, limit=200, db=db)
        assert [row["id"] for row in history_files] == [archived_file.id]
        history_changes = changes(limit=100, course_id=archived.id, include_archived=False, db=db)
        assert [row["id"] for row in history_changes] == [archived_event.id]
        assert [row.title for row in tasks(active_only=True, course_id=None, date_from=None, date_to=None, db=db)] == ["Active"]


def test_restore_is_immediate_idempotent_and_queues_reconciliation(tmp_path: Path) -> None:
    with memory_db() as db:
        course, _ = add_course(db, "OLD101", "ARCHIVED")
        source = CourseSource(
            course_id=course.id,
            name="Canvas",
            source_type="canvas",
            enabled=True,
            state="ARCHIVED",
        )
        db.add(source)
        db.commit()
        before = db.scalar(select(func.count()).select_from(Course))

        restored = restore_course(course.id, db=db, settings=make_settings(tmp_path))
        again = restore_course(course.id, db=db, settings=make_settings(tmp_path))

        assert restored.id == course.id == again.id
        assert course.lifecycle_state == "ACTIVE"
        assert course.active is True
        assert course.archived_at is None
        assert source.state == "PENDING_RECONCILIATION"
        assert db.scalar(select(func.count()).select_from(Course)) == before


def test_read_state_survives_refetch_and_unchanged_sync(tmp_path: Path) -> None:
    with memory_db() as db:
        course, _ = add_course(db, "CS101")
        service = SyncService(make_settings(tmp_path))
        first = RawSourceItem(
            source_type="website",
            source_name="course-site",
            external_id="homework",
            item_type="assignment",
            title="Homework",
            structured={"title": "Homework", "due_at": "2026-09-10T22:00:00Z"},
            normalized_text="version one",
        )
        service.store_observation(db, course, first, emit_initial_event=False)
        second = first.model_copy(update={"normalized_text": "version two", "structured": {**first.structured, "details": "new"}})
        event, _ = service.store_observation(db, course, second)
        db.commit()
        assert event is not None
        read_change(event.id, db=db)

        assert changes(limit=100, course_id=None, include_archived=False, db=db)[0]["read_at"] is not None
        duplicate, _ = service.store_observation(db, course, second)
        db.commit()
        assert duplicate is None
        assert db.get(ChangeEvent, event.id).read_at is not None

        third = second.model_copy(update={"normalized_text": "version three", "structured": {**second.structured, "details": "newer"}})
        later, _ = service.store_observation(db, course, third)
        db.commit()
        assert later is not None and later.id != event.id
        assert later.read_at is None
        assert db.get(ChangeEvent, event.id).read_at is not None


def test_canvas_seconds_late_is_not_a_semantic_change_after_upgrade(tmp_path: Path) -> None:
    with memory_db() as db:
        course, _ = add_course(db, "CS101")
        service = SyncService(make_settings(tmp_path))
        first = RawSourceItem(
            source_type="canvas",
            source_name="canvas:1",
            external_id="assignment:overdue",
            item_type="assignment",
            title="Overdue Homework",
            structured={
                "title": "Overdue Homework",
                "submission": {
                    "workflow_state": "unsubmitted",
                    "seconds_late": 120,
                    "attachments": [{"id": 1, "preview_url": "signed-token-one"}],
                },
            },
            normalized_text="Overdue Homework",
        )
        _, item = service.store_observation(db, course, first, emit_initial_event=False)
        db.commit()
        # Simulate the V0.5 hash, which included the volatile nested counter.
        item.current_hash = content_hash(first.structured, first.normalized_text)
        db.commit()
        later = first.model_copy(
            update={
                "structured": {
                    "title": "Overdue Homework",
                    "submission": {
                        "workflow_state": "unsubmitted",
                        "seconds_late": 240,
                        "attachments": [{"id": 1, "preview_url": "signed-token-two"}],
                    },
                }
            }
        )

        event, _ = service.store_observation(db, course, later)
        db.commit()

        assert event is None
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 0
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 1


def test_today_engine_is_deterministic_and_excludes_archived() -> None:
    now = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
    with memory_db() as db:
        active, _ = add_course(db, "PHYS225")
        archived, _ = add_course(db, "OLD225", "ARCHIVED")
        fixtures = [
            ("Overdue", active.id, now - timedelta(hours=1), "homework"),
            ("Due today", active.id, now + timedelta(hours=2), "homework"),
            ("Low slack", active.id, now + timedelta(days=1), "project"),
            ("Distant", active.id, now + timedelta(days=30), "reading"),
            ("Quick", active.id, now + timedelta(days=3), "other"),
            ("Archived overdue", archived.id, now - timedelta(days=2), "homework"),
        ]
        task_rows = []
        for title, course_id, due, kind in fixtures:
            task = Task(course_id=course_id, title=title, task_type=kind, due_at=due, status="NOT_STARTED")
            db.add(task)
            db.flush()
            task_rows.append(task)
        db.add(
            TaskAnalysis(
                task_id=task_rows[0].id,
                classification="homework",
                analysis_json={"source": "deterministic_fallback"},
                analysis_status="READY",
                ai_estimated_minutes=60,
                effective_estimated_minutes=60,
            )
        )
        db.add(TaskProgress(task_id=task_rows[4].id, status="IN_PROGRESS", progress_percent=80))
        db.commit()

        first = TodayEngine(180).build(db, now)
        second = TodayEngine(180).build(db, now)
        titles = [row["title"] for row in first["work"]]
        assert titles == [row["title"] for row in second["work"]]
        assert {"Overdue", "Due today", "Low slack", "Quick"}.issubset(titles)
        assert "Distant" not in titles
        assert "Archived overdue" not in titles
        reasons = {row["title"]: row["today_reason"] for row in first["work"]}
        assert reasons["Overdue"] == "Overdue"
        assert reasons["Due today"] == "Due today"
        assert reasons["Quick"].startswith("Quick finish")
        overdue = next(row for row in first["work"] if row["title"] == "Overdue")
        assert overdue["estimate_source"] == "fallback"


class ValidProvider:
    def __init__(self):
        self.calls = 0

    async def structured_generate(self, *, system_prompt, user_prompt, response_model):
        del system_prompt, user_prompt
        self.calls += 1
        if response_model is EffortAnalysisData:
            return EffortAnalysisData(
                total_minutes=180,
                low_minutes=120,
                high_minutes=240,
                confidence=0.8,
                complexity="medium",
                work_breakdown=[{"stage": "solve", "minutes": 180}],
                assumptions=["Lecture covered"],
            )
        return ChangeReviewDecision(notify_user=True, importance="critical", reason="Preparation time was reduced.")


@pytest.mark.asyncio
async def test_structured_effort_queue_persists_and_progress_does_not_recall(tmp_path: Path) -> None:
    with memory_db() as db:
        course, item = add_course(db, "CS101")
        snapshot = SourceSnapshot(source_item_id=item.id, content_hash="a" * 64, structured_json={}, normalized_text="Solve ten problems")
        now = datetime.now(UTC)
        task = Task(
            course_id=course.id,
            title="Homework",
            task_type="homework",
            status="NOT_STARTED",
            due_at=now + timedelta(days=1),
        )
        db.add_all([snapshot, task])
        db.flush()
        db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id))
        enqueue_task_analysis(db, task, snapshot.normalized_text)
        db.commit()
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        provider = ValidProvider()
        worker.service.provider = provider

        assert await worker.run_pending(db) == 1
        analysis = db.scalar(select(TaskAnalysis).where(TaskAnalysis.task_id == task.id))
        assert analysis.analysis_status == "READY"
        assert analysis.ai_estimated_minutes == 180
        assert analysis.effective_estimated_minutes == 180
        calls = provider.calls
        db.add(TaskProgress(task_id=task.id, status="IN_PROGRESS", progress_percent=50))
        db.commit()
        result = TodayEngine().build(db, now)
        assert provider.calls == calls
        assert analysis.remaining_effort_hours == 3
        assert result["work"][0]["estimate_source"] == "ai"
        assert result["work"][0]["remaining_minutes"] == 90


class InvalidProvider:
    async def structured_generate(self, *, system_prompt, user_prompt, response_model):
        del system_prompt, user_prompt
        return response_model.model_validate({"total_minutes": -1})


class LowChangeProvider:
    async def structured_generate(self, *, system_prompt, user_prompt, response_model):
        del system_prompt, user_prompt
        return response_model(
            importance="low",
            notify_user=False,
            reason="The academic meaning is unchanged.",
        )


@pytest.mark.asyncio
async def test_invalid_ai_output_retries_then_falls_back(tmp_path: Path) -> None:
    with memory_db() as db:
        course, item = add_course(db, "CS101")
        task = Task(course_id=course.id, title="Homework", task_type="homework", status="NOT_STARTED")
        db.add(task)
        db.flush()
        db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id))
        enqueue_task_analysis(db, task, "work")
        db.commit()
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"), max_attempts=2)
        worker.service.provider = InvalidProvider()

        assert await worker.run_pending(db) == 0
        job = db.scalar(select(AnalysisJob))
        assert job.state == "PENDING" and job.attempts == 1
        assert await worker.run_pending(db) == 0
        assert job.state == "FAILED" and job.attempts == 2
        analysis = db.scalar(select(TaskAnalysis).where(TaskAnalysis.task_id == task.id))
        assert analysis.analysis_status == "FAILED"
        assert TodayEngine()._estimate(task, analysis)[1] == "fallback"


@pytest.mark.asyncio
async def test_change_analysis_deduplicates_and_enters_attention(tmp_path: Path) -> None:
    with memory_db() as db:
        course, item = add_course(db, "CS101")
        old = SourceSnapshot(source_item_id=item.id, content_hash="a" * 64, structured_json={"due_at": "2026-09-10"}, normalized_text="Sep 10")
        new = SourceSnapshot(source_item_id=item.id, content_hash="b" * 64, structured_json={"due_at": "2026-09-08"}, normalized_text="Sep 8")
        db.add_all([old, new])
        db.flush()
        event = ChangeEvent(source_item_id=item.id, change_type="deadline_changed", old_snapshot_id=old.id, new_snapshot_id=new.id, importance="critical", requires_action=True, summary="Deadline changed")
        db.add(event)
        db.flush()
        assert enqueue_change_analysis(db, event) is not None
        assert enqueue_change_analysis(db, event) is None
        db.commit()
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        worker.service.provider = ValidProvider()

        assert await worker.run_pending(db) == 1
        analysis = db.scalar(select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id))
        assert analysis.analysis_status == "READY"
        assert analysis.requires_action is True
        attention = TodayEngine().build(db)["attention"]
        assert [row["change_id"] for row in attention] == [event.id]

        course.lifecycle_state = "ARCHIVED"
        db.commit()
        assert TodayEngine().build(db)["attention"] == []


@pytest.mark.asyncio
async def test_minor_change_analysis_stays_out_of_attention(tmp_path: Path) -> None:
    with memory_db() as db:
        _course, item = add_course(db, "CS101")
        old = SourceSnapshot(
            source_item_id=item.id,
            content_hash="c" * 64,
            structured_json={"title": "Homwork"},
            normalized_text="Homwork",
        )
        new = SourceSnapshot(
            source_item_id=item.id,
            content_hash="d" * 64,
            structured_json={"title": "Homework"},
            normalized_text="Homework",
        )
        db.add_all([old, new])
        db.flush()
        event = ChangeEvent(
            source_item_id=item.id,
            change_type="updated",
            old_snapshot_id=old.id,
            new_snapshot_id=new.id,
            importance="minor",
            requires_action=False,
            summary="Changed fields: title",
        )
        db.add(event)
        db.flush()
        enqueue_change_analysis(db, event)
        db.commit()
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        worker.service.provider = LowChangeProvider()

        assert await worker.run_pending(db) == 1
        analysis = db.scalar(
            select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id)
        )
        assert analysis.requires_action is False
        assert TodayEngine().build(db)["attention"] == []
