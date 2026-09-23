"""Isolated student HTML compatibility layer; never submits or mutates coursework.

Selectors originate from the authenticated student DOM observed 2026-09-18.
Missing stable IDs are reported, not replaced by title-derived identities.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.schemas import DiscoveredCourse, RawSourceItem


class GradescopeParseError(ValueError):
    def __init__(self):
        super().__init__("PROVIDER_RESPONSE_CHANGED")


@dataclass
class AssignmentPage:
    items: list[RawSourceItem]
    unidentified_rows: int = 0
    # Even a valid empty page is not proof that old work was deleted.
    deletion_authoritative: bool = False


def _same_origin_path(href: str, base: str) -> str | None:
    candidate, expected = urlsplit(urljoin(base + "/", href)), urlsplit(base)
    if (candidate.scheme, candidate.netloc) != (expected.scheme, expected.netloc):
        return None
    if candidate.username or candidate.password or candidate.query or candidate.fragment:
        return None
    return candidate.path


def parse_courses(html: str, base: str) -> list[DiscoveredCourse]:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.find("h1", string=re.compile(r"^\s*Course Dashboard\s*$"))
    if heading is None:
        raise GradescopeParseError()
    result = []
    for link in soup.select("a.courseBox[href]"):
        path = _same_origin_path(link["href"], base)
        match = re.fullmatch(r"/courses/(\d+)/?", path or "")
        code, title = link.select_one(".courseBox--shortname"), link.select_one(".courseBox--name")
        if not match or not code or not title:
            raise GradescopeParseError()
        # Term is deliberately unknown until its container markup is verified.
        result.append(DiscoveredCourse(source="gradescope", external_id=match[1],
            course_code=code.get_text(" ", strip=True), name=title.get_text(" ", strip=True),
            metadata={"url": base + path, "identity_evidence": "student_course_link"}))
    return result


def _time(node) -> str | None:
    if node is None:
        return None
    value = node.get("datetime")
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError):
        raise GradescopeParseError() from None
    return parsed.isoformat()


def parse_assignments(html: str, base: str, course_id: str) -> AssignmentPage:
    if not re.fullmatch(r"\d+", course_id):
        raise GradescopeParseError()
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#assignments-student-table")
    if table is None:
        raise GradescopeParseError()
    items, skipped, identities = [], 0, set()
    for row in table.select("tbody tr"):
        title = row.select_one("th.table--primaryLink")
        status = row.select_one("td.submissionStatus")
        if title is None or status is None:
            # A known empty-table message can never trigger deletion.
            if row.select_one("td.dataTables_empty"):
                continue
            raise GradescopeParseError()
        assignment_id, direct_url = None, None
        button = title.select_one("button[data-assignment-id]")
        if button is not None and re.fullmatch(r"\d+", button["data-assignment-id"]):
            assignment_id = button["data-assignment-id"]
        link = title.select_one("a[href]")
        if link:
            path = _same_origin_path(link["href"], base)
            match = re.fullmatch(rf"/courses/{re.escape(course_id)}/assignments/(\d+)/submissions/\d+/?", path or "")
            if not match or (assignment_id and assignment_id != match[1]):
                raise GradescopeParseError()
            assignment_id, direct_url = match[1], base + path
        if not assignment_id:
            skipped += 1
            continue
        if assignment_id in identities:
            raise GradescopeParseError()
        identities.add(assignment_id)
        due = late = released = None
        for node in row.select("time[datetime]"):
            label = node.get("aria-label", "")
            if label.startswith("Late Due Date at "):
                late = _time(node)
            elif label.startswith("Due at "):
                due = _time(node)
            elif label.startswith("Released at "):
                released = _time(node)
        text = status.get_text(" ", strip=True)
        score_match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
        state = "graded" if score_match else "submitted" if text == "Submitted" else "unsubmitted" if text == "No Submission" else "unknown"
        facts = {"provider": "gradescope", "provider_course_id": course_id,
            "provider_assignment_id": assignment_id, "due_at": due, "late_due_at": late,
            "personal_due_at": None, "release_at": released, "submission_state": state,
            "submitted_at": None, "score": float(score_match[1]) if score_match else None,
            "max_score": float(score_match[2]) if score_match else None,
            "grade_published": True if score_match else None,
            "direct_assignment_url": direct_url, "evidence": "gradescope_student_assignment_table"}
        items.append(RawSourceItem(source_type="gradescope", source_name=f"gradescope:{course_id}",
            external_id=f"assignment:{assignment_id}", item_type="assignment",
            title=title.get_text(" ", strip=True), url=direct_url or f"{base}/courses/{course_id}",
            structured={"title": title.get_text(" ", strip=True), "provider_facts": facts, "source_key": f"gradescope:{course_id}:{assignment_id}"}))
    return AssignmentPage(items, skipped)
