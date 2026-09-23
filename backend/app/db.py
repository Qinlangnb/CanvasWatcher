from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    literal_column,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Course(TimestampMixin, Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    course_code: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(255))
    term: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_state: Mapped[str] = mapped_column(
        String(16), default="ACTIVE", index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    term_id: Mapped[str | None] = mapped_column(String(255), index=True)
    term_name: Mapped[str | None] = mapped_column(String(120))
    term_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    term_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    term_sort_key: Mapped[str] = mapped_column(String(160), default="0000-0")
    display_course_code: Mapped[str | None] = mapped_column(String(120))
    display_name: Mapped[str | None] = mapped_column(String(512))
    section: Mapped[str | None] = mapped_column(String(255))
    commute_minutes: Mapped[int] = mapped_column(Integer, default=0)


class CourseSource(TimestampMixin, Base):
    __tablename__ = "course_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    source_type: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    url: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(24), default="http")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="healthy")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class CredentialProfile(TimestampMixin, Base):
    __tablename__ = "credential_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    auth_type: Mapped[str] = mapped_column(String(24))
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    probe_url: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="AUTH_REQUIRED", index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))

    @property
    def auth_mode(self) -> str | None:
        """Expose the non-secret Canvas mode without adding a database column."""
        value = (self.metadata_json or {}).get("auth_mode")
        return str(value) if value else None

    @property
    def expires_at(self) -> datetime | None:
        from app.services.canvas_lifecycle import parse_expiration

        return parse_expiration((self.metadata_json or {}).get("expires_at"))

    @property
    def days_remaining(self) -> int | None:
        from app.services.canvas_lifecycle import expiration_status

        return expiration_status((self.metadata_json or {}).get("expires_at")).days_remaining

    @property
    def expiration_state(self) -> str:
        from app.services.canvas_lifecycle import expiration_status

        return expiration_status((self.metadata_json or {}).get("expires_at")).state.value

    @property
    def expiration_source(self) -> str | None:
        value = (self.metadata_json or {}).get("expiration_source")
        return str(value) if value else None


class CoursePolicy(TimestampMixin, Base):
    __tablename__ = "course_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), unique=True, index=True)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_item_id: Mapped[int | None] = mapped_column(ForeignKey("source_items.id"))


class SourceItem(Base):
    __tablename__ = "source_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    course_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("course_sources.id"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_name: Mapped[str] = mapped_column(String(120))
    external_id: Mapped[str] = mapped_column(String(512))
    item_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    current_hash: Mapped[str] = mapped_column(String(64))
    snapshots: Mapped[list["SourceSnapshot"]] = relationship(
        back_populates="source_item", cascade="all, delete-orphan"
    )


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("source_items.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    structured_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    local_blob_path: Mapped[str | None] = mapped_column(Text)
    source_item: Mapped[SourceItem] = relationship(back_populates="snapshots")


class PendingResource(Base):
    __tablename__ = "pending_resources"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    source_item_id: Mapped[int | None] = mapped_column(ForeignKey("source_items.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(512))
    item_type: Mapped[str] = mapped_column(String(40))
    credential_id: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[str] = mapped_column(String(24), default="DISCOVERED", index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ChangeEvent(Base):
    __tablename__ = "change_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("source_items.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(40), index=True)
    old_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshots.id"))
    new_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshots.id"))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    importance: Mapped[str] = mapped_column(String(16), default="minor")
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    semantic_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)


class ChangeAnalysis(TimestampMixin, Base):
    __tablename__ = "change_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    change_event_id: Mapped[int] = mapped_column(
        ForeignKey("change_events.id"), unique=True, index=True
    )
    analysis_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="low")
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str | None] = mapped_column(Text)
    affected_task_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    replan_required: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)


class ChangeFilterRule(TimestampMixin, Base):
    __tablename__ = "change_filter_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    last_matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangeFilterAudit(Base):
    __tablename__ = "change_filter_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("change_filter_rules.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500))
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DownloadedFile(Base):
    __tablename__ = "downloaded_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("source_items.id"), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(String(512))
    local_path: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="other")
    source_detected_type: Mapped[str | None] = mapped_column(String(40))
    user_override_type: Mapped[str | None] = mapped_column(String(40), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(255))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    integrity_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    validation_error: Mapped[str | None] = mapped_column(Text)
    export_name: Mapped[str | None] = mapped_column(String(512))
    export_path: Mapped[str | None] = mapped_column(Text)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    export_status: Mapped[str] = mapped_column(String(32), default="NOT_CONFIGURED")
    export_error: Mapped[str | None] = mapped_column(Text)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("course_id", "source_key", name="uq_task_course_source_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    task_type: Mapped[str] = mapped_column(String(40), default="assignment")
    description: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    assigned_date_local: Mapped[date | None] = mapped_column(Date)
    due_date_local: Mapped[date | None] = mapped_column(Date, index=True)
    deadline_precision: Mapped[str | None] = mapped_column(String(24))
    deadline_timezone: Mapped[str | None] = mapped_column(String(80))
    source_deadline_text: Mapped[str | None] = mapped_column(Text)
    deadline_source_rank: Mapped[int | None] = mapped_column(Integer)
    source_key: Mapped[str | None] = mapped_column(String(160), index=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    points_possible: Mapped[float | None] = mapped_column(Float)
    grading_category: Mapped[str | None] = mapped_column(String(120))
    submission_state: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="NOT_STARTED", index=True)
    manual_time_adjustment_minutes: Mapped[int] = mapped_column(Integer, default=0)
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_client_remaining_minutes: Mapped[int | None] = mapped_column(Integer)
    last_client_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    local_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_origin: Mapped[str | None] = mapped_column(String(32))
    state_revision: Mapped[int] = mapped_column(Integer, default=0)


class TaskWorkSession(Base):
    __tablename__ = "task_work_sessions"
    __table_args__ = (
        Index(
            "uq_task_work_sessions_one_open",
            literal_column("(1)"),
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(24), default="timer")


class CalendarEvent(TimestampMixin, Base):
    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint(
            "source", "calendar_id", "external_id", name="uq_calendar_event_external_identity"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[str] = mapped_column(String(120), default="manual", index=True)
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", index=True)
    summary: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(String(512))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(80), default="America/Chicago")
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    recurring_event_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(24), default="confirmed", index=True)
    event_type: Mapped[str] = mapped_column(String(24), default="other", index=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), index=True)
    import_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("ics_calendar_import_sources.id", ondelete="CASCADE"), index=True
    )
    original_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    transparency: Mapped[str] = mapped_column(String(16), default="opaque")
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GoogleCalendarConnection(TimestampMixin, Base):
    __tablename__ = "google_calendar_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(24), default="DISCONNECTED", index=True)
    connected_account: Mapped[str | None] = mapped_column(String(320))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(Text)
    oauth_state_hash: Mapped[str | None] = mapped_column(String(64))
    oauth_state_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GoogleCalendarSelection(TimestampMixin, Base):
    __tablename__ = "google_calendar_selections"
    __table_args__ = (
        UniqueConstraint("connection_id", "calendar_id", name="uq_google_calendar_selection"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("google_calendar_connections.id", ondelete="CASCADE"), index=True
    )
    calendar_id: Mapped[str] = mapped_column(String(512), index=True)
    summary: Mapped[str] = mapped_column(String(512))
    primary: Mapped[bool] = mapped_column(Boolean, default=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    timezone: Mapped[str | None] = mapped_column(String(80))
    access_role: Mapped[str | None] = mapped_column(String(40))
    sync_token: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="READY")


class ICSCalendarImportSource(TimestampMixin, Base):
    __tablename__ = "ics_calendar_import_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    calendar_name: Mapped[str] = mapped_column(String(512))
    calendar_timezone: Mapped[str | None] = mapped_column(String(80))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_reimported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    recurring_series_count: Mapped[int] = mapped_column(Integer, default=0)
    date_range_start: Mapped[date | None] = mapped_column(Date)
    date_range_end: Mapped[date | None] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(24), default="IMPORTED", index=True)


class StudyAvailabilityRule(TimestampMixin, Base):
    __tablename__ = "study_availability_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer, index=True)
    start_local_time: Mapped[str] = mapped_column(String(5))
    end_local_time: Mapped[str] = mapped_column(String(5))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class StudyAvailabilityOverride(TimestampMixin, Base):
    __tablename__ = "study_availability_overrides"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    available_intervals: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(24), default="message")
    tool_name: Mapped[str | None] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIToolConfirmation(Base):
    __tablename__ = "ai_tool_confirmations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SourceConnection(TimestampMixin, Base):
    __tablename__ = "source_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    external_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    base_url: Mapped[str | None] = mapped_column(Text)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), index=True)
    credential_id: Mapped[str | None] = mapped_column(String(120), index=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="READY", index=True)


class TaskSourceLink(Base):
    __tablename__ = "task_source_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("source_items.id"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(40), default="primary")


class TaskAnalysis(TimestampMixin, Base):
    __tablename__ = "task_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), unique=True, index=True)
    classification: Mapped[str] = mapped_column(String(80))
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_effort_hours: Mapped[float] = mapped_column(Float, default=1.0)
    remaining_effort_hours: Mapped[float] = mapped_column(Float, default=1.0)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    analysis_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    ai_estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    calibration_factor: Mapped[float] = mapped_column(Float, default=1.0)
    effective_estimated_minutes: Mapped[int | None] = mapped_column(Integer)


class AnalysisJob(TimestampMixin, Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        UniqueConstraint("kind", "entity_id", "input_hash", name="uq_analysis_job_identity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class TaskProgress(Base):
    __tablename__ = "task_progress"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(24))
    progress_percent: Mapped[float | None] = mapped_column(Float)
    spent_minutes_delta: Mapped[int | None] = mapped_column(Integer)
    remaining_minutes_estimate: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class Deadline(Base):
    __tablename__ = "deadlines"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("source_items.id"), index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date_local: Mapped[date | None] = mapped_column(Date)
    deadline_precision: Mapped[str | None] = mapped_column(String(24))
    deadline_timezone: Mapped[str | None] = mapped_column(String(80))
    source_deadline_text: Mapped[str | None] = mapped_column(Text)
    source_rank: Mapped[int | None] = mapped_column(Integer)
    authoritative: Mapped[bool] = mapped_column(Boolean, default=False)
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="active")


class PlanBlock(Base):
    __tablename__ = "plan_blocks"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="planned")


class AIRun(Base):
    __tablename__ = "ai_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(160))
    input_hash: Mapped[str] = mapped_column(String(64))
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    change_event_id: Mapped[int | None] = mapped_column(ForeignKey("change_events.id"))
    level: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


settings = get_settings()
if settings.database_url.startswith("sqlite:////"):
    db_path = Path(make_url(settings.database_url).database or "")
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
