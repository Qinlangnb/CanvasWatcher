"""Provider-independent read model built on existing source snapshots and links."""

import re
from urllib.parse import urlsplit

from dateutil.parser import isoparse
from sqlalchemy import select

from app.db import SourceItem, SourceSnapshot, Task, TaskSourceLink


def weak_task_candidates(db, course_id, title, due_at):
    def normalized(value):
        value = re.sub(r"\b(?:hw|homework)\s*0*(\d+)\b", r"homework \1", value.lower())
        return re.sub(r"\W+", "", value)
    incoming = isoparse(due_at) if due_at else None
    result = []
    for task in db.scalars(select(Task).where(Task.course_id == course_id)):
        if normalized(task.title) != normalized(title):
            continue
        if incoming and task.due_at:
            from app.timezone import as_utc
            if abs((as_utc(incoming) - as_utc(task.due_at)).total_seconds()) > 86400:
                continue
        result.append(task)
    return result


def latest_structured(db, item_id):
    snapshot = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_item_id == item_id)
                         .order_by(SourceSnapshot.id.desc()).limit(1))
    return snapshot.structured_json if snapshot else {}


def task_provider_facts(db, task_id):
    items = db.scalars(select(SourceItem).join(TaskSourceLink, TaskSourceLink.source_item_id == SourceItem.id)
                      .where(TaskSourceLink.task_id == task_id).order_by(SourceItem.id)).all()
    result = []
    for item in items:
        structured = latest_structured(db, item.id)
        facts = structured.get("provider_facts")
        if item.source_type == "canvas":
            submission = structured.get("submission") or {}
            state = "graded" if submission.get("graded_at") or submission.get("grade") is not None else (
                "submitted" if submission.get("submitted_at") or submission.get("workflow_state") in {"submitted", "pending_review"}
                else submission.get("workflow_state") or "unknown")
            facts = {"provider": "canvas", "due_at": structured.get("due_at"), "submission_state": state,
                     "submitted_at": submission.get("submitted_at"), "direct_assignment_url": item.url}
        if isinstance(facts, dict):
            # Explicit allowlist: arbitrary provider HTML/private fields never enter AI or UI.
            allowed = {key: facts.get(key) for key in ("provider", "provider_course_id", "provider_assignment_id",
                "due_at", "late_due_at", "personal_due_at", "submission_state", "submitted_at",
                "score", "max_score", "grade_published", "direct_assignment_url", "evidence")}
            result.append({**allowed, "source_item_id": item.id, "source_url": item.url, "is_deleted": item.is_deleted})
    return result


def terminal_state(facts):
    states = {row.get("submission_state") for row in facts
              if row.get("provider") in {"canvas", "gradescope"} and not row.get("is_deleted")}
    # PrairieLearn progress/full credit is not proof of submission or completion.
    return "graded" if "graded" in states else "submitted" if "submitted" in states else None


def _gradescope_identity(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(r"/courses/(\d+)/assignments/(\d+)(?:/submissions/\d+)?/?", parsed.path)
    return (parsed.netloc, match[1], match[2]) if match else None


def strongly_bound_task(db, item, structured):
    """Only explicit same-origin provider IDs count; titles and due dates do not."""
    if item.source_type not in {"canvas", "gradescope"}:
        return None
    identity = _gradescope_identity((structured.get("external_tool_tag_attributes") or {}).get("url")) if item.source_type == "canvas" else None
    own = structured.get("provider_facts") or {}
    if item.source_type == "gradescope":
        base = urlsplit(item.url or "")
        if own.get("provider_course_id") and own.get("provider_assignment_id"):
            identity = (base.netloc, str(own["provider_course_id"]), str(own["provider_assignment_id"]))
    if not identity:
        return None
    opposite = "gradescope" if item.source_type == "canvas" else "canvas"
    candidates = set()
    for other, link in db.execute(select(SourceItem, TaskSourceLink).join(TaskSourceLink,
            TaskSourceLink.source_item_id == SourceItem.id).where(SourceItem.course_id == item.course_id,
                SourceItem.source_type == opposite, SourceItem.is_deleted.is_(False))):
        data = latest_structured(db, other.id)
        if opposite == "canvas":
            other_identity = _gradescope_identity((data.get("external_tool_tag_attributes") or {}).get("url"))
        else:
            facts = data.get("provider_facts") or {}
            other_identity = (urlsplit(other.url or "").netloc, str(facts.get("provider_course_id")), str(facts.get("provider_assignment_id")))
        if other_identity == identity:
            candidates.add(link.task_id)
    return db.get(Task, candidates.pop()) if len(candidates) == 1 else None
