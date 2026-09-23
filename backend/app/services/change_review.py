"""Bounded normalized candidate context; Stage 1 never authorizes notifications."""
from app.db import SourceItem
from app.services.change_policy import event_snapshots, is_system_change_type, is_user_facing_event

ACADEMIC_FIELDS = frozenset({"title", "description", "body", "text", "due_at", "due_date_local",
    "points_possible", "published", "locked", "submission", "grade", "score", "content_hash",
    "sha256", "filename", "display_name", "assignment_resource_url", "provider_facts"})


def normalized_candidate(db, event):
    item = db.get(SourceItem, event.source_item_id)
    old, new = event_snapshots(db, event)
    before = old.structured_json if old else {}
    after = new.structured_json if new else {}
    fields = sorted(key for key in ACADEMIC_FIELDS if before.get(key) != after.get(key))
    # Only normalized academic scalar values cross the provider boundary. Nested
    # provider objects can contain auth/transport metadata and are not dumped.
    def values(data):
        return {key: str(data.get(key))[:800] for key in fields
                if data.get(key) is None or isinstance(data.get(key), (str, int, float, bool))}
    return {"course_id": item.course_id if item else None,
            "source_type": item.source_type if item else "unknown",
            "source_name": item.source_name if item else "unknown",
            "change_type": event.change_type, "title": (item.title if item else "")[:300],
            "fields": fields, "before": values(before), "after": values(after),
            "old_text": (old.normalized_text or "")[:1800] if old else "",
            "new_text": (new.normalized_text or "")[:1800] if new else ""}


def prefilter(db, event):
    context = normalized_candidate(db, event)
    if is_system_change_type(event.change_type) or not is_user_facing_event(db, event):
        return "DROP", "system_or_nonsemantic", context
    old, new = event_snapshots(db, event)
    if old and new and not context["fields"] and (old.normalized_text or "") == (new.normalized_text or ""):
        return "DROP", "empty_or_metadata_only_diff", context
    from app.services.change_filters import matching_rule
    matched = matching_rule(db, context, event.id)
    if matched:
        context["matched_rule"] = matched
        return "DROP", "learned_suppression_rule", context
    return "AI_REVIEW", "academic_candidate", context
