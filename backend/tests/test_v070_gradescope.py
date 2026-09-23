from pathlib import Path

import pytest

from app.sources.gradescope_html import GradescopeParseError, parse_assignments, parse_courses

BASE = "https://www.gradescope.com"
FIXTURE = (Path(__file__).parent / "fixtures/v070/gradescope_assignments.html").read_text(encoding="utf-8")


def test_representative_student_fields_are_separate_and_nullable():
    result = parse_assignments(FIXTURE, BASE, "10")
    assert len(result.items) == 3
    assert result.unidentified_rows == 1
    assert not result.deletion_authoritative
    first = result.items[0].structured["provider_facts"]
    assert first["due_at"] == "2026-09-24T23:59:00-05:00"
    assert first["late_due_at"] == "2026-10-01T23:59:00-05:00"
    assert first["personal_due_at"] is None
    assert first["direct_assignment_url"] is None  # Never invent a GET route from a submit button.
    assert first["submission_state"] == "unsubmitted"
    assert result.items[1].structured["provider_facts"]["submission_state"] == "submitted"
    graded = result.items[2].structured["provider_facts"]
    assert (graded["score"], graded["max_score"], graded["grade_published"]) == (8, 10, True)
    assert graded["submitted_at"] is None


@pytest.mark.parametrize("html", ["<h1>Log in</h1>", FIXTURE.replace("assignments-student-table", "new-table"),
    FIXTURE.replace("2026-09-24 23:59:00 -0500", "tomorrow"),
    FIXTURE.replace("/courses/10/assignments/102/submissions/900", "https://other.example/courses/10/assignments/102/submissions/900")])
def test_changed_or_unsafe_response_fails_closed(html):
    with pytest.raises(GradescopeParseError, match="PROVIDER_RESPONSE_CHANGED"):
        parse_assignments(html, BASE, "10")


def test_title_changes_preserve_identity_and_unknown_status_stays_unknown():
    before = parse_assignments(FIXTURE, BASE, "10")
    after = parse_assignments(FIXTURE.replace("Homework A", "Renamed work").replace("No Submission", "New provider state"), BASE, "10")
    assert before.items[0].external_id == after.items[0].external_id
    assert after.items[0].structured["provider_facts"]["submission_state"] == "unknown"


def test_empty_parse_cannot_authorize_deletions():
    result = parse_assignments('<table id="assignments-student-table"><tbody></tbody></table>', BASE, "10")
    assert result.items == [] and not result.deletion_authoritative


def test_course_dom_contract():
    html = '<h1>Course Dashboard</h1><a class="courseBox" href="/courses/10"><h3 class="courseBox--shortname">TEST 101</h3><div class="courseBox--name">Example course</div></a>'
    course = parse_courses(html, BASE)[0]
    assert course.external_id == "10" and course.course_code == "TEST 101"
    assert course.term is None
    with pytest.raises(GradescopeParseError):
        parse_courses("<h1>Login</h1>", BASE)
