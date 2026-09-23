from datetime import datetime
from typing import Any


def _deadline(data: dict[str, Any]) -> Any:
    return (
        data.get("due_at")
        or data.get("due_date_local")
        or data.get("all_day_date")
        or data.get("end_at")
    )


def classify_change(old: dict[str, Any] | None, new: dict[str, Any] | None) -> tuple[str, str, bool]:
    if old is None:
        return "created", "important" if new and _deadline(new) else "minor", bool(new and _deadline(new))
    if new is None:
        return "deleted", "important", True
    if _deadline(old) != _deadline(new):
        old_due, new_due = _deadline(old), _deadline(new)
        severity = "critical" if _moved_earlier(old_due, new_due) else "important"
        return "deadline_changed", severity, True
    if old.get("published") is False and new.get("published") is True:
        return "published", "important", True
    if old.get("published") is True and new.get("published") is False:
        return "unpublished", "important", True
    return "updated", "important", False


def summarize_change(old: dict[str, Any] | None, new: dict[str, Any] | None) -> str:
    if old is None:
        return "New source item detected."
    if new is None:
        return "Source item is no longer present."
    changed = sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))
    visible = [key for key in changed if key not in {"html_url", "url", "updated_at"}]
    return "Changed fields: " + ", ".join((visible or changed)[:12])


def _moved_earlier(old_value: Any, new_value: Any) -> bool:
    try:
        return bool(old_value and new_value and datetime.fromisoformat(str(new_value).replace("Z", "+00:00")) < datetime.fromisoformat(str(old_value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return False
