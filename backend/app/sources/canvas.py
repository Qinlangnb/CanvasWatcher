import asyncio
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.auth.models import CanvasCredentialValue
from app.db import Course
from app.schemas import DiscoveredCourse, RawSourceItem
from app.services.normalizer import normalize_text
from app.sources.canvas_client import CanvasClient, CanvasDownload

_COURSE_FILE_PATH = re.compile(
    r"/(?:api/v1/)?courses/(?P<course_id>\d+)/files/(?P<file_id>\d+)(?:/download)?/?$"
)
_GLOBAL_FILE_PATH = re.compile(r"/api/v1/files/(?P<file_id>\d+)/?$")


def canvas_file_id_from_reference(
    value: str | None,
    *,
    course_id: str,
    base_url: str,
) -> str | None:
    """Normalize a same-origin Canvas file URL/API path to its stable file ID."""
    if not value or not value.strip():
        return None
    absolute = urljoin(f"{base_url.rstrip('/')}/", value.strip())
    expected = urlsplit(base_url)
    actual = urlsplit(absolute)
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        return None
    path = unquote(actual.path).rstrip("/")
    course_match = _COURSE_FILE_PATH.fullmatch(path)
    if course_match:
        if course_match.group("course_id") != str(course_id):
            return None
        return course_match.group("file_id")
    global_match = _GLOBAL_FILE_PATH.fullmatch(path)
    return global_match.group("file_id") if global_match else None


def extract_canvas_page_file_ids(
    body_html: str | None,
    *,
    course_id: str,
    base_url: str,
) -> list[str]:
    """Extract direct file references only; this never follows Page or external links."""
    if not body_html:
        return []
    soup = BeautifulSoup(body_html, "lxml")
    discovered: dict[str, None] = {}
    for element in soup.find_all(True):
        candidates: list[str] = []
        endpoint = element.get("data-api-endpoint")
        return_type = str(element.get("data-api-returntype") or "").lower()
        if endpoint and return_type == "file":
            # Rich-content metadata is the most deterministic reference.
            candidates.append(str(endpoint))
        elif endpoint:
            candidates.append(str(endpoint))
        if element.name in {"a", "iframe", "embed"} and element.get("href"):
            candidates.append(str(element.get("href")))
        if element.name in {"iframe", "embed"} and element.get("src"):
            candidates.append(str(element.get("src")))
        if element.name == "object" and element.get("data"):
            candidates.append(str(element.get("data")))
        for candidate in candidates:
            file_id = canvas_file_id_from_reference(
                candidate,
                course_id=str(course_id),
                base_url=base_url,
            )
            if file_id:
                discovered.setdefault(file_id, None)
    return list(discovered)


def _html_text(value: str | None) -> str:
    if not value:
        return ""
    return normalize_text(BeautifulSoup(value, "lxml").get_text(" "))


def _task_kind(title: str, group_name: str | None = None) -> str:
    value = f"{group_name or ''} {title}".lower()
    if "project" in value:
        return "project"
    if re.search(r"\bexam|midterm|final|quiz\b", value):
        return "exam"
    if "reading" in value:
        return "reading"
    return "homework" if re.search(r"\bhw\b|homework", value) else "assignment"


class CanvasAdapter:
    """Normalize one Canvas course through the shared source pipeline."""

    name = "canvas"

    def __init__(
        self,
        base_url: str,
        access_token: str = "",
        max_concurrency: int = 3,
        *,
        credential: CanvasCredentialValue | None = None,
        canvas_course_id: str | None = None,
        course_source_id: int | None = None,
        client: CanvasClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or CanvasClient(
            self.base_url,
            access_token,
            max_concurrency,
            credential=credential,
        )
        self.canvas_course_id = canvas_course_id
        self.course_source_id = course_source_id
        self.source_name = (
            f"canvas:{canvas_course_id}" if canvas_course_id is not None else "canvas"
        )
        self.errors: dict[str, str] = {}
        self.error_details: dict[str, dict] = {}
        self.successful_item_types: set[str] = set()

    async def discover_courses(self) -> list[DiscoveredCourse]:
        rows = await self.client.get_all_pages(
            "/api/v1/courses",
            [
                ("enrollment_state", "active"),
                ("include[]", "term"),
                ("include[]", "total_scores"),
                ("per_page", "100"),
            ],
        )
        courses: list[DiscoveredCourse] = []
        for row in rows:
            if row.get("access_restricted_by_date") is True:
                continue
            if row.get("workflow_state") in {"completed", "deleted"}:
                continue
            enrollments = row.get("enrollments") or []
            if enrollments and all(
                enrollment.get("enrollment_state") not in {"active", "invited"}
                for enrollment in enrollments
            ):
                continue
            code = (
                row.get("course_code")
                or row.get("sis_course_id")
                or row.get("name")
                or str(row["id"])
            )
            term = row.get("term") or {}
            courses.append(
                DiscoveredCourse(
                    source="canvas",
                    external_id=str(row["id"]),
                    course_code=code,
                    name=row.get("name") or code,
                    term=term.get("name"),
                    term_id=str(term["id"]) if term.get("id") is not None else None,
                    term_start_at=term.get("start_at"),
                    term_end_at=term.get("end_at"),
                    start_at=row.get("start_at"),
                    end_at=row.get("end_at"),
                    metadata=row,
                )
            )
        return courses

    def _record_error(self, kind: str, exc: Exception) -> None:
        code = getattr(getattr(exc, "code", None), "value", "invalid_response")
        self.errors[kind] = code
        self.error_details[kind] = {"resource": kind, "code": code, "http_status": getattr(exc, "status_code", None)}

    async def _collection(
        self,
        kind: str,
        path: str,
        params: Any = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = await self.client.get_all_pages(path, params)
            self.successful_item_types.add(kind)
            return rows
        except Exception as exc:
            self._record_error(kind, exc)
            return []

    async def fetch_items(
        self, course: Course, since: datetime | None = None
    ) -> list[RawSourceItem]:
        del since
        course_id = self.canvas_course_id or course.external_id
        if not course_id:
            raise ValueError("Canvas course source has no Canvas course ID")
        self.errors = {}
        self.error_details = {}
        self.successful_item_types = set()
        specs = [
            (
                "assignment_group",
                f"/api/v1/courses/{course_id}/assignment_groups",
                [("include[]", "rules"), ("per_page", "100")],
            ),
            (
                "assignment",
                f"/api/v1/courses/{course_id}/assignments",
                [
                    ("include[]", "submission"),
                    ("include[]", "all_dates"),
                    ("override_assignment_dates", "true"),
                    ("per_page", "100"),
                ],
            ),
            ("file", f"/api/v1/courses/{course_id}/files", {"per_page": "100"}),
            ("folder", f"/api/v1/courses/{course_id}/folders", {"per_page": "100"}),
            ("page", f"/api/v1/courses/{course_id}/pages", {"per_page": "100"}),
            ("module", f"/api/v1/courses/{course_id}/modules", {"per_page": "100"}),
            (
                "announcement",
                "/api/v1/announcements",
                [("context_codes[]", f"course_{course_id}"), ("per_page", "100")],
            ),
            (
                "calendar_event",
                "/api/v1/calendar_events",
                [
                    ("context_codes[]", f"course_{course_id}"),
                    ("type", "event"),
                    ("per_page", "100"),
                ],
            ),
        ]
        results = await asyncio.gather(
            *(self._collection(kind, path, params) for kind, path, params in specs)
        )
        grouped = {
            kind: rows for (kind, _, _), rows in zip(specs, results, strict=True)
        }
        grouped["assignment"] = await asyncio.gather(*(
            self._assignment_detail(course_id, row) for row in grouped.get("assignment", [])
        ))

        page_rows = grouped.get("page", [])
        if page_rows:
            details = await asyncio.gather(
                *(self._page_detail(course_id, row) for row in page_rows)
            )
            grouped["page"] = [row for row in details if row is not None]
            await self._merge_page_files(course_id, grouped)

        module_items: list[dict[str, Any]] = []
        for module in grouped.get("module", []):
            rows = await self._collection(
                "module_item",
                f"/api/v1/courses/{course_id}/modules/{module['id']}/items",
                {"per_page": "100"},
            )
            for row in rows:
                module_items.append(
                    {
                        **row,
                        "module_id": module["id"],
                        "module_name": module.get("name"),
                    }
                )

        items: list[RawSourceItem] = []
        assignment_groups = {
            str(row.get("id")): row.get("name")
            for row in grouped.get("assignment_group", [])
        }
        for kind, rows in grouped.items():
            for row in rows:
                items.append(
                    self._normalize(kind, row, course_id, assignment_groups)
                )
        items.extend(
            self._normalize("module_item", row, course_id, assignment_groups)
            for row in module_items
        )

        try:
            details = await self.client.get_json(
                f"/api/v1/courses/{course_id}",
                [("include[]", "syllabus_body"), ("include[]", "term")],
            )
            self.successful_item_types.update({"course_metadata", "syllabus"})
            if isinstance(details, dict):
                items.append(
                    self._normalize(
                        "course_metadata", details, course_id, assignment_groups
                    )
                )
                if details.get("syllabus_body"):
                    items.append(
                        self._normalize(
                            "syllabus",
                            {
                                "id": f"course-{course_id}-syllabus",
                                "title": "Syllabus",
                                "body": details["syllabus_body"],
                                "html_url": f"{self.base_url}/courses/{course_id}/assignments/syllabus",
                                "updated_at": details.get("updated_at"),
                            },
                            course_id,
                            assignment_groups,
                        )
                    )
        except Exception as exc:
            self._record_error("course_metadata", exc)
        return items

    async def _assignment_detail(self, course_id: str, row: dict) -> dict:
        if "due_at" in row and "description" in row:
            return row
        try:
            detail = await self.client.get_json(
                f"/api/v1/courses/{course_id}/assignments/{row['id']}",
                [("include[]", "submission"), ("all_dates", "true"),
                 ("override_assignment_dates", "true")],
            )
            if not isinstance(detail, dict) or str(detail.get("id")) != str(row["id"]):
                raise ValueError("Assignment detail identity mismatch")
            return {**row, **detail}
        except Exception as exc:
            self._record_error("assignment_detail", exc)
            return row

    async def _merge_page_files(
        self,
        course_id: str,
        grouped: dict[str, list[dict[str, Any]]],
    ) -> None:
        contexts: dict[str, list[str]] = {}
        pages: dict[str, list[str]] = {}
        for page in grouped.get("page", []):
            title = str(page.get("title") or page.get("url") or "Canvas Page")
            slug = str(page.get("url") or title)
            for file_id in extract_canvas_page_file_ids(
                page.get("body"),
                course_id=course_id,
                base_url=self.base_url,
            ):
                contexts.setdefault(file_id, []).append(title)
                pages.setdefault(file_id, []).append(slug)

        if not contexts:
            return
        file_rows = grouped.setdefault("file", [])
        known = {str(row.get("id")): row for row in file_rows if row.get("id") is not None}
        missing_ids = [file_id for file_id in contexts if file_id not in known]
        if missing_ids:
            details = await asyncio.gather(
                *(self._page_file_detail(course_id, file_id) for file_id in missing_ids)
            )
            for row in details:
                if row is not None and row.get("id") is not None:
                    known[str(row["id"])] = row
                    file_rows.append(row)

        for file_id, titles in contexts.items():
            row = known.get(file_id)
            if row is None:
                continue
            row["page_context_titles"] = sorted(set(titles))
            row["discovered_via_pages"] = sorted(set(pages[file_id]))

    async def _page_file_detail(
        self, course_id: str, file_id: str
    ) -> dict[str, Any] | None:
        try:
            payload = await self.client.get_json(
                f"/api/v1/courses/{course_id}/files/{file_id}"
            )
            if isinstance(payload, dict):
                self.successful_item_types.add("file")
                return payload
        except Exception as exc:
            self._record_error(f"page_file:{file_id}", exc)
        return None

    async def _page_detail(
        self, course_id: str, row: dict[str, Any]
    ) -> dict[str, Any] | None:
        slug = row.get("url")
        if not slug:
            return row
        try:
            payload = await self.client.get_json(
                f"/api/v1/courses/{course_id}/pages/{quote(str(slug), safe='')}"
            )
            return payload if isinstance(payload, dict) else row
        except Exception as exc:
            self._record_error("page", exc)
            return None

    def _normalize(
        self,
        kind: str,
        row: dict[str, Any],
        course_id: str,
        assignment_groups: dict[str, str],
    ) -> RawSourceItem:
        row_id = row.get("id") or row.get("url") or row.get("title") or kind
        external_id = f"{kind}:{row_id}"
        title = (
            row.get("name")
            or row.get("title")
            or row.get("display_name")
            or f"{kind.replace('_', ' ')} {row_id}"
        )
        url = row.get("html_url") or (
            row.get("url") if kind not in {"file", "page"} else None
        )
        body = row.get("description") or row.get("body") or row.get("message") or ""
        normalized_text = _html_text(body) if isinstance(body, str) else ""
        structured = dict(row)
        download_url = None
        content_type = None
        content_length = None

        if kind == "file":
            download_url = structured.pop("url", None)
            structured.pop("thumbnail_url", None)
            content_type = row.get("content-type") or row.get("content_type")
            content_length = row.get("size")
            url = (
                row.get("html_url")
                or f"{self.base_url}/courses/{course_id}/files/{row_id}"
            )
        elif kind == "assignment":
            # Canvas due_at is already personalized (override_assignment_dates=true).
            # all_dates is evidence for multiple audiences, not a date-selection list.
            group_name = assignment_groups.get(str(row.get("assignment_group_id")))
            structured.update(
                {
                    "assignment_group_name": group_name,
                    "task_type": _task_kind(str(title), group_name),
                    "description": normalized_text,
                    "description_state": "known" if "description" in row else "missing",
                    "due_at_state": "known" if row.get("due_at") else "removed" if "due_at" in row else "missing",
                    "deadline_precision": "EXACT_DATETIME"
                    if row.get("due_at")
                    else None,
                    "deadline_timezone": "America/Chicago",
                    "deadline_source_rank": 1,
                    "source_deadline_text": row.get("due_at"),
                    "source_key": self._assignment_source_key(
                        str(title), course_id, row_id
                    ),
                }
            )
        elif kind == "module_item":
            structured.pop("url", None)
            structured["content_id"] = row.get("content_id")

        return RawSourceItem(
            source_type="canvas",
            source_name=self.source_name,
            external_id=external_id,
            item_type=kind,
            title=str(title),
            url=url,
            source_created_at=row.get("created_at") or row.get("posted_at"),
            source_updated_at=row.get("updated_at") or row.get("modified_at"),
            structured=structured,
            normalized_text=normalized_text,
            is_file=kind == "file",
            download_url=download_url,
            content_type=content_type,
            content_length=content_length,
        )

    @staticmethod
    def _assignment_source_key(title: str, course_id: str, row_id: Any) -> str:
        match = re.search(r"\b(?:HW|Homework)\s*0*(\d+)\b", title, re.IGNORECASE)
        return (
            f"homework:{int(match.group(1))}"
            if match
            else f"canvas_assignment:{course_id}:{row_id}"
        )

    async def fetch_file(self, item: RawSourceItem) -> bytes:
        return (await self.fetch_file_result(item)).content

    async def fetch_file_result(self, item: RawSourceItem) -> CanvasDownload:
        if not item.download_url:
            raise ValueError("Canvas item has no download URL")
        return await self.client.download(item.download_url)
