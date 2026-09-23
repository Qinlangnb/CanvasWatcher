"""Resolve stored, user-facing task links without manufacturing upstream URLs."""

from urllib.parse import unquote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SourceItem, SourceSnapshot, TaskSourceLink


def safe_task_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if any(ord(char) < 32 for char in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
        _ = parts.port
    except ValueError:
        return None
    path = unquote(parts.path).lower()
    # Stored URLs with query/fragment credentials or expiring signatures are not
    # safe navigation targets. Prefer another stored stable source URL instead.
    if parts.query or parts.fragment or path.startswith("/api/"):
        return None
    if any(part in path.split("/") for part in ("download", "preview")):
        return None
    return value


def task_source_link(db: Session, task_id: int) -> dict:
    return task_source_links(db, [task_id])[task_id]


def task_source_links(db: Session, task_ids: list[int]) -> dict[int, dict]:
    if not task_ids:
        return {}
    latest = select(SourceSnapshot.source_item_id, func.max(SourceSnapshot.id).label("latest_id")).group_by(SourceSnapshot.source_item_id).subquery()
    rows = db.execute(
        select(TaskSourceLink.task_id, SourceItem, SourceSnapshot)
        .join(TaskSourceLink, TaskSourceLink.source_item_id == SourceItem.id)
        .outerjoin(latest, latest.c.source_item_id == SourceItem.id)
        .outerjoin(SourceSnapshot, SourceSnapshot.id == latest.c.latest_id)
        .where(TaskSourceLink.task_id.in_(task_ids), SourceItem.is_deleted.is_(False))
        .order_by(TaskSourceLink.task_id, SourceItem.id)
    ).all()
    grouped: dict[int, list] = {task_id: [] for task_id in task_ids}
    for task_id, item, snapshot in rows:
        grouped[task_id].append((item, snapshot))
    return {task_id: _resolve(rows) for task_id, rows in grouped.items()}


def _resolve(rows) -> dict:
    candidates: list[tuple[int, str, str]] = []
    for item, snapshot in rows:
        data = snapshot.structured_json if snapshot else {}
        facts = data.get("provider_facts") or {}
        # These links belong to already-bound source items, never fuzzy matches.
        if item.source_type in {"gradescope", "prairielearn"} and isinstance(facts, dict):
            execution = safe_task_url(facts.get("direct_assignment_url"))
            if execution and item.item_type in {"assignment", "assessment"}:
                candidates.append((-1, execution, "assignment"))
        if item.source_type == "website":
            resource = safe_task_url(data.get("assignment_resource_url"))
            if resource:
                candidates.append((1, resource, "assignment"))
        for value in (data.get("html_url"), item.url):
            url = safe_task_url(value)
            if url:
                exact = item.item_type == "assignment" and url != safe_task_url(data.get("schedule_url"))
                rank = 0 if exact and item.source_type == "canvas" else 1 if exact else 2
                candidates.append((rank, url, "assignment" if exact else "course_page"))
    if not candidates:
        return {"source_url": None, "source_link_kind": "unavailable"}
    _, url, kind = min(candidates, key=lambda candidate: candidate[0])
    return {"source_url": url, "source_link_kind": kind}
