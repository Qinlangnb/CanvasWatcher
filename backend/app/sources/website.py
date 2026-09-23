import re
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from bs4 import BeautifulSoup

from app.auth.fetchers import ResourceFetcher, resource_fetcher
from app.auth.models import AuthRule, FetchResult, FetchStatus
from app.auth.scope import in_auth_scope
from app.db import Course
from app.schemas import DiscoveredCourse, RawSourceItem
from app.services.normalizer import canonicalize_url, normalize_text
from app.sources.phys225_schedule import parse_phys225_schedule

logger = structlog.get_logger()

FILE_EXTENSIONS = {
    ".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx",
    ".csv", ".txt", ".md", ".zip", ".ipynb", ".py",
}


class ResourceFetchError(RuntimeError):
    def __init__(self, status: FetchStatus):
        super().__init__(f"Resource fetch did not return content: {status}")
        self.status = status


class WebsiteAdapter:
    def __init__(
        self,
        *,
        name: str,
        course_code: str,
        url: str | None = None,
        base_url: str | None = None,
        mode: str = "http",
        link_selectors: list[str] | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        extensions: list[str] | None = None,
        public_pages: list[str] | None = None,
        auth_rules: list[dict] | None = None,
        discovery: dict | None = None,
        download: dict | None = None,
        timezone: str = "America/Chicago",
        term_calendar: dict | None = None,
        browser_profile_root: Path = Path("/data/browser_profiles"),
        fetcher: ResourceFetcher = resource_fetcher,
    ):
        self.name = name
        self.course_code = course_code
        self.url = canonicalize_url(base_url or url or "")
        self.mode = mode
        self.link_selectors = link_selectors or ["a"]
        self.includes = [re.compile(value) for value in (include_patterns or [])]
        self.excludes = [re.compile(value) for value in (exclude_patterns or [])]
        self.extensions = FILE_EXTENSIONS | {
            value.lower() for value in (extensions or [])
        }
        self.public_pages = public_pages or []
        self.auth_rules = [
            AuthRule(
                path_prefix=row["path_prefix"],
                auth_type=row["auth"]["type"],
                credential_id=row["auth"]["credential_id"],
                probe_url=row["auth"]["probe_url"],
                base_url=self.url,
            )
            for row in (auth_rules or [])
        ]
        discovery = discovery or {}
        self.same_origin_only = discovery.get("same_origin_only", True)
        self.allowed_path_prefixes = discovery.get("allowed_path_prefixes", [])
        self.max_depth = max(0, min(int(discovery.get("max_depth", 1)), 5))
        self.download_enabled = (download or {}).get("enabled", True)
        self.timezone = timezone
        self.term_calendar = term_calendar or {}
        self.browser_profile = browser_profile_root / re.sub(
            r"[^A-Za-z0-9._-]", "_", name
        )
        self.fetcher = fetcher
        self._results: dict[str, FetchResult] = {}

    async def discover_courses(self) -> list[DiscoveredCourse]:
        return []

    def auth_rule_for(self, url: str) -> AuthRule | None:
        matching = [rule for rule in self.auth_rules
                    if in_auth_scope(url, self.url, rule.path_prefix)]
        return max(matching, key=lambda rule: len(rule.path_prefix), default=None)

    def _allowed(self, url: str) -> bool:
        target = urlsplit(url)
        base = urlsplit(self.url)
        if self.same_origin_only and (
            target.scheme.lower(), target.netloc.lower()
        ) != (base.scheme.lower(), base.netloc.lower()):
            return False
        return not self.allowed_path_prefixes or any(
            target.path.startswith(prefix) for prefix in self.allowed_path_prefixes
        )

    def _included(self, label: str, url: str) -> bool:
        searchable = f"{label} {url}"
        if self.includes and not any(rule.search(searchable) for rule in self.includes):
            return False
        return not any(rule.search(searchable) for rule in self.excludes)

    async def _playwright_result(self, url: str) -> FetchResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Install the playwright optional dependency for playwright mode"
            ) from exc
        self.browser_profile.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as play:
            context = await play.chromium.launch_persistent_context(
                str(self.browser_profile), headless=True
            )
            page = await context.new_page()
            response = await page.goto(url, wait_until="networkidle")
            content = (await page.content()).encode()
            status_code = response.status if response else 200
            await context.close()
        return FetchResult(
            status=FetchStatus.OK if status_code < 300 else FetchStatus.ERROR,
            url=url,
            status_code=status_code,
            content=content,
            content_type="text/html",
        )

    async def _fetch(self, url: str) -> FetchResult:
        rule = self.auth_rule_for(url)
        if self.mode == "playwright" and rule is None:
            result = await self._playwright_result(url)
        else:
            result = await self.fetcher.fetch(url, rule)
        self._results[canonicalize_url(url)] = result
        return result

    @staticmethod
    def _is_html(url: str, content_type: str | None) -> bool:
        suffix = Path(urlsplit(url).path).suffix.lower()
        return bool(
            content_type == "text/html"
            or suffix in {"", ".html", ".htm", ".asp", ".aspx"}
        )

    def _pending_item(
        self,
        url: str,
        title: str,
        result: FetchResult,
        rule: AuthRule | None,
    ) -> RawSourceItem:
        suffix = Path(urlsplit(url).path).suffix.lower()
        is_file = suffix in self.extensions
        return RawSourceItem(
            source_type="website",
            source_name=self.name,
            external_id=url,
            item_type="website_file" if is_file else "website_page",
            title=title,
            url=url,
            structured={
                "url": url,
                "label": title,
                "fetch_status": result.status.value,
                "auth_scheme": result.auth_scheme,
                "auth_rule": rule.model_dump() if rule else None,
                "error": result.error,
            },
            normalized_text=title,
            fetch_status=result.status.value,
            credential_id=rule.credential_id if rule else None,
            content_type=result.content_type,
        )

    async def fetch_items(self, course: Course, since=None) -> list[RawSourceItem]:
        del course, since
        seeds = self.public_pages or [self.url]
        queue = deque(
            (canonicalize_url(seed, self.url), 0, Path(seed).name or self.name)
            for seed in seeds
        )
        seen: set[str] = set()
        items: list[RawSourceItem] = []
        while queue:
            url, depth, title = queue.popleft()
            if url in seen or not self._allowed(url):
                continue
            seen.add(url)
            result = await self._fetch(url)
            rule = self.auth_rule_for(url)
            if result.status is not FetchStatus.OK or result.content is None:
                items.append(self._pending_item(url, title, result, rule))
                continue
            is_html = self._is_html(url, result.content_type)
            if not is_html:
                items.append(
                    RawSourceItem(
                        source_type="website",
                        source_name=self.name,
                        external_id=url,
                        item_type="website_file",
                        title=title,
                        url=url,
                        structured={
                            "url": url,
                            "label": title,
                            "fetch_status": result.status.value,
                            "etag": result.etag,
                            "last_modified": result.last_modified,
                        },
                        normalized_text=title,
                        is_file=self.download_enabled,
                        download_url=url if self.download_enabled else None,
                        credential_id=rule.credential_id if rule else None,
                        content_type=result.content_type,
                        etag=result.etag,
                        last_modified=result.last_modified,
                        http_status=result.status_code,
                        content_length=result.content_length,
                    )
                )
                continue
            soup = BeautifulSoup(result.content, "lxml")
            page_title = (
                normalize_text(soup.title.get_text(" ")) if soup.title else title
            )
            items.append(
                RawSourceItem(
                    source_type="website",
                    source_name=self.name,
                    external_id=url,
                    item_type="website_page",
                    title=page_title,
                    url=url,
                    structured={
                        "url": url,
                        "title": page_title,
                        "fetch_status": result.status.value,
                        "etag": result.etag,
                        "last_modified": result.last_modified,
                    },
                    normalized_text=normalize_text(soup.get_text(" ")),
                    credential_id=rule.credential_id if rule else None,
                    content_type=result.content_type,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    http_status=result.status_code,
                    content_length=result.content_length,
                )
            )
            is_homework_schedule = (
                re.sub(r"\s+", "", self.course_code).upper() == "PHYS225"
                and urlsplit(url).path.lower().endswith("/schedule.html")
            )
            if is_homework_schedule and not self.term_calendar.get("week_1_monday"):
                logger.warning("website_deadline_anchor_missing", source_name=self.name,
                               hint="Configure an exact source_calendars URL and week_1_monday")
            if is_homework_schedule and self.term_calendar.get("week_1_monday"):
                for homework in parse_phys225_schedule(
                    result.content,
                    week_1_monday=self.term_calendar["week_1_monday"],
                    schedule_url=url,
                ):
                    items.append(
                        RawSourceItem(
                            source_type="website",
                            source_name=self.name,
                            external_id=f"{url}#homework-{homework.number}",
                            item_type="assignment",
                            title=f"HW {homework.number}",
                            url=url,
                            structured={
                                "source_key": homework.source_key,
                                "task_type": "homework",
                                "assigned_date_local": homework.assigned_date.isoformat()
                                if homework.assigned_date
                                else None,
                                "due_date_local": homework.due_date.isoformat()
                                if homework.due_date
                                else None,
                                "deadline_precision": "DATE_ONLY",
                                "deadline_timezone": self.timezone,
                                "source_deadline_text": homework.due_text,
                                "deadline_source_rank": 3,
                                "deadline_evidence": {
                                    "method": "schedule_week_and_weekday",
                                    "week_1_monday": self.term_calendar["week_1_monday"],
                                    "calendar_source_url": self.term_calendar.get("source_url"),
                                },
                                "assignment_resource_url": homework.assignment_resource_url,
                                "solution_resource_url": homework.solution_resource_url,
                                "schedule_url": url,
                            },
                            normalized_text=" ".join(
                                value
                                for value in [homework.assigned_text, homework.due_text]
                                if value
                            ),
                        )
                    )
            if depth >= self.max_depth:
                continue
            for selector in self.link_selectors:
                for node in soup.select(selector):
                    href = node.get("href")
                    if not href:
                        continue
                    linked_url = canonicalize_url(href, url)
                    label = normalize_text(node.get_text(" "))
                    if self._allowed(linked_url) and self._included(label, linked_url):
                        queue.append(
                            (
                                linked_url,
                                depth + 1,
                                label or Path(urlsplit(linked_url).path).name or linked_url,
                            )
                        )
        return items

    async def fetch_file(self, item: RawSourceItem) -> bytes:
        url = canonicalize_url(item.download_url or item.url or "")
        result = self._results.get(url) or await self._fetch(url)
        if result.status is not FetchStatus.OK or result.content is None:
            raise ResourceFetchError(result.status)
        return result.content
