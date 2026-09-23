from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth.models import FetchResult, FetchStatus
from app.config import Settings
from app.db import Base, Course, CourseSource, Task
from app.schemas import TodayWorkOut
from app.services.ai_chat import EmptyArgs, TaskSearchArgs, _search_tasks, _today
from app.services.sync import SyncService
from app.services.task_links import task_source_link
from app.services.today import TodayEngine
from app.sources.config import website_term_calendar
from app.sources.phys225_schedule import parse_phys225_schedule
from app.sources.website import WebsiteAdapter

URL = "https://courses.physics.illinois.edu/phys225/fa2026/schedule.html"
HTML = (Path(__file__).parent / "fixtures" / "phys225_schedule_fa2026.html").read_bytes()


class ScheduleFetcher:
    async def fetch(self, url, auth_rule=None):
        return FetchResult(status=FetchStatus.OK, url=url, status_code=200,
                           content=HTML, content_type="text/html")


@pytest.mark.asyncio
async def test_ui_created_spaced_course_gets_dates_without_yaml_source_seed(tmp_path):
    config = tmp_path / "courses.yaml"
    config.write_text(f'courses: []\nsource_calendars:\n  {URL}:\n'
                      '    week_1_monday: "2026-08-24"\n', encoding="utf-8")
    settings = Settings(courses_config=config, download_root=tmp_path / "downloads",
                        availability_config=tmp_path / "absent.yaml",
                        file_rules_config=tmp_path / "rules.yaml")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="website", external_id="ui-course", course_code="PHYS 225",
                        name="Relativity", lifecycle_state="ACTIVE", active=True)
        db.add(course)
        db.flush()
        source = CourseSource(course_id=course.id, name="PHYS 225 Site", source_type="website",
                              external_id="website:" + URL, url=URL, enabled=True,
                              config_json={"discovery": {"max_depth": 0}})
        db.add(source)
        db.commit()
        service = SyncService(settings)
        pairs = await service.default_adapters(db, include_canvas=False)
        assert len(pairs) == 1
        assert pairs[0][1].term_calendar["week_1_monday"] == "2026-08-24"
        pairs[0][1].fetcher = ScheduleFetcher()
        await service.run(db, pairs)
        task = db.scalar(select(Task).where(Task.source_key == "homework:3"))
        assert task.due_date_local == date(2026, 9, 17)
        assert task.due_at is None and task.deadline_precision == "DATE_ONLY"
        today = TodayEngine().build(db, datetime(2026, 9, 17, 16, tzinfo=UTC))
        row = next(row for row in today["work"] if row["task_id"] == task.id)
        assert TodayWorkOut(**row).model_dump(mode="json")["due_date_local"] == "2026-09-17"
        assert row["deadline_precision"] == "DATE_ONLY"
        assert task_source_link(db, task.id) == {
            "source_url": URL.replace("schedule.html", "secure/homework/HW-03.pdf"),
            "source_link_kind": "assignment",
        }
        task.local_completed = True
        original_id = task.id
        db.commit()
        await service.run(db, pairs)
        assert db.scalar(select(func.count()).select_from(Task)) == 13
        assert db.scalar(select(func.count()).select_from(CourseSource)) == 1
        assert db.get(Task, original_id).local_completed


@pytest.mark.parametrize("anchor", ["invalid", "2026-08-25", ""])
def test_invalid_anchor_does_not_invent_dates(anchor):
    assert parse_phys225_schedule(HTML, week_1_monday=anchor, schedule_url=URL) == []


def test_keyword_evidence_includes_week_and_weekday():
    rows = parse_phys225_schedule(HTML.replace(b"HW 3 due", b"Homework 3 due"),
                                  week_1_monday="2026-08-24", schedule_url=URL)
    third = next(row for row in rows if row.number == 3)
    assert third.due_date == date(2026, 9, 17)
    assert third.due_text == "Week 4, Thursday: Homework 3 due"


def test_anchor_is_exact_source_and_term_specific(tmp_path):
    config = tmp_path / "courses.yaml"
    config.write_text(f'source_calendars:\n  {URL}:\n'
                      '    week_1_monday: "2026-08-24"\n', encoding="utf-8")
    settings = Settings(courses_config=config)
    assert website_term_calendar(settings, URL)["week_1_monday"] == "2026-08-24"
    assert website_term_calendar(settings, URL.replace("fa2026", "fa2027")) == {}
    assert website_term_calendar(settings, URL.replace("phys225", "phys226")) == {}


@pytest.mark.parametrize("day,lower,upper", [
    (date(2026, 9, 17), "2026-09-17T05:00:00Z", "2026-09-18T04:59:59Z"),
    (date(2026, 1, 17), "2026-01-17T06:00:00Z", "2026-01-18T05:59:59Z"),
])
def test_ai_date_window_includes_date_only_without_undated_or_other_days(day, lower, upper):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="website", external_id="ai-date", course_code="TEST", name="Test")
        db.add(course)
        db.flush()
        target = Task(course_id=course.id, title="Date only", due_date_local=day, deadline_precision="DATE_ONLY")
        other = Task(course_id=course.id, title="Other day", due_date_local=day.replace(day=16))
        undated = Task(course_id=course.id, title="Undated")
        exact = Task(course_id=course.id, title="Exact", due_at=datetime.fromisoformat(lower))
        db.add_all([target, other, undated, exact])
        db.commit()
        result = _search_tasks(db, TaskSearchArgs(due_from=lower, due_to=upper), Settings())
        assert {row["id"] for row in result} == {target.id, exact.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("code,url,anchor", [
    ("PHYS 226", URL, {"week_1_monday": "2026-08-24"}),
    ("PHYS 225", URL.replace("schedule.html", "notes.html"), {"week_1_monday": "2026-08-24"}),
    ("PHYS 225", URL.replace("fa2026", "fa2027"), {}),
])
async def test_non_matching_course_page_or_term_emits_no_homework(code, url, anchor, monkeypatch):
    warnings = []
    monkeypatch.setattr("app.sources.website.logger.warning", lambda event, **kw: warnings.append(event))
    adapter = WebsiteAdapter(name="test", course_code=code, base_url=url,
                             term_calendar=anchor, discovery={"max_depth": 0}, fetcher=ScheduleFetcher())
    items = await adapter.fetch_items(Course(course_code=code, name="Test", source="website", external_id="test"))
    assert not any(item.item_type == "assignment" for item in items)
    assert warnings == (["website_deadline_anchor_missing"] if not anchor else [])


@pytest.mark.parametrize("section", ["work", "completed"])
def test_ai_today_never_exposes_planner_cutoff_as_source_time(section, monkeypatch):
    instant = datetime(2026, 9, 18, 4, 59, tzinfo=UTC)
    rows = [
        {"deadline": instant, "deadline_precision": "DATE_ONLY", "due_date_local": date(2026, 9, 17)},
        {"deadline": instant, "deadline_precision": "EXACT_DATETIME", "due_date_local": None},
    ]
    monkeypatch.setattr(TodayEngine, "build", lambda self, db: {section: rows})
    result = _today(None, EmptyArgs(), Settings())[section]
    assert result[0]["deadline"] is None
    assert result[0]["due_date_local"] == date(2026, 9, 17)
    assert result[0]["deadline_precision"] == "DATE_ONLY"
    assert "not specified" in result[0]["deadline_note"]
    assert result[1]["deadline"] == instant
