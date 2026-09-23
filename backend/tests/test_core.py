from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas import CoursePolicyData
from app.services.diff import classify_change
from app.services.downloader import classify_file, safe_filename, versioned_path
from app.services.normalizer import canonicalize_url, content_hash, normalize_text
from app.services.planner import split_minutes
from app.services.priority import calculate_priority, urgency_score
from app.services.syllabus import extract_html_text


def test_normalization_and_canonical_url():
    assert normalize_text(" hello\n  world ") == "hello world"
    assert canonicalize_url("/x//file.pdf?utm_source=a&b=2&a=1#part", "HTTPS://Example.EDU/course/") == "https://example.edu/x/file.pdf?a=1&b=2"


def test_content_hash_is_stable():
    assert content_hash({"b": 2, "a": 1}, "x   y") == content_hash({"a": 1, "b": 2}, "x y")


def test_deadline_change_is_critical_when_moved_earlier():
    old = {"due_at": "2026-09-05T00:00:00Z"}
    new = {"due_at": "2026-09-04T00:00:00Z"}
    assert classify_change(old, new) == ("deadline_changed", "critical", True)


def test_file_classification_and_collision(tmp_path):
    assert classify_file("ASTR405 Lecture04.pdf") == "lecture_notes"
    assert safe_filename("https://x.invalid/a%20b.pdf") == "a b.pdf"
    (tmp_path / "notes.pdf").write_bytes(b"old")
    assert versioned_path(tmp_path, "notes.pdf", 2).name.startswith("notes__")


def test_priority_and_urgency_are_bounded():
    now = datetime.now(UTC)
    assert urgency_score(now - timedelta(minutes=1), now) == 1.0
    assert 0 < urgency_score(now + timedelta(days=3), now) < 1
    score = calculate_priority({"grade_impact": 1, "urgency": 1, "dependency": 1, "failure_risk": 1, "academic_importance": 1})
    assert score == 1.0


def test_planner_block_splitting():
    assert split_minutes(195) == [90, 90, 15]
    assert split_minutes(0) == []


def test_syllabus_schema_requires_evidence():
    with pytest.raises(ValidationError):
        CoursePolicyData(course_id="1", policies_text_summary="summary", evidence=[], confidence=0.8)


def test_html_extraction_removes_navigation():
    text = extract_html_text("<nav>Menu</nav><main>Late work loses 10%.</main>")
    assert text == "Late work loses 10%."

