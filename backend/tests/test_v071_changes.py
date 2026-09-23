import asyncio

import httpx
import pytest
from sqlalchemy import func, select
from test_v051 import add_course, make_settings, memory_db

from app.ai.queue import AnalysisWorker, enqueue_change_analysis
from app.api.routes import notifications
from app.db import AnalysisJob, ChangeAnalysis, ChangeEvent, Notification, SourceSnapshot
from app.services.change_review import prefilter
from app.services.notifier import NtfyNotifier
from app.services.today import TodayEngine


def candidate(db, *, field="due_at", old="2026-10-02", new="2026-10-01", kind="DEADLINE_CHANGED"):
    _, item = add_course(db, "TEST")
    snapshots = [SourceSnapshot(source_item_id=item.id, content_hash=str(i),
        structured_json={field: value}, normalized_text="same academic text") for i, value in enumerate((old, new))]
    db.add_all(snapshots)
    db.flush()
    event = ChangeEvent(source_item_id=item.id, change_type=kind, old_snapshot_id=snapshots[0].id,
        new_snapshot_id=snapshots[1].id, importance="critical", requires_action=True, summary="Changed")
    db.add(event)
    db.flush()
    return event


class DecisionProvider:
    def __init__(self, notify=True, invalid=False, importance="high"):
        self.notify, self.invalid, self.importance, self.calls = notify, invalid, importance, 0

    async def structured_generate(self, **kwargs):
        self.calls += 1
        return kwargs["response_model"](notify_user="yes" if self.invalid else self.notify,
            importance=self.importance, reason="Academic change" if self.notify else "Repeated formatting noise")


@pytest.mark.asyncio
@pytest.mark.parametrize("notify,importance", [
    (True, "low"), (True, "medium"), (True, "high"), (True, "critical"), (False, "high")
])
async def test_only_ai_approval_creates_home_notification(tmp_path, notify, importance):
    with memory_db() as db:
        event = candidate(db)
        assert enqueue_change_analysis(db, event)
        assert enqueue_change_analysis(db, event) is None
        assert db.scalar(select(func.count()).select_from(AnalysisJob)) == 1
        assert TodayEngine()._attention(db) == []
        assert await NtfyNotifier("", "").notify_change(db, event, "test") is None
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        provider = DecisionProvider(notify, importance=importance)
        worker.service.provider = provider
        assert await worker.run_pending(db) == 1
        assert bool(TodayEngine()._attention(db)) == notify
        assert bool(db.scalar(select(Notification))) == notify
        assert bool(notifications(db)) == notify
        if notify:
            assert db.scalar(select(Notification)).level == ("critical" if importance == "critical" else "important")
        assert await worker.run_pending(db) == 0
        assert provider.calls == 1


def test_historical_unreviewed_notification_is_preserved_but_not_a_home_banner():
    with memory_db() as db:
        event = candidate(db)
        row = Notification(change_event_id=event.id, level="critical", title="Legacy",
            body="Old deterministic push", dedupe_key="legacy")
        db.add(row)
        db.commit()
        assert notifications(db) == []
        assert db.get(Notification, row.id).body == "Old deterministic push"


@pytest.mark.asyncio
async def test_legacy_critical_backlog_cannot_hide_approved_banner(tmp_path):
    with memory_db() as db:
        event = candidate(db)
        enqueue_change_analysis(db, event)
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        worker.service.provider = DecisionProvider(True, importance="low")
        assert await worker.run_pending(db) == 1
        approved = db.scalar(select(Notification))
        assert approved.level == "important"

        legacy = candidate(db)
        db.add_all(Notification(change_event_id=legacy.id, level="critical", title="Legacy",
            body="Unreviewed", dedupe_key=f"legacy-{index}") for index in range(105))
        db.commit()

        assert [row["id"] for row in notifications(db)] == [approved.id]


@pytest.mark.asyncio
async def test_ntfy_outage_does_not_undo_ai_approval(tmp_path, monkeypatch):
    attempts = []

    async def fail(*args, **kwargs):
        attempts.append(1)
        raise httpx.ConnectError("private transport details")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    with memory_db() as db:
        enqueue_change_analysis(db, candidate(db))
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock").model_copy(
            update={"ntfy_url": "https://ntfy.example", "ntfy_topic": "test"}))
        provider = DecisionProvider(True)
        worker.service.provider = provider
        assert await worker.run_pending(db) == 1
        assert await worker.run_pending(db) == 0
        analysis = db.scalar(select(ChangeAnalysis))
        assert analysis.analysis_status == "READY"
        assert analysis.analysis_json["review_status"] == "NOTIFY"
        assert db.scalar(select(Notification)).status == "failed"
        assert db.scalar(select(AnalysisJob)).attempts == 1
        assert provider.calls == 1 and len(attempts) == 2
        assert len(TodayEngine()._attention(db)) == 1
        assert len(notifications(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_importance,ai_importance,expected_priority", [
    ("minor", "critical", "urgent"), ("critical", "low", "default")
])
async def test_ntfy_priority_follows_ai_decision(tmp_path, monkeypatch,
                                                 event_importance, ai_importance, expected_priority):
    sent_priorities = []

    async def fake_post(self, url, **kwargs):
        sent_priorities.append(kwargs["headers"]["Priority"])
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with memory_db() as db:
        event = candidate(db)
        event.importance = event_importance
        enqueue_change_analysis(db, event)
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock").model_copy(
            update={"ntfy_url": "https://ntfy.example", "ntfy_topic": "test"}))
        worker.service.provider = DecisionProvider(True, importance=ai_importance)
        assert await worker.run_pending(db) == 1
        assert sent_priorities == [expected_priority]
        assert db.scalar(select(Notification)).status == "sent"


@pytest.mark.asyncio
async def test_unconfigured_queue_cannot_starve_other_jobs_and_resumes(tmp_path):
    with memory_db() as db:
        for _ in range(10):
            enqueue_change_analysis(db, candidate(db))
        effort = AnalysisJob(kind="task_effort", entity_id=123456, input_hash="test", state="PENDING")
        db.add(effort)
        db.commit()
        worker = AnalysisWorker(make_settings(tmp_path))
        await worker.run_pending(db)
        assert effort.state == "FAILED"
        parked = list(db.scalars(select(AnalysisJob).where(AnalysisJob.kind == "change_importance")))
        assert all(job.state == "WAITING_AI" and job.attempts == 0 for job in parked)
        configured = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        configured.service.provider = DecisionProvider(False)
        assert await configured.run_pending(db) == 10
        assert all(job.state == "READY" and job.attempts == 1 for job in parked)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_provider_http_errors_remain_bounded_and_never_push(tmp_path, status):
    class BrokenProvider:
        calls = 0

        async def structured_generate(self, **kwargs):
            self.calls += 1
            request = httpx.Request("POST", "https://ai.example")
            raise httpx.HTTPStatusError("private body", request=request,
                response=httpx.Response(status, request=request))

    with memory_db() as db:
        enqueue_change_analysis(db, candidate(db))
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        provider = BrokenProvider()
        worker.service.provider = provider
        for _ in range(3):
            await worker.run_pending(db)
        assert provider.calls == 2
        assert db.scalar(select(ChangeAnalysis)).analysis_json["review_status"] == "AI_FAILED_FINAL"
        assert TodayEngine()._attention(db) == []


@pytest.mark.asyncio
async def test_invalid_ai_output_retries_bounded_never_uses_deadline_fallback(tmp_path):
    with memory_db() as db:
        event = candidate(db)
        enqueue_change_analysis(db, event)
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        provider = DecisionProvider(invalid=True)
        worker.service.provider = provider
        for _ in range(4):
            await worker.run_pending(db)
        assert provider.calls == 2
        analysis = db.scalar(select(ChangeAnalysis))
        assert analysis.analysis_json["review_status"] == "AI_FAILED_FINAL"
        assert db.scalar(select(Notification)) is None
        assert TodayEngine()._attention(db) == []


@pytest.mark.asyncio
async def test_unconfigured_ai_stays_pending_without_call_or_notification(tmp_path):
    with memory_db() as db:
        enqueue_change_analysis(db, candidate(db))
        worker = AnalysisWorker(make_settings(tmp_path))
        await worker.run_pending(db)
        job = db.scalar(select(AnalysisJob))
        assert job.state == "WAITING_AI" and job.attempts == 0
        assert job.last_error == "provider_not_configured"
        assert db.scalar(select(Notification)) is None


def test_metadata_only_diff_is_dropped_and_audited():
    with memory_db() as db:
        event = candidate(db, field="preview_url", kind="CONTENT_UPDATED")
        assert prefilter(db, event)[0] == "DROP"
        assert enqueue_change_analysis(db, event) is None
        assert db.scalar(select(ChangeAnalysis)).analysis_json["stage1_reason"] == "empty_or_metadata_only_diff"
        assert db.scalar(select(AnalysisJob)) is None


@pytest.mark.asyncio
async def test_provider_timeout_is_bounded_and_retryable(tmp_path, monkeypatch):
    original = asyncio.wait_for
    async def short(awaitable, timeout):
        return await original(awaitable, timeout=.001)
    class Slow:
        async def structured_generate(self, **kwargs):
            await asyncio.sleep(1)
    monkeypatch.setattr("app.ai.service.asyncio.wait_for", short)
    with memory_db() as db:
        enqueue_change_analysis(db, candidate(db))
        worker = AnalysisWorker(make_settings(tmp_path, llm_provider="mock"))
        worker.service.provider = Slow()
        await worker.run_pending(db)
        assert db.scalar(select(AnalysisJob)).last_error == "TimeoutError"
        assert db.scalar(select(ChangeAnalysis)).analysis_json["review_status"] == "AI_FAILED_RETRYABLE"
        assert db.scalar(select(Notification)) is None
