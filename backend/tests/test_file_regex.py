from pathlib import Path

import pytest

from app.db import DownloadedFile
from app.services.downloader import classify_file, effective_file_type
from app.services.file_types import NORMALIZED_FILE_TYPES, normalize_file_type


@pytest.mark.parametrize("value", NORMALIZED_FILE_TYPES)
def test_canonical_types_are_idempotent(value):
    assert normalize_file_type(normalize_file_type(value)) == value


@pytest.mark.parametrize("name, category", [
    ("lecture6-microlensing.pdf", "lecture"),
    ("Print_Lecture_4_Stellar_Observables.pdf", "lecture"),
    ("Week 4, Lecture 4_.pdf", "lecture"),
    ("LEC_04.pdf", "lecture"),
    ("slides-4.pdf", "lecture"),
    ("Discussion 4.pdf", "discussion"),
    ("DISC_04.pdf", "discussion"),
    ("dis04.pdf", "discussion"),
    ("recitation-2.pdf", "discussion"),
    ("HW_04.pdf", "homework"),
    ("problem_set_02.pdf", "homework"),
    ("midterm_1.pdf", "exam"),
    ("practice_quiz02.pdf", "exam"),
    ("HW_04_solutions.pdf", "solution"),
    ("discussion-2-answer_key.pdf", "solution"),
    ("(solutions).pdf", "solution"),
    ("Handbook_of_Exoplanets.pdf", "reading"),
    ("Einstein 1905 paper.pdf", "reading"),
    ("第4周讲义.pdf", "lecture"),
    ("https://example.edu/files/Discussion%204.pdf?token=exam", "discussion"),
    ("https://example.edu/files/123?token=lecture#solutions", "other"),
    ("discovery.pdf", "other"),
    ("examination_of_transit.pdf", "other"),
    ("withdrawal.pdf", "other"),
    ("statistics.pdf", "other"),
])
def test_regex_names(name, category):
    assert normalize_file_type(classify_file(name)) == category
    rules = Path(__file__).resolve().parents[2] / "config" / "file_rules.yaml"
    assert normalize_file_type(classify_file(name, rules)) == category


def test_custom_rules_have_priority_and_builtin_fallback(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text('rules:\n  - match: {regex: "special"}\n    category: other\n', encoding="utf8")
    assert classify_file("special_lecture.pdf", rules) == "other"
    assert normalize_file_type(classify_file("Discussion_04.pdf", rules)) == "discussion"


def test_existing_files_recover_without_changing_manual_overrides():
    record = DownloadedFile(original_filename="Discussion 4.pdf", source_url="https://example.edu/files/123",
                            category="other", source_detected_type="other")
    assert effective_file_type(record) == "discussion"
    assert record.source_detected_type == "other"  # read projection, no GET-side mutation
    record.user_override_type = "other"
    assert effective_file_type(record) == "other"
    record.user_override_type = "reading"
    assert effective_file_type(record) == "reading"
    record.user_override_type = None
    record.source_detected_type = "lecture"
    assert effective_file_type(record) == "lecture"


@pytest.mark.parametrize("content", ["rules: [", "- row", "rules: [null]", "rules: [{match: {regex: '['}, category: lecture}]", "rules: [{category: lecture}]"])
def test_invalid_custom_rules_fall_back(content, tmp_path):
    rules = tmp_path / "bad.yaml"
    rules.write_text(content, encoding="utf8")
    assert classify_file("Discussion 4.pdf", rules) == "discussion"


def test_rule_cache_reuses_parse_and_invalidates_after_edit(tmp_path, monkeypatch):
    from app.services.downloader import _cached_rules
    _cached_rules.cache_clear()
    rules = tmp_path / "cache.yaml"
    rules.write_text("rules: []", encoding="utf8")
    original, reads = Path.read_text, []
    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    for _ in range(30):
        assert classify_file("Discussion 4.pdf", rules) == "discussion"
    assert len(reads) == 1
    rules.write_text('rules:\n  - match: {regex: Discussion}\n    category: reading\n', encoding="utf8")
    assert classify_file("Discussion 4.pdf", rules) == "reading"
    assert len(reads) == 2


@pytest.fixture
def classification_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.config import Settings
    from app.db import Base, Course, SourceItem
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source="manual", external_id="test", course_code="TEST101", name="Test", lifecycle_state="ACTIVE")
        db.add(course)
        db.flush()
        item = SourceItem(course_id=course.id, source_type="website", source_name="test", external_id="1",
                          item_type="file", title="HW_01.pdf", current_hash="test")
        db.add(item)
        db.flush()
        settings = Settings(download_root=tmp_path / "downloads", file_rules_config=tmp_path / "rules.yaml",
                            courses_config=tmp_path / "courses.yaml", scheduler_enabled=False)
        yield db, course, item, settings
    engine.dispose()


def test_files_api_and_ai_agree_with_bad_rules_and_manual_other(classification_db, monkeypatch):
    from app.api import routes
    from app.services.ai_chat import FileSearchArgs, _file_row, _search_files
    db, course, item, settings = classification_db
    settings.file_rules_config.write_text("rules: [", encoding="utf8")
    record = DownloadedFile(course_id=course.id, source_item_id=item.id, source_url="https://example.edu/files/1",
                            original_filename="Discussion 4.pdf", local_path="", size_bytes=1, sha256="test",
                            category="other", source_detected_type="other")
    db.add(record)
    db.flush()
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    assert routes.file(record.id, db=db)["effective_type"] == "discussion"
    assert len(routes.files(course_id=None, type="discussion", latest_only=True, limit=200, db=db)) == 1
    assert _file_row(db, record, settings)["type"] == "discussion"
    assert len(_search_files(db, FileSearchArgs(file_type="discussion"), settings)) == 1
    record.user_override_type = "other"
    db.flush()
    assert _search_files(db, FileSearchArgs(file_type="discussion"), settings) == []
    assert routes.file(record.id, db=db)["effective_type"] == "other"


@pytest.mark.parametrize("implementation", ["development", "hotfix"])
async def test_homework_file_update_keeps_attention(classification_db, implementation):
    from app.db import ChangeEvent
    from app.schemas import RawSourceItem
    from app.services.sync import SyncService
    if implementation == "hotfix":
        from hotfix_sync import SyncService
    db, course, item, settings = classification_db
    service = SyncService(settings)
    raw = RawSourceItem(source_type="website", source_name="test", external_id="1", item_type="file",
                        title="HW_01.pdf", url="https://example.edu/HW_01.pdf", content_type="application/pdf", is_file=True)
    class Adapter:
        content = b"%PDF-1.4\nfirst\n%%EOF"
        async def fetch_file(self, _raw):
            return self.content
    adapter = Adapter()
    await service._sync_file(db, course, adapter, item, raw, None, True)
    adapter.content = b"%PDF-1.4\nupdated\n%%EOF"
    event = await service._sync_file(db, course, adapter, item, raw, ChangeEvent(source_item_id=item.id), False)
    assert event.change_type == "FILE_CHANGE"
    assert event.importance == "important"
    assert event.requires_action is True
