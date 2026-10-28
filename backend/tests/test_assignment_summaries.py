from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.ai.queue import AnalysisWorker
from app.config import Settings
from app.db import (
    AIRun,
    AnalysisJob,
    Base,
    Course,
    SourceItem,
    SourceSnapshot,
    Task,
    TaskSourceLink,
)
from app.services import assignment_summaries as summaries
from app.services.ai_settings import ai_probe_secrets, set_ai_credential, set_ai_probe_settings
from app.services.task_providers import task_provider_facts, terminal_state
from app.services.today import TodayEngine


@pytest.fixture
def db():
    ai_probe_secrets.configure_persistence(None)
    ai_probe_secrets.clear()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    ai_probe_secrets.clear()


def configure(db, *, verified=True, provider="deepseek", model="fixture"):
    value = set_ai_probe_settings(db, "", model, provider)
    if not provider.startswith("ollama"):
        set_ai_credential(db, "fixture-key", provider)
    if verified:
        from app.services.ai_settings import _identity, _value
        ai_probe_secrets.mark_verified(_identity(_value(db)))
    return value


def seed(db, description="Solve questions 1–4; upload a single PDF."):
    course = Course(source="canvas", external_id="7", course_code="TEST", name="Test")
    db.add(course)
    db.flush()
    task = Task(course_id=course.id, title="Not the description", due_at=datetime.now(UTC) + timedelta(hours=1))
    item = SourceItem(course_id=course.id, source_type="canvas", source_name="canvas:7",
        external_id="assignment:5", item_type="assignment", title="HW", current_hash="x",
        url="https://canvas.example/courses/7/assignments/5")
    db.add_all([task, item])
    db.flush()
    db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id))
    db.add(SourceSnapshot(source_item_id=item.id, content_hash="x", structured_json={
        "description": description, "due_at": "2026-10-09T05:00:00Z",
        "submission": {"workflow_state": "submitted"}}))
    db.commit()
    return task, item


class Provider:
    def __init__(self, reply=None, mutate=None):
        self.calls = []
        self.reply = reply or {"content": '{"summary":"Solve questions 1–4 and submit one PDF."}'}
        self.mutate = mutate

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.mutate:
            self.mutate()
        return self.reply


def test_no_ai_hides_all_source_facts_without_affecting_business_state(db):
    task, _ = seed(db)
    assert summaries.source_facts_for_today(db, task.id) == []
    assert TodayEngine().build(db)["work"][0]["provider_facts"] == []
    assert terminal_state(task_provider_facts(db, task.id)) == "submitted"
    summaries.enqueue_visible_summaries(db)
    assert db.scalar(select(func.count(AnalysisJob.id))) == 0


@pytest.mark.asyncio
async def test_cached_exact_description_generation_and_no_poll_requests(db, monkeypatch):
    task, item = seed(db)
    configure(db)
    provider = Provider()
    monkeypatch.setattr(summaries, "chat_provider", lambda db: provider)
    assert summaries.source_facts_for_today(db, task.id)[0]["description_summary_status"] == "pending"
    assert await summaries.run_summaries(db) == 1
    for _ in range(3):
        facts = summaries.source_facts_for_today(db, task.id)
        assert facts[0]["source_item_id"] == item.id
        assert facts[0]["description_summary"] == "Solve questions 1–4 and submit one PDF."
        assert facts[0]["due_at"] == "2026-10-09T05:00:00Z"
        assert "direct_assignment_url" not in facts[0]
        assert "is_deleted" not in facts[0] and "source_url" not in facts[0]
        assert await summaries.run_summaries(db) == 0
    assert len(provider.calls) == 1
    prompt = provider.calls[0]
    assert "Solve questions" in prompt["messages"][1]["content"]
    assert "Not the description" not in prompt["messages"][1]["content"]
    assert prompt["tools"] is None and not prompt["native_tools"]
    assert "untrusted" in prompt["messages"][0]["content"]
    ai_probe_secrets.clear()
    assert summaries.source_facts_for_today(db, task.id) == []
    assert db.scalar(select(func.count(AIRun.id))) == 1


@pytest.mark.parametrize("change", ["description", "model", "provider", "deleted", "missing", "cross_course"])
@pytest.mark.asyncio
async def test_obsolete_cache_and_scope(db, monkeypatch, change):
    task, item = seed(db)
    configure(db)
    monkeypatch.setattr(summaries, "chat_provider", lambda db: Provider())
    await summaries.run_summaries(db)
    if change in {"description", "missing"}:
        db.add(SourceSnapshot(source_item_id=item.id, content_hash="new", structured_json={
            "description": "New requirements" if change == "description" else ""}))
    elif change == "deleted":
        item.is_deleted = True
    elif change == "cross_course":
        item.course_id += 1
    else:
        configure(db, provider="kimi" if change == "provider" else "deepseek", model="new")
    db.commit()
    facts = summaries.source_facts_for_today(db, task.id)
    if change in {"deleted", "cross_course"}:
        assert facts == []
    else:
        assert facts[0]["description_summary"] is None
        assert facts[0]["description_summary_status"] == ("unavailable" if change == "missing" else "pending")


@pytest.mark.asyncio
async def test_configured_unverified_waits_and_local_ollama_needs_no_key(db):
    task, _ = seed(db)
    configure(db, verified=False)
    assert summaries.source_facts_for_today(db, task.id)[0]["description_summary_status"] == "needs_connection"
    assert await summaries.run_summaries(db) == 0
    configure(db, provider="ollama_local", verified=False)
    assert summaries.source_facts_for_today(db, task.id)


@pytest.mark.parametrize("reply", [{"content": "not json"}, {"content": '{"summary":" "}'},
    {"content": '{"summary":"ok","extra":1}'}, {"content": '{"summary":"' + "x" * 601 + '"}'},
    {"content": None}, {"content": '{"summary":"ok"}', "tool_calls": [{"id": "x"}]}])
@pytest.mark.asyncio
async def test_invalid_generation_is_bounded_and_has_no_fake_summary(db, monkeypatch, reply):
    task, _ = seed(db)
    configure(db)
    provider = Provider(reply)
    monkeypatch.setattr(summaries, "chat_provider", lambda db: provider)
    for _ in range(3):
        assert await summaries.run_summaries(db) == 0
    assert len(provider.calls) == 2
    fact = summaries.source_facts_for_today(db, task.id)[0]
    assert fact["description_summary_status"] == "failed" and fact["description_summary"] is None


@pytest.mark.parametrize("mutation", ["description", "clear"])
@pytest.mark.asyncio
async def test_change_during_request_cannot_publish_stale_output(db, monkeypatch, mutation):
    task, item = seed(db)
    configure(db)
    def mutate():
        if mutation == "clear":
            ai_probe_secrets.clear()
        else:
            db.add(SourceSnapshot(source_item_id=item.id, content_hash="y", structured_json={"description": "new"}))
            db.commit()
    monkeypatch.setattr(summaries, "chat_provider", lambda db: Provider(mutate=mutate))
    assert await summaries.run_summaries(db) == 0
    assert db.scalar(select(func.count(AIRun.id))) == 0
    assert db.scalar(select(AnalysisJob)).state == "SUPERSEDED"


@pytest.mark.asyncio
async def test_worker_handles_summary_kind_without_effort_provider_and_skips_missing_description(db, monkeypatch):
    task, _ = seed(db)
    configure(db)
    provider = Provider()
    monkeypatch.setattr(summaries, "chat_provider", lambda db: provider)
    worker = AnalysisWorker(Settings(llm_provider="disabled"))
    assert await worker.run_pending(db) == 1
    assert db.scalar(select(AnalysisJob)).state == "READY"
    assert summaries.source_facts_for_today(db, task.id)[0]["description_summary_status"] == "ready"
    _, second = seed(db, "")
    assert await worker.run_pending(db) == 0
    assert summaries.source_facts_for_today(db, task.id)[0]["description_summary"]
    assert summaries.summary_input(db, second.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["enqueue_error", "slow_call_order"])
async def test_summary_failure_and_slow_call_do_not_block_main_queue(db, monkeypatch, failure):
    configure(db)
    worker = AnalysisWorker(Settings(llm_provider="mock"))
    job = AnalysisJob(kind="task_effort", entity_id=123, input_hash="fixture", state="PENDING")
    db.add(job)
    db.commit()
    async def main_task(db, job):
        return
    monkeypatch.setattr(worker, "_task", main_task)
    if failure == "enqueue_error":
        def fail(db, **kwargs):
            raise ValueError("fixture enqueue failure")
        monkeypatch.setattr(summaries, "enqueue_visible_summaries", fail)
    else:
        async def slow_summary(db, max_attempts, settings=None):
            assert job.state == "READY", "Main queue must run before a slow summary request"
            return 0
        monkeypatch.setattr(summaries, "run_summaries", slow_summary)
    assert await worker.run_pending(db) == 1
    assert job.state == "READY"


@pytest.mark.asyncio
async def test_restored_input_retries_superseded_job_without_resetting_budget(db, monkeypatch):
    task, _ = seed(db)
    configure(db)
    first = Provider(mutate=ai_probe_secrets.clear)
    monkeypatch.setattr(summaries, "chat_provider", lambda db: first)
    assert await summaries.run_summaries(db) == 0
    job = db.scalar(select(AnalysisJob))
    assert (job.state, job.attempts) == ("SUPERSEDED", 1)
    configure(db)
    recovered = Provider()
    monkeypatch.setattr(summaries, "chat_provider", lambda db: recovered)
    assert await summaries.run_summaries(db) == 1
    assert (job.state, job.attempts) == ("READY", 2)
    assert await summaries.run_summaries(db) == 0
    assert len(first.calls) == len(recovered.calls) == 1
    assert db.scalar(select(func.count(AnalysisJob.id))) == 1
    assert summaries.source_facts_for_today(db, task.id)[0]["description_summary_status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("state,attempts", [("READY", 1), ("READY", 2), ("SUPERSEDED", 2)])
async def test_missing_cache_recovery_respects_existing_attempt_limit(db, monkeypatch, state, attempts):
    task, item = seed(db)
    configure(db)
    _, digest = summaries.summary_input(db, item.id)
    job = AnalysisJob(kind=summaries.KIND, entity_id=item.id, input_hash=digest, state=state, attempts=attempts)
    db.add(job)
    db.commit()
    provider = Provider()
    monkeypatch.setattr(summaries, "chat_provider", lambda db: provider)
    for _ in range(3):
        await summaries.run_summaries(db)
    assert len(provider.calls) == 2-attempts
    assert job.attempts == 2
    assert job.state == ("READY" if attempts == 1 else "FAILED")
    assert summaries.source_facts_for_today(db, task.id)[0]["description_summary_status"] == (
        "ready" if attempts == 1 else "failed")


@pytest.mark.asyncio
async def test_configured_capacity_matches_http_chat_and_background_discovery(db, monkeypatch):
    from app.services.ai_chat import _today
    now = datetime(2026, 10, 19, 16, tzinfo=UTC)
    task, _ = seed(db)
    task.due_at = now + timedelta(days=2)
    db.commit()
    configure(db)
    settings = Settings(llm_provider="disabled", daily_study_capacity_minutes=30)
    original = TodayEngine.build
    monkeypatch.setattr(TodayEngine, "build", lambda self, db, **kw: original(self, db, now=now, **kw))
    # This is the constructor used by the HTTP route; the default 180 would hide it.
    assert not TodayEngine().build(db)["work"]
    expected = TodayEngine(settings.daily_study_capacity_minutes).build(db)
    assert [row["task_id"] for row in expected["work"]] == [task.id]
    assert _today(db, None, settings) == expected
    monkeypatch.setattr(summaries, "get_settings", lambda: settings)
    summaries.enqueue_visible_summaries(db)
    assert db.scalar(select(func.count(AnalysisJob.id))) == 1
    db.delete(db.scalar(select(AnalysisJob)))
    db.commit()
    provider = Provider()
    monkeypatch.setattr(summaries, "chat_provider", lambda db: provider)
    # The worker must pass its injected settings, not silently read defaults.
    monkeypatch.setattr(summaries, "get_settings", lambda: Settings())
    assert await AnalysisWorker(settings).run_pending(db) == 1
    assert len(provider.calls) == 1
