from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.services.change_filters import FilterMutation
from app.timezone import as_utc


def _validated_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Unknown timezone; use an IANA timezone such as America/Chicago") from error


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _utc_text(value: datetime | None) -> str | None:
    return as_utc(value).isoformat().replace("+00:00", "Z") if value else None


class CourseOut(ORMModel):
    id: int
    course_code: str
    name: str
    term: str | None
    active: bool
    lifecycle_state: str = "ACTIVE"
    archived_at: datetime | None = None
    term_id: str | None = None
    term_name: str | None = None
    term_start_at: datetime | None = None
    term_end_at: datetime | None = None
    term_sort_key: str = "0000-0"
    display_course_code: str | None = None
    display_name: str | None = None
    section: str | None = None
    commute_minutes: int = 0

    @field_serializer("archived_at", "term_start_at", "term_end_at")
    def serialize_course_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class CourseTermOut(BaseModel):
    term_id: str
    display_name: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    sort_key: str
    active_course_count: int = 0
    archived_course_count: int = 0
    is_default: bool = False

    @field_serializer("start_at", "end_at")
    def serialize_term_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class CourseSourceOut(ORMModel):
    health_summary: str | None = None
    health_diagnostics: list[dict] = Field(default_factory=list)
    id: int
    course_id: int
    name: str
    source_type: str
    external_id: str | None
    url: str | None
    enabled: bool
    state: str
    metadata_json: dict[str, Any]
    last_sync_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None

    @field_serializer("last_sync_at", "last_success_at")
    def serialize_source_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class SourceEnabledIn(BaseModel):
    enabled: bool


class TaskOut(ORMModel):
    source_url: str | None = None
    source_link_kind: str = "unavailable"
    local_completed: bool = False
    local_completed_at: datetime | None = None
    completion_origin: str | None = None
    state_revision: int = 0
    submission_state: str | None = None
    id: int
    course_id: int
    title: str
    task_type: str
    due_at: datetime | None
    assigned_date_local: date | None
    due_date_local: date | None
    deadline_precision: str | None
    deadline_timezone: str | None
    source_deadline_text: str | None
    points_possible: float | None
    status: str
    manual_time_adjustment_minutes: int = 0
    ignored_at: datetime | None = None

    @field_serializer("due_at", "ignored_at", "local_completed_at")
    def serialize_due_at(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class ProgressIn(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    action_at: datetime | None = None
    reopen: bool = False
    status: Literal[
        "NOT_STARTED", "IN_PROGRESS", "BLOCKED", "READY_TO_SUBMIT", "SUBMITTED", "GRADED", "CANCELLED"
    ]
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    spent_minutes_delta: int | None = Field(default=None, ge=0)
    remaining_minutes_estimate: int | None = Field(default=None, ge=0)
    note: str | None = None


class ChangeOut(ORMModel):
    id: int
    source_item_id: int
    change_type: str
    detected_at: datetime
    importance: str
    requires_action: bool
    summary: str
    ai_summary: str | None
    read_at: datetime | None = None
    category: str = "system"
    change_field: str | None = None
    course_code: str | None = None
    title: str | None = None
    primary_text: str | None = None
    event_label: str | None = None
    has_meaningful_diff: bool = False

    @field_serializer("detected_at", "read_at")
    def serialize_change_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class DownloadedFileOut(ORMModel):
    id: int
    course_id: int
    source_url: str
    original_filename: str
    local_path: str
    category: str
    source_detected_type: str | None = None
    user_override_type: str | None = None
    effective_type: str = "other"
    size_bytes: int
    sha256: str
    downloaded_at: datetime
    version_number: int
    course_code: str
    course_name: str
    mime_type: str | None
    integrity_status: str
    validation_error: str | None
    export_name: str | None
    export_path: str | None
    export_status: str
    export_error: str | None
    source_deleted: bool = False

    @field_serializer("downloaded_at")
    def serialize_downloaded_at(self, value: datetime) -> str:
        return _utc_text(value) or ""


class GradingCategory(BaseModel):
    name: str
    weight: float | None = None
    drop_lowest: int | None = None
    drop_highest: int | None = None


class LatePolicy(BaseModel):
    allowed: bool | None = None
    penalty_description: str | None = None
    max_late_duration: str | None = None


class ImportantAssessment(BaseModel):
    name: str
    category: str | None = None
    date: datetime | None = None
    weight: float | None = None


class CoursePolicyData(BaseModel):
    course_id: str
    grading_categories: list[GradingCategory] = Field(default_factory=list)
    late_policy: LatePolicy | None = None
    attendance_required: bool | None = None
    important_assessments: list[ImportantAssessment] = Field(default_factory=list)
    project_dependencies: list[str] = Field(default_factory=list)
    policies_text_summary: str
    evidence: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class EffortEstimate(BaseModel):
    hours: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)


class ImportanceFactors(BaseModel):
    grade_impact: float = Field(ge=0, le=1)
    urgency: float = Field(ge=0, le=1)
    dependency: float = Field(ge=0, le=1)
    academic_importance: float = Field(ge=0, le=1)
    failure_risk: float = Field(ge=0, le=1)


class TaskAnalysisData(BaseModel):
    task_id: str
    classification: str
    importance: ImportanceFactors
    estimated_effort: EffortEstimate
    recommended_start_before_hours: float = Field(ge=0)
    suggested_session_minutes: list[int]
    rationale: str
    evidence: list[str]
    confidence: float = Field(ge=0, le=1)


class EffortWorkStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1, max_length=80)
    minutes: int = Field(gt=0, le=14400)


class EffortAnalysisData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_minutes: int = Field(gt=0, le=14400)
    low_minutes: int = Field(gt=0, le=14400)
    high_minutes: int = Field(gt=0, le=14400)
    confidence: float = Field(ge=0, le=1)
    complexity: Literal["low", "medium", "high"]
    work_breakdown: list[EffortWorkStage] = Field(min_length=1, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("high_minutes")
    @classmethod
    def validate_range(cls, value: int, info):
        low = info.data.get("low_minutes")
        total = info.data.get("total_minutes")
        if low is not None and total is not None and not low <= total <= value:
            raise ValueError("effort range must satisfy low <= total <= high")
        return value


class ChangeReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    notify_user: bool
    importance: Literal["low", "medium", "high", "critical"]
    reason: str = Field(min_length=1, max_length=500)
    rule_feedback: FilterMutation | None = None


class ChangeAnalysisData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    importance: float = Field(ge=0, le=1)
    severity: Literal["low", "medium", "high", "critical"]
    requires_action: bool
    should_notify_user: bool = False
    summary: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)
    recommended_action: str | None = Field(default=None, max_length=1000)
    affected_task_ids: list[int] = Field(default_factory=list)
    replan_required: bool
    confidence: float = Field(ge=0, le=1)


class TodayWorkOut(BaseModel):
    provider_facts: list[dict[str, Any]] = Field(default_factory=list)
    submission_state: str | None = None
    state_revision: int = 0
    local_completed: bool = False
    local_completed_at: datetime | None = None
    task_id: int
    course_id: int
    course_code: str
    title: str
    source_url: str | None = None
    source_link_kind: Literal["assignment", "course_page", "unavailable"] = "unavailable"
    deadline: datetime | None
    deadline_precision: str | None
    due_date_local: date | None = None
    progress: float
    effective_total_minutes: int
    remaining_minutes: int
    estimate_source: Literal["observed_ratio", "ai_calibrated", "ai", "fallback"]
    estimate_confidence: float
    analysis_status: Literal["PENDING", "READY", "FAILED"]
    priority_score: float
    priority_bucket: Literal["Low", "Medium", "High", "Critical"]
    today_reason: str
    slack_minutes: int | None
    slack_bucket: str
    tracked_minutes: int = 0
    manual_adjustment_minutes: int = 0
    effective_used_minutes: int = 0
    is_working: bool = False
    active_started_at: datetime | None = None
    active_elapsed_seconds: int = 0

    @field_serializer("deadline", "active_started_at")
    def serialize_deadline(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class TodayAttentionOut(BaseModel):
    ai_reviewed: bool = False
    review_reason: str | None = None
    reviewed_at: str | None = None
    change_id: int
    course_id: int
    course_code: str
    summary: str
    severity: str
    importance: float
    requires_action: bool
    recommended_action: str | None
    affected_task_ids: list[int]
    detected_at: datetime
    analysis_status: Literal["PENDING", "READY", "FAILED"]

    @field_serializer("detected_at")
    def serialize_detected_at(self, value: datetime) -> str:
        return _utc_text(value) or ""


class TodayOut(BaseModel):
    completed: list[TodayWorkOut] = Field(default_factory=list)
    date: date
    timezone: str
    work: list[TodayWorkOut]
    attention: list[TodayAttentionOut]
    capacity_minutes: int | None = None
    capacity_source: Literal["calendar", "availability", "fallback"] = "fallback"


class RawSourceItem(BaseModel):
    source_type: str
    source_name: str
    external_id: str
    item_type: str
    title: str
    url: str | None = None
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    structured: dict[str, Any] = Field(default_factory=dict)
    normalized_text: str = ""
    is_file: bool = False
    download_url: str | None = None
    fetch_status: str = "ok"
    credential_id: str | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    http_status: int | None = None
    content_length: int | None = None


class DiscoveredCourse(BaseModel):
    source: str
    external_id: str
    course_code: str
    name: str
    term: str | None = None
    term_id: str | None = None
    term_start_at: datetime | None = None
    term_end_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NtlmCredentialIn(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr


def _canvas_base_url(value: str) -> str:
    from urllib.parse import urlsplit

    normalized = value.strip().rstrip("/")
    parts = urlsplit(normalized)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("Canvas base URL must be an absolute HTTPS URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Canvas base URL cannot contain credentials, query, or fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("Canvas base URL must be an origin without a path")
    return normalized


class CanvasCredentialIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    base_url: str = Field(min_length=8, max_length=512)
    token: SecretStr
    expiration_date: date | None = None

    _validate_base_url = field_validator("base_url")(_canvas_base_url)


class CanvasBrowserSessionIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    base_url: str = Field(min_length=8, max_length=512)
    session_import: SecretStr

    _validate_base_url = field_validator("base_url")(_canvas_base_url)


class CredentialProfileOut(ORMModel):
    credential_id: str
    auth_type: str
    auth_mode: str | None = None
    expires_at: datetime | None = None
    days_remaining: int | None = None
    expiration_state: str = "unknown"
    expiration_source: str | None = None
    username: str | None
    display_name: str | None
    probe_url: str
    metadata_json: dict[str, Any]
    state: str
    last_verified_at: datetime | None
    last_failure_at: datetime | None
    last_error_code: str | None

    @field_serializer("probe_url")
    def serialize_probe(self, value: str) -> str:
        from app.services.source_connections import stored_probe_url

        return (stored_probe_url(value) or "") if self.auth_type == "ntlm" else value

    @field_serializer("last_verified_at", "last_failure_at", "expires_at")
    def serialize_auth_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class CredentialVerificationOut(CredentialProfileOut):
    verified: bool
    pending_retried: int = 0
    discovered_courses: int = 0
    items_seen: int = 0
    changes: int = 0
    sync_errors: list[str] = Field(default_factory=list)


class CanvasAuthStatusOut(BaseModel):
    configured: bool
    auth_mode: str | None = None
    credential_state: str
    connection_state: str = "AUTH_REQUIRED"
    preferred_method: str = "pat"
    effective_method: str | None = None
    backup_ready: bool = False
    credentials: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fallback: dict[str, Any] | None = None
    source_health: str = "UNKNOWN"
    verified: bool = False
    verified_at: datetime | None = None
    account_id: str | None = None
    account_display_name: str | None = None
    expires_at: datetime | None = None
    days_remaining: int | None = None
    expiration_state: str = "unknown"
    expiration_source: str | None = None

    @field_serializer("verified_at", "expires_at")
    def serialize_canvas_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class FileClassificationIn(BaseModel):
    type: Literal[
        "lecture", "homework", "discussion", "reading", "exam", "solution", "other"
    ]


class TaskWorkSessionOut(ORMModel):
    id: int
    task_id: int
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    source: str

    @field_serializer("started_at", "ended_at")
    def serialize_work_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class TaskWorkSummaryOut(BaseModel):
    state_revision: int = 0
    local_completed: bool = False
    task_id: int
    tracked_minutes: int
    manual_adjustment_minutes: int
    effective_used_minutes: int
    active_session_id: int | None
    active_started_at: datetime | None
    active_elapsed_seconds: int
    sessions: list[TaskWorkSessionOut] = Field(default_factory=list)
    switched_from_task_id: int | None = None

    @field_serializer("active_started_at")
    def serialize_active_time(self, value: datetime | None) -> str | None:
        return _utc_text(value)


class TimeUsedIn(BaseModel):
    total_minutes: int = Field(ge=0, le=1_000_000)


class WorkHeartbeatIn(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    elapsed_snapshot_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    manual_adjustment_minutes: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    remaining_minutes: int | None = Field(default=None, ge=0, le=1_000_000)
    client_timestamp: datetime


class CourseCommuteIn(BaseModel):
    commute_minutes: int = Field(ge=0, le=240)


EventType = Literal["class", "busy", "personal", "study", "other"]


class CalendarEventIn(BaseModel):
    summary: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=5000)
    location: str | None = Field(default=None, max_length=512)
    start_at: datetime
    end_at: datetime
    all_day: bool = False
    timezone: str = Field(default="America/Chicago", min_length=1, max_length=80)
    recurrence_rule: str | None = Field(default=None, max_length=512)
    event_type: EventType = "other"
    course_id: int | None = None

    @model_validator(mode="after")
    def validate_event(self):
        if self.end_at <= self.start_at:
            raise ValueError("Event end must be after start")
        timezone = _validated_timezone(self.timezone)
        if self.recurrence_rule:
            from app.services.calendar_recurrence import recurrence
            from app.timezone import as_utc

            recurrence(self.recurrence_rule, as_utc(self.start_at).astimezone(timezone))
        return self


class CalendarEventOut(ORMModel):
    id: int
    calendar_id: str
    external_id: str | None
    source: str
    summary: str
    description: str
    location: str | None
    start_at: datetime
    end_at: datetime
    all_day: bool
    timezone: str
    recurrence_rule: str | None
    recurring_event_id: str | None
    status: str
    event_type: str
    course_id: int | None
    import_source_id: int | None = None
    original_start_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    transparency: str = "opaque"
    read_only: bool = False

    @field_serializer("start_at", "end_at", "original_start_at")
    def serialize_event_time(self, value: datetime) -> str:
        return _utc_text(value) or ""


class AvailabilityInterval(BaseModel):
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def validate_order(self):
        if self.end <= self.start:
            raise ValueError("Availability end must be after start")
        return self


class AvailabilityRuleIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_local_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_local_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    enabled: bool = True

    @model_validator(mode="after")
    def validate_order(self):
        if self.end_local_time <= self.start_local_time:
            raise ValueError("Availability end must be after start")
        return self


class AvailabilityRuleOut(AvailabilityRuleIn):
    id: int


class AvailabilityOverrideIn(BaseModel):
    date: date
    available_intervals: list[AvailabilityInterval] = Field(default_factory=list)


class AvailabilityOverrideOut(AvailabilityOverrideIn):
    id: int


class GeneralSettingsIn(BaseModel):
    academic_timezone: str = Field(default="America/Chicago", min_length=1, max_length=80)

    @field_validator("academic_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        _validated_timezone(value)
        return value


class AIProbeSettingsIn(BaseModel):
    provider: str | None = Field(default=None, max_length=32)
    base_url: str = Field(default="", max_length=512)
    model_id: str = Field(default="", max_length=255)


class AIProbeCredentialIn(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    api_key: SecretStr
    provider: str = Field(min_length=1, max_length=32)


class SourceConnectionOut(ORMModel):
    id: int
    source_type: str
    name: str
    base_url: str | None
    course_id: int | None
    credential_id: str | None
    config_json: dict[str, Any]
    state: str

    @field_serializer("config_json")
    def serialize_config(self, value: dict) -> dict:
        from copy import deepcopy

        from app.services.source_connections import stored_probe_url

        result = deepcopy(value)
        if self.source_type == "website":
            if "probe_url" in result:
                result["probe_url"] = stored_probe_url(result["probe_url"])
            for rule in result.get("auth_rules", []):
                auth = rule.get("auth") or {}
                if auth.get("type") == "ntlm" and "probe_url" in auth:
                    auth["probe_url"] = stored_probe_url(auth["probe_url"])
        return result


class SourceConnectionUpdateIn(BaseModel):
    ntlm: NtlmCredentialIn | None = Field(default=None, exclude=True, repr=False)
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=8, max_length=512)
    course_name: str | None = Field(default=None, max_length=255)
    course_code: str | None = Field(default=None, max_length=80)
    term: str | None = Field(default=None, max_length=120)
    authentication_method: Literal["none", "ntlm"] = "none"
    protected_path_prefix: str | None = Field(default=None, max_length=512)
    probe_url: str | None = Field(default=None, max_length=512)
    default_auth_method: Literal["pat", "browser_session"] | None = None
    discovery_path_limit: int = Field(default=100, ge=1, le=10_000)


class GoogleCalendarSelectionIn(BaseModel):
    calendar_ids: list[str] = Field(default_factory=list, max_length=500)


class CalendarAIRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class AIChatMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class AIChatPageContext(BaseModel):
    route: str = Field(default="/today", max_length=512)
    course_id: int | None = None


class AIChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=36)
    messages: list[AIChatMessageIn] = Field(min_length=1, max_length=50)
    page_context: AIChatPageContext = Field(default_factory=AIChatPageContext)

    @model_validator(mode="after")
    def require_user_message(self):
        if self.messages[-1].role != "user" or not self.messages[-1].content.strip():
            raise ValueError("The final chat message must be a non-empty user message")
        return self


class AIChatConfirmationIn(BaseModel):
    confirmation_id: str = Field(min_length=1, max_length=36)
    action: Literal["confirm", "cancel"]


class CalendarMutationEvent(BaseModel):
    summary: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=5000)
    location: str | None = Field(default=None, max_length=512)
    start: datetime
    end: datetime
    timezone: str = Field(default="America/Chicago", max_length=80)
    event_type: EventType = "other"
    course_id: int | None = None

    @model_validator(mode="after")
    def validate_times(self):
        if self.end <= self.start:
            raise ValueError("Event end must be after start")
        _validated_timezone(self.timezone)
        return self


class CreateManualEventOperation(BaseModel):
    operation: Literal["create_manual_event"]
    event: CalendarMutationEvent


class UpdateManualEventOperation(BaseModel):
    operation: Literal["update_manual_event"]
    event_id: int
    event: CalendarMutationEvent


class DeleteManualEventOperation(BaseModel):
    operation: Literal["delete_manual_event"]
    event_id: int


class SetWeeklyAvailabilityOperation(BaseModel):
    operation: Literal["set_weekly_availability"]
    weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    intervals: list[AvailabilityInterval] = Field(default_factory=list, max_length=24)


class SetDateOverrideOperation(BaseModel):
    operation: Literal["set_date_override"]
    date: date
    intervals: list[AvailabilityInterval] = Field(default_factory=list, max_length=24)


class RemoveDateOverrideOperation(BaseModel):
    operation: Literal["remove_date_override"]
    date: date


class SetCourseCommuteOperation(BaseModel):
    operation: Literal["set_course_commute"]
    course_id: int
    minutes: int = Field(ge=0, le=240)


CalendarMutationOperation = (
    CreateManualEventOperation
    | UpdateManualEventOperation
    | DeleteManualEventOperation
    | SetWeeklyAvailabilityOperation
    | SetDateOverrideOperation
    | RemoveDateOverrideOperation
    | SetCourseCommuteOperation
)


class CalendarMutationPlan(BaseModel):
    type: Literal["calendar_mutation_plan"]
    summary: str = Field(min_length=1, max_length=1000)
    operations: list[CalendarMutationOperation] = Field(min_length=1, max_length=20)
    requires_confirmation: Literal[True]


class CalendarMutationExecuteIn(BaseModel):
    plan: CalendarMutationPlan


class CanvasSourceCreateIn(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    base_url: str = Field(min_length=8, max_length=512)

    _validate_base_url = field_validator("base_url")(_canvas_base_url)


class WebsiteSourceCreateIn(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    ntlm: NtlmCredentialIn | None = Field(default=None, exclude=True, repr=False)
    course_name: str = Field(min_length=1, max_length=255)
    course_code: str | None = Field(default=None, max_length=80)
    term: str | None = Field(default=None, max_length=120)
    base_url: str = Field(min_length=8, max_length=512)
    authentication_method: Literal["none", "ntlm"] = "none"
    protected_path_prefix: str | None = Field(default=None, max_length=512)
    probe_url: str | None = Field(default=None, max_length=512)
