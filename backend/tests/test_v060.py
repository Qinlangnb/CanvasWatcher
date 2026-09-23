import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import (
    AIToolConfirmation,
    Base,
    ChatConversation,
    ChatMessage,
    Course,
    CoursePolicy,
    DownloadedFile,
    Plan,
    SourceItem,
    SourceSnapshot,
    Task,
)
from app.schemas import AIChatMessageIn, AIChatPageContext, AIChatRequest
from app.services.ai_chat import (
    PLANNER_INVALIDATING_TOOLS,
    TOOLS,
    ToolPermission,
    chat,
    execute_tool,
    resolve_confirmation,
)
from app.services.ics_calendar import preview_ics
from app.services.source_connections import create_website_connection
from app.services.work_tracking import record_work_heartbeat, start_task_work


@contextmanager
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def settings() -> Settings:
    return Settings(database_url="sqlite:///:memory:", scheduler_enabled=False)


def add_course(db: Session, code: str = "CS101", term: str = "Fall 2026") -> Course:
    row = Course(
        source="manual",
        external_id=f"manual:{code}",
        course_code=code,
        name=code,
        active=True,
        lifecycle_state="ACTIVE",
        term=term,
        term_name=term,
        term_id="fall-2026",
        term_sort_key="2026-08-01",
    )
    db.add(row)
    db.flush()
    return row


class FakeProvider:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def request(text: str, conversation_id: str | None = None) -> AIChatRequest:
    return AIChatRequest(
        conversation_id=conversation_id,
        messages=[AIChatMessageIn(role="user", content=text)],
        page_context=AIChatPageContext(route="/?tab=Today"),
    )


def test_tool_registry_has_typed_permissions_and_no_secret_tools() -> None:
    assert TOOLS["list_courses"].permission == ToolPermission.READ
    assert TOOLS["set_task_progress"].permission == ToolPermission.CONFIRM_WRITE
    exposed = " ".join(TOOLS).casefold()
    assert all(word not in exposed for word in ("password", "cookie", "api_key", "shell", "sql"))
    assert all(tool.args_model.model_json_schema()["type"] == "object" for tool in TOOLS.values())
    planner_writes = {
        name
        for name, tool in TOOLS.items()
        if tool.permission == ToolPermission.CONFIRM_WRITE and name != "edit_source_metadata"
    }
    assert PLANNER_INVALIDATING_TOOLS == planner_writes


def test_ics_preview_contract_is_stable_for_first_import() -> None:
    raw = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nDTSTART:20260903T150000Z\r\nDTEND:20260903T160000Z\r\nSUMMARY:Class\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    with memory_db() as db:
        result = preview_ics(db, raw, "class.ics", settings())
    assert result["status"] == "ready"
    assert result["events_found"] == 1
    assert result["reconciliation"] == {"new": 1, "updated": 0, "removed": 0, "unchanged": 0}
    assert isinstance(result["warnings"], list)


def test_existing_trimmed_term_is_reused_for_website_course() -> None:
    with memory_db() as db:
        existing = add_course(db)
        connection = create_website_connection(
            db,
            {
                "course_name": "Independent Course",
                "course_code": "IND200",
                "term": "  Fall 2026  ",
                "base_url": "https://course.example.edu",
                "authentication_method": "none",
                "discovery_path_limit": 100,
            },
        )
        created = db.get(Course, connection.course_id)
        assert created is not None
        assert created.term_id == existing.term_id
        assert created.term_name == existing.term_name
        assert connection.config_json["term"] == "Fall 2026"


def test_chat_reads_tools_persists_history_and_provenance() -> None:
    provider = FakeProvider(
        [
            {"content": "", "tool_calls": [{"id": "one", "function": {"name": "list_courses", "arguments": "{}"}}]},
            {"content": "CS101 is active.", "tool_calls": []},
        ]
    )
    with memory_db() as db:
        add_course(db)
        db.commit()
        result = asyncio.run(chat(db, request("What courses are active?"), settings(), provider=provider))
        assert result["message"] == "CS101 is active."
        assert result["tool_results"][0]["tool_name"] == "list_courses"
        assert result["tool_results"][0]["result"][0]["route"].startswith("/courses/")
        saved = list(db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == result["conversation_id"])))
        assert {row.kind for row in saved} >= {"message", "tool_result"}


def test_write_tool_does_not_mutate_until_confirmed() -> None:
    provider = FakeProvider(
        [{"content": "", "tool_calls": [{"id": "write", "function": {"name": "set_task_progress", "arguments": '{"task_id":1,"progress_percent":75}'}}]}]
    )
    with memory_db() as db:
        course = add_course(db)
        db.add(Task(id=1, course_id=course.id, source_key="task-1", title="Project", task_type="assignment", status="NOT_STARTED"))
        db.commit()
        proposed = asyncio.run(chat(db, request("Set progress to 75%"), settings(), provider=provider))
        assert proposed["confirmation"]["status"] == "PENDING"
        assert db.get(Task, 1).status == "NOT_STARTED"
        applied = asyncio.run(resolve_confirmation(db, proposed["confirmation"]["id"], "confirm", settings()))
        assert applied["confirmation"]["status"] == "APPLIED"
        assert db.get(Task, 1).status == "IN_PROGRESS"
        assert db.scalar(select(Plan).where(Plan.status == "active")) is not None


def _pending_confirmation(db: Session, tool_name: str, arguments: dict) -> AIToolConfirmation:
    conversation = ChatConversation(id=str(uuid4()))
    row = AIToolConfirmation(
        id=str(uuid4()),
        conversation_id=conversation.id,
        tool_name=tool_name,
        arguments_json=arguments,
        summary=f"Confirm {tool_name}",
        status="PENDING",
    )
    db.add_all([conversation, row])
    db.commit()
    return row


def test_confirmed_ignore_and_archive_rebuild_the_active_plan() -> None:
    with memory_db() as db:
        course = add_course(db)
        task = Task(course_id=course.id, source_key="task-1", title="Project", status="NOT_STARTED")
        db.add(task)
        db.commit()

        ignore = _pending_confirmation(db, "ignore_task", {"task_id": task.id})
        asyncio.run(resolve_confirmation(db, ignore.id, "confirm", settings()))
        first_plan = db.scalar(select(Plan).where(Plan.status == "active"))
        assert first_plan is not None
        assert db.get(Task, task.id).ignored_at is not None

        archive = _pending_confirmation(db, "archive_course", {"course_id": course.id})
        asyncio.run(resolve_confirmation(db, archive.id, "confirm", settings()))
        plans = list(db.scalars(select(Plan).order_by(Plan.id)))
        assert [row.status for row in plans] == ["superseded", "active"]
        assert db.get(Course, course.id).lifecycle_state == "ARCHIVED"
        assert db.get(Course, course.id).active is False


def test_confirmed_completion_and_commute_rebuild_the_active_plan() -> None:
    with memory_db() as db:
        course = add_course(db)
        task = Task(course_id=course.id, source_key="task-1", title="Project", status="NOT_STARTED")
        db.add(task)
        db.commit()

        complete = _pending_confirmation(db, "mark_task_completed", {"task_id": task.id})
        asyncio.run(resolve_confirmation(db, complete.id, "confirm", settings()))
        first_plan = db.scalar(select(Plan).where(Plan.status == "active"))
        assert first_plan is not None
        assert db.get(Task, task.id).status == "READY_TO_SUBMIT"

        commute = _pending_confirmation(
            db,
            "set_course_commute_minutes",
            {"course_id": course.id, "minutes": 25},
        )
        asyncio.run(resolve_confirmation(db, commute.id, "confirm", settings()))
        plans = list(db.scalars(select(Plan).order_by(Plan.id)))
        assert [row.status for row in plans] == ["superseded", "active"]
        assert db.get(Course, course.id).commute_minutes == 25


def test_file_search_matches_extracted_text_and_returns_context() -> None:
    with memory_db() as db:
        course = add_course(db)
        item = SourceItem(
            course_id=course.id,
            source_type="website",
            source_name="Course site",
            external_id="review-file",
            item_type="file",
            title="Review notes",
            current_hash="a" * 64,
        )
        db.add(item)
        db.flush()
        db.add(
            SourceSnapshot(
                source_item_id=item.id,
                content_hash="b" * 64,
                normalized_text="The review covers gravitational lensing and stellar spectra.",
            )
        )
        db.add(
            DownloadedFile(
                course_id=course.id,
                source_item_id=item.id,
                source_url="https://course.example/review.pdf",
                original_filename="review.pdf",
                local_path="/data/review.pdf",
                size_bytes=10,
                sha256="c" * 64,
            )
        )
        db.commit()
        result = asyncio.run(
            execute_tool(db, "search_files", {"query": "lensing"}, settings())
        )
        assert [row["filename"] for row in result] == ["review.pdf"]
        assert "gravitational lensing" in result[0]["match_context"]
        assert result[0]["file_id"] == result[0]["id"]
        knowledge = asyncio.run(execute_tool(db, "search_course_knowledge", {"query": "lensing"}, settings()))
        assert knowledge[0]["source_item_id"] == item.id
        assert knowledge[0]["file_id"] == result[0]["file_id"]
        text = asyncio.run(execute_tool(db, "get_file_text", {"file_id": knowledge[0]["file_id"]}, settings()))
        assert "gravitational lensing" in text["text"]


def test_exam_summary_distinguishes_multiple_exams_from_conflicting_sources() -> None:
    with memory_db() as db:
        course = add_course(db)
        db.add_all(
            [
                Task(course_id=course.id, source_key="midterm", title="Midterm 1", task_type="exam", due_at=datetime(2026, 10, 8, 23, tzinfo=UTC)),
                Task(course_id=course.id, source_key="final", title="Final exam", task_type="exam", due_at=datetime(2026, 12, 15, 20, tzinfo=UTC)),
            ]
        )
        db.commit()
        result = asyncio.run(
            execute_tool(db, "get_course_exam_summary", {"course_id": course.id}, settings())
        )
        assert result["conflict"] is False

        db.add(
            CoursePolicy(
                course_id=course.id,
                policy_json={
                    "important_assessments": [
                        {"name": "Midterm 1", "date": "2026-10-09T19:00:00-05:00"}
                    ]
                },
                evidence_json=["Syllabus"],
            )
        )
        db.commit()
        result = asyncio.run(
            execute_tool(db, "get_course_exam_summary", {"course_id": course.id}, settings())
        )
        assert result["conflict"] is True


def test_native_http_400_falls_back_to_strict_json() -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.example/chat"))
    provider = FakeProvider([httpx.HTTPStatusError("bad tools", request=response.request, response=response), {"content": '{"type":"final","content":"Fallback works."}', "tool_calls": []}])
    with memory_db() as db:
        result = asyncio.run(chat(db, request("Hello"), settings(), provider=provider))
    assert result["message"] == "Fallback works."
    assert result["provider_mode"] == "structured_fallback"
    assert provider.calls[0]["native_tools"] is True
    assert provider.calls[1]["native_tools"] is False


def test_malformed_fallback_is_normalized() -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.example/chat"))
    provider = FakeProvider([httpx.HTTPStatusError("bad tools", request=response.request, response=response), {"content": "not json", "tool_calls": []}])
    with memory_db() as db:
        result = asyncio.run(chat(db, request("Hello"), settings(), provider=provider))
    assert result["error"]["code"] == "INVALID_TOOL_OUTPUT"


def test_mid_conversation_fallback_strips_native_blocks_and_keeps_evidence() -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.example/chat"))
    native = {"role": "assistant", "content": None,
              "tool_calls": [{"id": "native-1", "type": "function", "function": {"name": "list_courses", "arguments": "{}"}}],
              "_anthropic_content": [{"type": "tool_use", "id": "native-1", "name": "list_courses", "input": {}}]}
    provider = FakeProvider([native, httpx.HTTPStatusError("bad tools", request=response.request, response=response),
                             {"content": '{"type":"final","content":"Fallback after evidence works."}', "tool_calls": []}])
    with memory_db() as db:
        add_course(db, "CS101")
        result = asyncio.run(chat(db, request("List my courses"), settings(), provider=provider))
    assert result["message"] == "Fallback after evidence works."
    assert result["provider_mode"] == "structured_fallback"
    forwarded = provider.calls[-1]["messages"]
    assert all(set(row) == {"role", "content"} for row in forwarded)
    assert all(row["role"] != "tool" and row["content"] for row in forwarded)
    assert any("CS101" in row["content"] and "Tool evidence" in row["content"] for row in forwarded)


def test_heartbeat_persists_client_remaining_snapshot() -> None:
    with memory_db() as db:
        course = add_course(db)
        task = Task(course_id=course.id, source_key="task-1", title="Project", task_type="assignment", status="IN_PROGRESS")
        db.add(task)
        db.commit()
        start_task_work(db, task.id)
        when = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)
        record_work_heartbeat(db, task.id, progress_percent=25, remaining_minutes=90, client_timestamp=when, now=when)
        db.refresh(task)
        assert task.last_client_remaining_minutes == 90
        assert task.last_client_reported_at.replace(tzinfo=UTC) == when
