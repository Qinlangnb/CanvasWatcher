from typing import Any

from sqlalchemy.orm import Session

from app.db import ChangeAnalysis, ChangeEvent, SourceSnapshot

SYSTEM_MARKERS = (
    "SYSTEM",
    "AUTH",
    "TOKEN",
    "CREDENTIAL",
    "FETCH_STATUS",
    "SOURCE_HEALTH",
)

GENERIC_DISCOVERY_TYPES = {
    "CREATED",
    "FILE_ADDED",
    "NEW_FILE",
    "NEW_CONTENT",
    "SOURCE_ITEM_CREATED",
    "CANVAS_FILE_ADDED",
    "CANVAS_PAGE_CREATED",
    "CANVAS_MODULE_CREATED",
    "CANVAS_MODULE_ITEM_CREATED",
    "CANVAS_CONTENT_CREATED",
}

GENERIC_REMOVAL_TYPES = {
    "DELETED",
    "FILE_REMOVED",
    "CONTENT_REMOVED",
    "SOURCE_ITEM_REMOVED",
    "CANVAS_FILE_REMOVED",
    "CANVAS_PAGE_REMOVED",
    "CANVAS_MODULE_REMOVED",
    "CANVAS_MODULE_ITEM_REMOVED",
    "CANVAS_CONTENT_REMOVED",
}


def meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(meaningful_value(child) for child in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(meaningful_value(child) for child in value)
    # Zero and False are meaningful normalized values for points and publish state.
    return True


def is_system_change_type(change_type: str) -> bool:
    value = change_type.strip().upper()
    return value == "SYSTEM" or any(marker in value for marker in SYSTEM_MARKERS)


def is_assignment_change_type(change_type: str) -> bool:
    value = change_type.upper()
    return "ASSIGNMENT" in value or "DEADLINE" in value or "POINT" in value


def is_semantic_creation(change_type: str) -> bool:
    value = change_type.upper()
    domain = "ASSIGNMENT" in value or "ANNOUNCEMENT" in value
    return domain and any(marker in value for marker in ("CREATED", "PUBLISHED"))


def is_semantic_removal(change_type: str) -> bool:
    value = change_type.upper()
    domain = "ASSIGNMENT" in value or "ANNOUNCEMENT" in value
    return domain and any(marker in value for marker in ("REMOVED", "UNPUBLISHED"))


def is_generic_discovery(change_type: str) -> bool:
    value = change_type.strip().upper()
    return value in GENERIC_DISCOVERY_TYPES


def is_generic_removal(change_type: str) -> bool:
    return change_type.strip().upper() in GENERIC_REMOVAL_TYPES


def is_intrinsically_meaningful(change_type: str) -> bool:
    value = change_type.upper()
    if is_semantic_creation(value) or is_semantic_removal(value):
        return True
    if any(marker in value for marker in ("DEADLINE", "POINT", "UNPUBLISHED")):
        return True
    return "FILE" in value and any(
        marker in value for marker in ("UPDATED", "CHANGED", "INVALID", "RENAMED")
    )


def should_emit_change(
    change_type: str,
    old_value: Any,
    new_value: Any,
) -> bool:
    if new_value is None:
        return False
    if isinstance(new_value, dict) and new_value.get("hidden") is True:
        return False
    if (
        change_type.strip().upper() == "NEW_FILE"
        and isinstance(old_value, dict)
        and old_value.get("hidden") is True
        and isinstance(new_value, dict)
        and new_value.get("hidden") is False
    ):
        return True
    if "FILE_METADATA" in change_type.strip().upper():
        return False
    if is_system_change_type(change_type):
        return False
    if is_semantic_creation(change_type) or is_semantic_removal(change_type):
        return True
    if is_generic_discovery(change_type) or is_generic_removal(change_type):
        return False
    if is_intrinsically_meaningful(change_type):
        return True
    return meaningful_value(old_value) and meaningful_value(new_value)


def event_snapshots(
    db: Session, event: ChangeEvent
) -> tuple[SourceSnapshot | None, SourceSnapshot | None]:
    old = db.get(SourceSnapshot, event.old_snapshot_id) if event.old_snapshot_id else None
    new = db.get(SourceSnapshot, event.new_snapshot_id) if event.new_snapshot_id else None
    return old, new


def is_user_facing_event(db: Session, event: ChangeEvent) -> bool:
    if event.old_snapshot_id is None and event.new_snapshot_id is None:
        # Preserve legacy/synthetic records that predate snapshot links. New
        # synchronization events always carry normalized snapshots and therefore
        # still receive the V0.5.4 hidden/null filtering below.
        return not is_system_change_type(event.change_type)
    old, new = event_snapshots(db, event)
    return should_emit_change(
        event.change_type,
        old.structured_json if old else None,
        new.structured_json if new else None,
    )


def analysis_should_notify_user(
    event: ChangeEvent, analysis: ChangeAnalysis | None
) -> bool:
    if analysis is None:
        return False
    payload = analysis.analysis_json or {}
    return analysis.analysis_status == "READY" and payload.get("review_version") == "0.7.1" and payload.get("notify_user") is True
