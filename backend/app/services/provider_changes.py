"""Semantic changes only; layout/session/countdown text never enters these facts."""

from app.services.diff import _moved_earlier


def provider_change(old, new):
    prefix = str((new.get("provider_facts") or {}).get("provider", "provider")).upper()
    if old is None:
        return f"{prefix}_ASSIGNMENT_CREATED", "important", True, "New provider assignment detected."
    before, after = old.get("provider_facts") or {}, new.get("provider_facts") or {}
    for key, label in (("personal_due_at", "Personal deadline"), ("due_at", "Deadline"), ("late_due_at", "Late deadline")):
        if after.get(key) is not None and before.get(key) != after[key]:
            severity = "critical" if _moved_earlier(before.get(key), after[key]) else "important"
            return f"{prefix}_DEADLINE_CHANGED", severity, True, f"{label} changed from {before.get(key) or 'unknown'} to {after[key]}."
    if after.get("submission_state") not in {None, "unknown"} and before.get("submission_state") != after["submission_state"]:
        return f"{prefix}_ASSIGNMENT_SUBMISSION_CHANGED", "minor", False, f"Provider submission state changed to {after['submission_state']}."
    if any(after.get(key) is not None and after.get(key) != before.get(key) for key in ("score", "max_score", "grade_published")):
        return f"{prefix}_ASSIGNMENT_GRADE_CHANGED", "important", False, "Provider grade information changed."
    if old.get("title") != new.get("title"):
        return f"{prefix}_ASSIGNMENT_TITLE_CHANGED", "minor", False, "Provider assignment title changed."
    if after.get("release_at") is not None and before.get("release_at") != after["release_at"]:
        return f"{prefix}_ASSIGNMENT_ACCESS_CHANGED", "minor", False, "Provider release information changed."
    return None
