import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.queue import enqueue_change_analysis, enqueue_task_analysis
from app.ai.service import AIService
from app.auth.models import CanvasCredentialValue
from app.config import Settings
from app.db import (
    ChangeEvent,
    Course,
    CoursePolicy,
    CourseSource,
    CredentialProfile,
    DownloadedFile,
    PendingResource,
    SourceItem,
    SourceSnapshot,
    utcnow,
)
from app.schemas import RawSourceItem
from app.services.auth import AuthService
from app.services.change_policy import (
    is_assignment_change_type,
    should_emit_change,
)
from app.services.course_terms import (
    normalize_course_display,
    normalize_term,
    should_archive_new_course,
)
from app.services.diff import classify_change, summarize_change
from app.services.downloader import FileDownloader, effective_file_type, safe_filename
from app.services.normalizer import content_hash
from app.services.notifier import NtfyNotifier
from app.services.planner import Planner
from app.services.task_engine import task_from_assignment
from app.sources.base import SourceAdapter
from app.sources.canvas import CanvasAdapter
from app.sources.canvas_client import CanvasAPIError
from app.sources.config import (
    WebsiteDefinition,
    adapter_kwargs,
    canvas_aliases,
    course_export_configs,
    website_definitions,
    website_term_calendar,
)
from app.sources.website import WebsiteAdapter

logger = structlog.get_logger()


@dataclass
class SyncState:
    state: str = "idle"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    courses: int = 0
    items_seen: int = 0
    changes: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


sync_state = SyncState()


def _normalized_course_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def stable_change_key(
    source_item_id: int,
    change_type: str,
    old_identity: str | None,
    new_identity: str | None,
) -> str:
    material = f"{source_item_id}|{change_type}|{old_identity or '-'}|{new_identity or '-'}"
    return hashlib.sha256(material.encode()).hexdigest()


def _semantic_value(value: dict[str, Any], item_type: str) -> dict[str, Any]:
    ignored = {
        "html_url",
        "url",
        "updated_at",
        "created_at",
        "lock_info",
        "permissions",
        "preview_url",
        "page_context_titles",
        "discovered_via_pages",
        # Canvas increments this value continuously after an overdue deadline;
        # it is telemetry, not a semantic assignment change.
        "seconds_late",
        "description_state", "due_at_state", "retained_fields",
    }
    if item_type == "file":
        ignored.discard("updated_at")
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(child)
                for key, child in value.items()
                if key not in ignored
            }
        if isinstance(value, list):
            return [scrub(child) for child in value]
        return value

    return scrub(value)


def _semantic_structured(raw: RawSourceItem) -> dict[str, Any]:
    return _semantic_value(raw.structured, raw.item_type)


class SyncService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.downloader = FileDownloader(
            settings.download_root,
            settings.file_rules_config,
            course_export_configs(settings),
        )
        self.ai = AIService(settings)
        self.notifier = NtfyNotifier(settings.ntfy_url, settings.ntfy_topic)
        self.auth = AuthService(settings)

    async def default_adapters(
        self,
        db: Session,
        *,
        include_canvas: bool = True,
        include_websites: bool = True,
    ) -> list[tuple[Course, SourceAdapter]]:
        pairs: list[tuple[Course, SourceAdapter]] = []
        self.auth.ensure_profiles(db)
        if include_canvas:
            credential = self.auth.canvas_credential()
            if credential is not None and self.auth.store.get_canvas(self.auth.settings.canvas_credential_id) is None:
                profile = self.auth.profile(db, self.auth.settings.canvas_credential_id)
                if profile is None or not await self.auth.verify(db, profile):
                    credential = None
                else:
                    credential = self.auth.canvas_credential()
            if credential is not None:
                try:
                    pairs.extend(await self.discover_canvas(db, credential))
                except CanvasAPIError as exc:
                    self.auth.mark_runtime_failure(db, self.auth.settings.canvas_credential_id, exc.code.value)
                    logger.warning("canvas_discovery_failed", error_code=exc.code.value)
        if include_websites:
            for definition in website_definitions(self.settings):
                code = definition.course_code
                seeded_source = db.scalar(select(CourseSource).where(
                    CourseSource.source_type == "website",
                    CourseSource.external_id == definition.config["name"],
                ))
                course = db.get(Course, seeded_source.course_id) if seeded_source else db.scalar(select(Course).where(Course.course_code == code))
                if course is None:
                    display = normalize_course_display(code, definition.course_name)
                    course = Course(
                        source="website",
                        external_id=f"configured:{code}",
                        course_code=code,
                        name=definition.course_name,
                        active=True,
                        lifecycle_state="ACTIVE",
                        display_course_code=display.course_code,
                        display_name=display.name,
                        section=display.section,
                    )
                    db.add(course)
                    db.flush()
                if course.lifecycle_state == "ARCHIVED":
                    continue
                source = db.scalar(
                    select(CourseSource).where(
                        CourseSource.course_id == course.id,
                        CourseSource.source_type == "website",
                        (CourseSource.name == definition.config["name"]) | (CourseSource.external_id == definition.config["name"]),
                    )
                )
                if source is None:
                    source = CourseSource(
                        course_id=course.id,
                        name=definition.config["name"],
                        source_type="website",
                        external_id=definition.config["name"],
                        url=definition.config.get("base_url")
                        or definition.config.get("url"),
                        mode=definition.config.get("mode", "http"),
                        enabled=True,
                    )
                    db.add(source)
                    db.flush()
                if not source.enabled:
                    continue
                if not source.config_json:
                    # Retain YAML seed access rules for the editor and credential link.
                    source.config_json = dict(definition.config)
                if source.config_json:
                    definition = WebsiteDefinition(
                        course_code=course.course_code, course_name=course.name,
                        timezone=definition.timezone, term_calendar=definition.term_calendar,
                        export=definition.export,
                        config={**definition.config, **source.config_json,
                                "name": source.name, "base_url": source.url, "mode": source.mode},
                    )
                adapter = WebsiteAdapter(**adapter_kwargs(definition))
                adapter.course_source_id = source.id
                pairs.append((course, adapter))
            configured_source_ids = {
                getattr(adapter, "course_source_id", None) for _, adapter in pairs
            }
            manual_rows = db.execute(
                select(CourseSource, Course)
                .join(Course, Course.id == CourseSource.course_id)
                .where(
                    CourseSource.source_type == "website",
                    CourseSource.enabled.is_(True),
                    Course.lifecycle_state == "ACTIVE",
                )
            ).all()
            for source, course in manual_rows:
                if source.id in configured_source_ids:
                    continue
                config = {
                    **(source.config_json or {}),
                    "name": source.name,
                    "base_url": source.url,
                    "mode": source.mode,
                }
                if not config.get("base_url"):
                    continue
                definition = WebsiteDefinition(
                    course_code=course.course_code,
                    course_name=course.name,
                    timezone="America/Chicago",
                    term_calendar=website_term_calendar(self.settings, source.url),
                    export={},
                    config=config,
                )
                adapter = WebsiteAdapter(**adapter_kwargs(definition))
                adapter.course_source_id = source.id
                pairs.append((course, adapter))
        db.commit()
        return pairs

    async def discover_canvas(
        self, db: Session, credential: CanvasCredentialValue | None = None
    ) -> list[tuple[Course, CanvasAdapter]]:
        credential = credential or self.auth.canvas_credential()
        if credential is None:
            return []
        discovery = CanvasAdapter(
            credential.base_url,
            max_concurrency=self.settings.canvas_max_concurrency,
            credential=credential,
            client=self.auth.routed_client(db),
        )
        discovered = await discovery.discover_courses()
        aliases = canvas_aliases(self.settings)
        pairs: list[tuple[Course, CanvasAdapter]] = []
        for row in discovered:
            term = normalize_term(
                row.term,
                term_id=row.term_id,
                start_at=row.term_start_at,
                end_at=row.term_end_at,
            )
            display = normalize_course_display(row.course_code, row.name, term.display_name)
            source = db.scalar(
                select(CourseSource).where(
                    CourseSource.source_type == "canvas",
                    CourseSource.external_id == row.external_id,
                )
            )
            course = db.get(Course, source.course_id) if source else None
            if course is None:
                alias_code = aliases.get(row.external_id)
                target_code = alias_code or row.course_code
                normalized = _normalized_course_code(target_code)
                candidates = list(db.scalars(select(Course).where(Course.active.is_(True))))
                exact = [
                    candidate
                    for candidate in candidates
                    if _normalized_course_code(candidate.course_code) == normalized
                ]
                course = exact[0] if len(exact) == 1 else None
                if course is None:
                    archived = should_archive_new_course(
                        term, course_end_at=row.end_at
                    )
                    course = Course(
                        source="canvas",
                        external_id=f"canvas:{row.external_id}",
                        course_code=target_code,
                        name=row.name,
                        term=row.term,
                        active=not archived,
                        start_at=row.start_at,
                        end_at=row.end_at,
                        lifecycle_state="ARCHIVED" if archived else "ACTIVE",
                        archived_at=utcnow() if archived else None,
                    )
                    db.add(course)
                    db.flush()
                source = CourseSource(
                    course_id=course.id,
                    name=f"canvas:{row.external_id}",
                    source_type="canvas",
                    external_id=row.external_id,
                    url=f"{credential.base_url}/courses/{row.external_id}",
                    mode="api",
                    enabled=True,
                    state="NEW",
                    config_json={"baseline_complete": False},
                )
                db.add(source)
                db.flush()
            if course.source == "canvas":
                course.name = row.name
                course.term = row.term
                course.start_at = row.start_at
                course.end_at = row.end_at
            course.term_id = term.term_id
            course.term_name = term.display_name
            course.term_start_at = term.start_at
            course.term_end_at = term.end_at
            course.term_sort_key = term.sort_key
            course.display_course_code = display.course_code
            course.display_name = display.name
            course.section = display.section
            source.url = f"{credential.base_url}/courses/{row.external_id}"
            source.metadata_json = row.metadata
            source.config_json = source.config_json or {"baseline_complete": False}
            if source.enabled and course.lifecycle_state == "ACTIVE":
                pairs.append(
                    (
                        course,
                        CanvasAdapter(
                            credential.base_url,
                            max_concurrency=self.settings.canvas_max_concurrency,
                            credential=credential,
                            canvas_course_id=row.external_id,
                            client=self.auth.routed_client(db),
                            course_source_id=source.id,
                        ),
                    )
                )
        profile = db.scalar(
            select(CredentialProfile).where(
                CredentialProfile.credential_id == self.auth.settings.canvas_credential_id
            )
        )
        if profile:
            profile.metadata_json = {
                **(profile.metadata_json or {}),
                "discovered_courses": len(discovered),
                "monitored_courses": len(pairs),
            }
        db.commit()
        return pairs

    async def sync_canvas(self, db: Session) -> tuple[int, SyncState]:
        pairs = await self.discover_canvas(db)
        state = await self.run(db, pairs)
        return len(pairs), state

    async def reconcile_course(self, db: Session, course_id: int) -> SyncState:
        pairs = await self.default_adapters(db)
        return await self.run(
            db,
            [(course, adapter) for course, adapter in pairs if course.id == course_id],
        )

    async def run(
        self,
        db: Session,
        adapters: Iterable[tuple[Course, SourceAdapter]] | None = None,
        *,
        include_canvas: bool = True,
        include_websites: bool = True,
    ) -> SyncState:
        sync_state.state = "running"
        sync_state.started_at = utcnow()
        sync_state.finished_at = None
        sync_state.courses = 0
        sync_state.items_seen = 0
        sync_state.changes = 0
        sync_state.by_kind = {}
        sync_state.errors = []
        tasks_changed = False
        kind_counts: Counter[str] = Counter()
        try:
            # Explicit adapter callers also need the saved institution/profile identity.
            self.auth.ensure_profiles(db)
            pairs = (
                list(adapters)
                if adapters is not None
                else await self.default_adapters(
                    db,
                    include_canvas=include_canvas,
                    include_websites=include_websites,
                )
            )
            pairs = [
                (course, adapter)
                for course, adapter in pairs
                if course.lifecycle_state == "ACTIVE"
            ]
            sync_state.courses = len(pairs)
            if not pairs:
                sync_state.state = "degraded"
                sync_state.errors.append(
                    "No configured source with available credentials"
                )
                return sync_state
            for course, adapter in pairs:
                source = self._course_source(db, adapter)
                is_canvas = isinstance(adapter, CanvasAdapter)
                baseline = bool(
                    is_canvas
                    and source
                    and not (source.config_json or {}).get("baseline_complete")
                )
                seen: dict[str, set[str]] = {}
                course_rows: list[RawSourceItem] = []
                try:
                    course_rows = await adapter.fetch_items(course)
                    for raw in course_rows:
                        kind_counts[raw.item_type] += 1
                        seen.setdefault(raw.item_type, set()).add(raw.external_id)
                        if raw.fetch_status in {"auth_required", "auth_failed"}:
                            item = self._pending_source_item(
                                db,
                                course,
                                raw,
                                course_source_id=source.id if source else None,
                            )
                            sync_state.items_seen += 1
                            self._mark_pending(db, course, item, raw)
                            if raw.credential_id:
                                self.auth.mark_runtime_failure(
                                    db, raw.credential_id, raw.fetch_status
                                )
                            db.commit()
                            continue
                        event, item = self.store_observation(
                            db,
                            course,
                            raw,
                            course_source_id=source.id if source else None,
                            emit_initial_event=not baseline,
                        )
                        sync_state.items_seen += 1
                        sync_state.changes += int(event is not None)
                        self._mark_fetched(db, course, raw)
                        if raw.item_type == "assignment":
                            task = task_from_assignment(db, item, raw.structured)
                            tasks_changed = True
                            if event is not None or baseline:
                                enqueue_task_analysis(db, task, raw.normalized_text)
                        if raw.item_type == "syllabus":
                            try:
                                await self.ai.parse_policy(
                                    db, course, item, raw.normalized_text
                                )
                            except Exception:
                                logger.exception(
                                    "syllabus_ai_parse_failed",
                                    course=course.course_code,
                                )
                        if raw.is_file and raw.download_url:
                            file_event = await self._sync_file(
                                db,
                                course,
                                adapter,
                                item,
                                raw,
                                event,
                                baseline,
                            )
                            if file_event is not None:
                                sync_state.changes += 1
                                enqueue_change_analysis(db, file_event)
                        if event is not None:
                            enqueue_change_analysis(db, event)
                            # All change notifications require the persisted AI gate.
                            if not is_assignment_change_type(event.change_type):
                                try:
                                    await self.notifier.notify_change(
                                        db, event, raw.title
                                    )
                                except Exception:
                                    logger.exception(
                                        "notification_failed",
                                        change_event_id=event.id,
                                    )
                        db.commit()

                    if is_canvas:
                        self._apply_canvas_grading_policy(db, course, course_rows)
                        successful = (
                            adapter.successful_item_types - set(adapter.errors)
                        )
                        removed = self._reconcile_canvas(
                            db,
                            course,
                            adapter,
                            seen,
                            successful,
                            emit_events=not baseline,
                        )
                        sync_state.changes += removed
                        if source:
                            source.last_sync_at = utcnow()
                            source.last_reconciled_at = utcnow()
                            from app.services.source_health import classify_canvas_errors
                            source.state = classify_canvas_errors(adapter.errors, successful)
                            source.last_error = (
                                "; ".join(
                                    f"{kind}:{code}"
                                    for kind, code in sorted(adapter.errors.items())
                                )
                                or None
                            )
                            if successful:
                                source.last_success_at = utcnow()
                            source.config_json = {
                                **(source.config_json or {}),
                                "baseline_complete": True,
                            }
                            source.metadata_json = {
                                **(source.metadata_json or {}),
                                "resource_errors": [getattr(adapter, "error_details", {}).get(kind,
                                    {"resource": kind, "code": code, "http_status": None}) for kind, code in sorted(adapter.errors.items())],
                                "counts": dict(
                                    Counter(row.item_type for row in course_rows)
                                ),
                            }
                        for kind, code in sorted(adapter.errors.items()):
                            sync_state.errors.append(
                                f"{course.course_code}/canvas/{kind}: {code}"
                            )
                        error_codes = set(adapter.errors.values())
                        for auth_error in (
                            "auth_required",
                            "invalid_token",
                        ):
                            if auth_error in error_codes:
                                self.auth.mark_runtime_failure(
                                    db, self.auth.settings.canvas_credential_id, auth_error
                                )
                                break
                    elif source:
                        source.last_sync_at = utcnow()
                        source.last_success_at = utcnow()
                        provider_errors = getattr(adapter, "errors", {})
                        source.state = "DEGRADED" if provider_errors else "HEALTHY"
                        source.last_error = "; ".join(f"{key}:{value}" for key, value in provider_errors.items()) or None
                        if provider_errors:
                            sync_state.errors.append(f"{course.course_code}/{adapter.name}: partial provider response")
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    message = f"{course.course_code}/{adapter.name}: {type(exc).__name__}"
                    sync_state.errors.append(message)
                    if source:
                        source.state = "DEGRADED" if source.source_type in {"gradescope", "prairielearn"} else "ERROR"
                        source.last_sync_at = utcnow()
                        source.last_error = type(exc).__name__
                        db.commit()
                    logger.exception(
                        "source_sync_failed",
                        course=course.course_code,
                        source=adapter.name,
                    )
            sync_state.state = "degraded" if sync_state.errors else "healthy"
            sync_state.by_kind = dict(kind_counts)
            if tasks_changed:
                Planner(self.settings.availability_config).rebuild(db)
        finally:
            sync_state.finished_at = utcnow()
        return sync_state

    @staticmethod
    def _course_source(
        db: Session, adapter: SourceAdapter
    ) -> CourseSource | None:
        source_id = getattr(adapter, "course_source_id", None)
        return db.get(CourseSource, source_id) if source_id else None

    async def _sync_file(
        self,
        db: Session,
        course: Course,
        adapter: SourceAdapter,
        item: SourceItem,
        raw: RawSourceItem,
        metadata_event: ChangeEvent | None,
        baseline: bool,
    ) -> ChangeEvent | None:
        latest = db.scalar(
            select(DownloadedFile)
            .where(DownloadedFile.source_item_id == item.id)
            .order_by(DownloadedFile.version_number.desc())
            .limit(1)
        )
        if latest is not None and metadata_event is None:
            return None
        if isinstance(adapter, CanvasAdapter):
            fetched = await adapter.fetch_file_result(raw)
            content = fetched.content
            mime_type = fetched.content_type or raw.content_type
            http_status = fetched.status_code
            content_length = fetched.content_length
            etag = fetched.etag
            last_modified = fetched.last_modified
        else:
            content = await adapter.fetch_file(raw)
            mime_type = raw.content_type
            http_status = raw.http_status
            content_length = raw.content_length
            etag = raw.etag
            last_modified = raw.last_modified
        filename = (
            raw.structured.get("display_name")
            or raw.structured.get("filename")
            or raw.title
        )
        record_url = (
            raw.url
            if isinstance(adapter, CanvasAdapter) and raw.url
            else raw.download_url or raw.url or raw.external_id
        )
        result = self.downloader.save_bytes(
            db,
            course_code=course.course_code,
            course_id=course.id,
            source_item=item,
            url=record_url,
            content=content,
            filename=str(filename),
            classification_context=" ".join(
                str(value)
                for value in raw.structured.get("page_context_titles", [])
                if value
            ),
            mime_type=mime_type,
            etag=etag,
            last_modified=last_modified,
            http_status=http_status,
            expected_length=content_length,
        )
        if not result.record:
            return None
        if not result.changed:
            result.record.source_url = record_url
            result.record.original_filename = safe_filename(
                record_url,
                str(filename),
                mime_type,
            )
            return None
        if result.record.integrity_status == "INVALID_DOWNLOAD":
            if isinstance(adapter, CanvasAdapter) and metadata_event is not None:
                metadata_event.change_type = "CANVAS_FILE_INVALID"
                metadata_event.importance = "important"
                metadata_event.requires_action = True
                metadata_event.summary = (
                    "Rejected invalid Canvas file: "
                    f"{result.record.validation_error or 'validation failed'}."
                )
                return None
            event = ChangeEvent(
                source_item_id=item.id,
                change_type=(
                    "CANVAS_FILE_INVALID"
                    if isinstance(adapter, CanvasAdapter)
                    else "invalid_download"
                ),
                importance="important",
                requires_action=True,
                summary=(
                    "Rejected invalid course file: "
                    f"{result.record.validation_error or 'validation failed'}."
                ),
                semantic_key=stable_change_key(
                    item.id,
                    "CANVAS_FILE_INVALID"
                    if isinstance(adapter, CanvasAdapter)
                    else "invalid_download",
                    latest.sha256 if latest else None,
                    result.record.sha256,
                ),
            )
            db.add(event)
            db.flush()
            return event
        if result.record.version_number > 1:
            if isinstance(adapter, CanvasAdapter) and metadata_event is not None:
                # Preserve the historical persisted type for existing consumers;
                # user-facing APIs normalize this semantic to FILE_CHANGE.
                metadata_event.change_type = "CANVAS_FILE_UPDATED"
                metadata_event.summary = (
                    "Canvas file content changed; saved version "
                    f"{result.record.version_number}."
                )
                return None
            event = ChangeEvent(
                source_item_id=item.id,
                change_type="FILE_CHANGE",
                importance=(
                    "important"
                    if effective_file_type(result.record, self.settings.file_rules_config) in {"homework", "reading"}
                    else "minor"
                ),
                requires_action=effective_file_type(result.record, self.settings.file_rules_config) in {"homework", "reading"},
                summary=(
                    "File content changed; saved version "
                    f"{result.record.version_number}."
                ),
                semantic_key=stable_change_key(
                    item.id,
                    "FILE_CHANGE",
                    latest.sha256 if latest else None,
                    result.record.sha256,
                ),
            )
            db.add(event)
            db.flush()
            return event
        # First discovery belongs in Files, not in the student-facing Changes stream.
        return None

    @staticmethod
    def _apply_canvas_grading_policy(
        db: Session, course: Course, rows: list[RawSourceItem]
    ) -> None:
        groups = [
            row
            for row in rows
            if row.source_type == "canvas" and row.item_type == "assignment_group"
        ]
        if not groups:
            return
        policy = db.scalar(
            select(CoursePolicy).where(CoursePolicy.course_id == course.id)
        )
        if policy is None:
            policy = CoursePolicy(course_id=course.id)
            db.add(policy)
        categories = []
        for row in groups:
            rules = row.structured.get("rules") or {}
            categories.append(
                {
                    "id": row.structured.get("id"),
                    "name": row.title,
                    "weight": row.structured.get("group_weight"),
                    "drop_lowest": rules.get("drop_lowest"),
                    "drop_highest": rules.get("drop_highest"),
                    "never_drop": rules.get("never_drop") or [],
                }
            )
        policy.policy_json = {
            **(policy.policy_json or {}),
            "grading_categories": categories,
            "grading_authority": "canvas_structured",
        }
        policy.evidence_json = [
            "Canvas assignment-group metadata (authoritative structured source)"
        ]
        policy.confidence = 1.0

    def _reconcile_canvas(
        self,
        db: Session,
        course: Course,
        adapter: CanvasAdapter,
        seen: dict[str, set[str]],
        successful_item_types: set[str],
        *,
        emit_events: bool,
    ) -> int:
        events = 0
        rows = list(
            db.scalars(
                select(SourceItem).where(
                    SourceItem.course_id == course.id,
                    SourceItem.source_type == "canvas",
                    SourceItem.source_name == adapter.source_name,
                    SourceItem.item_type.in_(successful_item_types),
                )
            )
        )
        for item in rows:
            if item.external_id in seen.get(item.item_type, set()):
                continue
            if item.is_deleted:
                continue
            item.is_deleted = True
            if not emit_events:
                continue
            old_snapshot = db.scalar(
                select(SourceSnapshot)
                .where(SourceSnapshot.source_item_id == item.id)
                .order_by(SourceSnapshot.captured_at.desc())
                .limit(1)
            )
            change_type = {
                "file": "CANVAS_FILE_REMOVED",
                "assignment": "CANVAS_ASSIGNMENT_REMOVED",
                "page": "CANVAS_PAGE_REMOVED",
                "announcement": "CANVAS_ANNOUNCEMENT_REMOVED",
                "module": "CANVAS_MODULE_REMOVED",
                "module_item": "CANVAS_MODULE_ITEM_REMOVED",
            }.get(item.item_type, "CANVAS_CONTENT_REMOVED")
            semantic_key = stable_change_key(
                item.id,
                change_type,
                old_snapshot.content_hash if old_snapshot else None,
                "deleted",
            )
            if db.scalar(
                select(ChangeEvent.id).where(ChangeEvent.semantic_key == semantic_key)
            ) is not None:
                continue
            event = ChangeEvent(
                source_item_id=item.id,
                change_type=change_type,
                old_snapshot_id=old_snapshot.id if old_snapshot else None,
                importance="important" if item.item_type == "assignment" else "minor",
                requires_action=item.item_type == "assignment",
                summary="Canvas item is no longer present after full reconciliation.",
                semantic_key=semantic_key,
            )
            db.add(event)
            db.flush()
            enqueue_change_analysis(db, event)
            events += 1
        return events

    def _pending_source_item(
        self,
        db: Session,
        course: Course,
        raw: RawSourceItem,
        *,
        course_source_id: int | None = None,
    ) -> SourceItem:
        """Link auth failures to the pending queue without replacing good content."""
        item = db.scalar(
            select(SourceItem).where(
                SourceItem.course_id == course.id,
                SourceItem.source_type == raw.source_type,
                SourceItem.source_name == raw.source_name,
                SourceItem.external_id == raw.external_id,
            )
        )
        if item is not None:
            item.last_seen_at = utcnow()
            item.course_source_id = course_source_id or item.course_source_id
            return item
        item = SourceItem(
            course_id=course.id,
            course_source_id=course_source_id,
            source_type=raw.source_type,
            source_name=raw.source_name,
            external_id=raw.external_id,
            item_type=raw.item_type,
            title=raw.title,
            url=raw.url,
            source_created_at=raw.source_created_at,
            source_updated_at=raw.source_updated_at,
            current_hash=content_hash(_semantic_structured(raw), raw.normalized_text),
        )
        db.add(item)
        db.flush()
        return item

    def _mark_pending(
        self, db: Session, course: Course, item: SourceItem, raw: RawSourceItem
    ) -> PendingResource:
        pending = db.scalar(
            select(PendingResource).where(
                PendingResource.course_id == course.id,
                PendingResource.source_name == raw.source_name,
                PendingResource.url == raw.url,
            )
        )
        if pending is None:
            pending = PendingResource(
                course_id=course.id,
                source_name=raw.source_name,
                url=raw.url or raw.external_id,
                title=raw.title,
                item_type=raw.item_type,
                credential_id=raw.credential_id,
            )
            db.add(pending)
        pending.source_item_id = item.id
        pending.state = (
            "FAILED" if raw.fetch_status == "auth_failed" else "AUTH_REQUIRED"
        )
        pending.last_attempt_at = utcnow()
        pending.last_error_code = raw.fetch_status
        pending.metadata_json = raw.structured
        db.flush()
        return pending

    @staticmethod
    def _mark_fetched(db: Session, course: Course, raw: RawSourceItem) -> None:
        pending = db.scalar(
            select(PendingResource).where(
                PendingResource.course_id == course.id,
                PendingResource.source_name == raw.source_name,
                PendingResource.url == raw.url,
            )
        )
        if pending:
            pending.state = "FETCHED"
            pending.last_attempt_at = utcnow()
            pending.last_error_code = None

    async def retry_pending_resources(
        self, db: Session, credential_id: str
    ) -> int:
        pending_before = db.scalar(
            select(PendingResource.id)
            .where(
                PendingResource.credential_id == credential_id,
                PendingResource.state.in_(
                    ["DISCOVERED", "AUTH_REQUIRED", "FAILED"]
                ),
            )
            .limit(1)
        )
        if pending_before is None:
            return 0
        pairs = await self.default_adapters(db)
        matching = [
            (course, adapter)
            for course, adapter in pairs
            if isinstance(adapter, WebsiteAdapter)
            and any(
                rule.credential_id == credential_id
                for rule in adapter.auth_rules
            )
        ]
        if not matching:
            return 0
        await self.run(db, matching)
        return int(
            db.scalar(
                select(func.count())
                .select_from(PendingResource)
                .where(
                    PendingResource.credential_id == credential_id,
                    PendingResource.state == "FETCHED",
                )
            )
            or 0
        )

    def store_observation(
        self,
        db: Session,
        course: Course,
        raw: RawSourceItem,
        *,
        course_source_id: int | None = None,
        emit_initial_event: bool = True,
    ) -> tuple[ChangeEvent | None, SourceItem]:
        digest = content_hash(_semantic_structured(raw), raw.normalized_text)
        item = db.scalar(
            select(SourceItem).where(
                SourceItem.course_id == course.id,
                SourceItem.source_type == raw.source_type,
                SourceItem.source_name == raw.source_name,
                SourceItem.external_id == raw.external_id,
            )
        )
        old_snapshot = None
        if item:
            old_snapshot = db.scalar(
                select(SourceSnapshot)
                .where(SourceSnapshot.source_item_id == item.id)
                .order_by(SourceSnapshot.captured_at.desc())
                .limit(1)
            )
            if old_snapshot and raw.source_type == "canvas" and raw.item_type == "assignment":
                # A partial list/detail response is not authoritative removal.
                # Keep the last good field values and label their provenance when
                # another real change causes a new snapshot to be stored.
                previous = old_snapshot.structured_json or {}
                structured = dict(raw.structured)
                retained = []
                if structured.get("description_state") == "missing" and "description" in previous:
                    structured["description"] = previous["description"]
                    structured["description_state"] = "retained"
                    raw.normalized_text = old_snapshot.normalized_text
                    retained.append("description")
                if structured.get("due_at_state") == "missing":
                    for field in ("due_at", "due_date_local", "deadline_precision", "deadline_timezone", "source_deadline_text", "deadline_source_rank"):
                        if field in previous:
                            structured[field] = previous[field]
                            retained.append(field)
                    if "due_at" in retained:
                        structured["due_at_state"] = "retained"
                if retained:
                    structured["retained_fields"] = retained
                    raw.structured = structured
                    digest = content_hash(_semantic_structured(raw), raw.normalized_text)
            if old_snapshot and raw.source_type in {"gradescope", "prairielearn"}:
                previous = old_snapshot.structured_json or {}
                old_facts, facts = previous.get("provider_facts") or {}, dict(raw.structured.get("provider_facts") or {})
                retained = []
                for key, value in old_facts.items():
                    if facts.get(key) in (None, "unknown") and value not in (None, "unknown"):
                        facts[key] = value
                        retained.append(key)
                if retained:
                    raw.structured = {**raw.structured, "provider_facts": facts, "retained_fields": retained}
                    if "due_at" in retained:
                        raw.structured["due_at"] = previous.get("due_at")
                    if "submission_state" in retained:
                        raw.structured["submission"] = {"workflow_state": facts["submission_state"]}
                    digest = content_hash(_semantic_structured(raw), raw.normalized_text)
            item.last_seen_at = utcnow()
            item.title = raw.title
            item.url = raw.url
            item.source_updated_at = raw.source_updated_at
            item.is_deleted = False
            item.course_source_id = course_source_id or item.course_source_id
            if item.current_hash == digest:
                return None, item
            if old_snapshot is not None:
                upgraded_old_digest = content_hash(
                    _semantic_value(old_snapshot.structured_json, item.item_type),
                    old_snapshot.normalized_text,
                )
                if upgraded_old_digest == digest:
                    item.current_hash = digest
                    return None, item
        else:
            item = SourceItem(
                course_id=course.id,
                course_source_id=course_source_id,
                source_type=raw.source_type,
                source_name=raw.source_name,
                external_id=raw.external_id,
                item_type=raw.item_type,
                title=raw.title,
                url=raw.url,
                source_created_at=raw.source_created_at,
                source_updated_at=raw.source_updated_at,
                current_hash=digest,
            )
            db.add(item)
            db.flush()
        snapshot = SourceSnapshot(
            source_item_id=item.id,
            content_hash=digest,
            structured_json=raw.structured,
            normalized_text=raw.normalized_text,
        )
        db.add(snapshot)
        db.flush()
        item.current_hash = digest
        if old_snapshot is None and not emit_initial_event:
            return None, item
        old_data = old_snapshot.structured_json if old_snapshot else None
        change_type, importance, requires_action = classify_change(
            old_data, raw.structured
        )
        provider_summary = None
        if raw.source_type in {"gradescope", "prairielearn"}:
            from app.services.provider_changes import provider_change
            semantic = provider_change(old_data, raw.structured)
            if semantic is None:
                return None, item
            change_type, importance, requires_action, provider_summary = semantic
        if raw.source_type == "canvas" and (
            raw.source_name.startswith("canvas:")
            or (old_data is None and raw.item_type in {"assignment", "announcement"})
        ):
            change_type, importance, requires_action = self._canvas_change(
                raw, old_data, change_type, importance, requires_action
            )
        # Canvas file metadata is a download trigger, not a user-facing event.
        # Keep a provisional event so _sync_file can promote it only when the
        # downloaded SHA really changes; unchanged signed-URL churn is removed.
        provisional_canvas_file = (
            raw.source_type == "canvas"
            and raw.item_type == "file"
            and change_type == "CANVAS_FILE_METADATA_UPDATED"
        )
        if not should_emit_change(change_type, old_data, raw.structured) and not provisional_canvas_file:
            return None, item
        semantic_key = stable_change_key(
            item.id,
            change_type,
            old_snapshot.content_hash if old_snapshot else None,
            snapshot.content_hash,
        )
        existing_event = db.scalar(
            select(ChangeEvent).where(ChangeEvent.semantic_key == semantic_key)
        )
        if existing_event is not None:
            return None, item
        event = ChangeEvent(
            source_item_id=item.id,
            change_type=change_type,
            old_snapshot_id=old_snapshot.id if old_snapshot else None,
            new_snapshot_id=snapshot.id,
            importance=importance,
            requires_action=requires_action,
            summary=provider_summary or summarize_change(old_data, raw.structured),
            semantic_key=semantic_key,
        )
        db.add(event)
        db.flush()
        return event, item

    @staticmethod
    def _canvas_change(
        raw: RawSourceItem,
        old: dict[str, Any] | None,
        fallback_type: str,
        importance: str,
        requires_action: bool,
    ) -> tuple[str, str, bool]:
        if raw.item_type == "assignment":
            if old is None:
                return "CANVAS_ASSIGNMENT_CREATED", "important", True
            if old.get("due_at") != raw.structured.get("due_at"):
                return "CANVAS_DEADLINE_CHANGED", importance, True
            if old.get("published") is False and raw.structured.get("published") is True:
                return "CANVAS_ASSIGNMENT_PUBLISHED", "important", True
            return "CANVAS_ASSIGNMENT_UPDATED", "important", True
        if raw.item_type == "file":
            if old is None:
                return "CANVAS_FILE_ADDED", "minor", False
            if old.get("hidden") is True and raw.structured.get("hidden") is False:
                return "NEW_FILE", "important", False
            old_name = old.get("display_name") or old.get("filename")
            new_name = raw.structured.get("display_name") or raw.structured.get(
                "filename"
            )
            if old_name != new_name:
                return "CANVAS_FILE_RENAMED", "minor", False
            old_sha = old.get("sha256") or old.get("content_sha256")
            new_sha = raw.structured.get("sha256") or raw.structured.get("content_sha256")
            if old_sha and new_sha and old_sha != new_sha:
                return "FILE_CHANGE", "important", False
            return "CANVAS_FILE_METADATA_UPDATED", "minor", False
        prefix = {
            "page": "CANVAS_PAGE",
            "module": "CANVAS_MODULE",
            "module_item": "CANVAS_MODULE_ITEM",
            "announcement": "CANVAS_ANNOUNCEMENT",
            "syllabus": "CANVAS_SYLLABUS",
            "assignment_group": "CANVAS_GRADING_POLICY",
        }.get(raw.item_type)
        if prefix:
            return (
                f"{prefix}_{'CREATED' if old is None else 'UPDATED'}",
                "important" if raw.item_type in {"announcement", "syllabus"} else "minor",
                requires_action,
            )
        return f"CANVAS_{fallback_type.upper()}", importance, requires_action
