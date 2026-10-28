"""Exact Canvas assignment-to-downloaded-file associations, without title guessing."""

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import DownloadedFile, SourceItem, SourceSnapshot, Task, TaskSourceLink


def task_attachments(db: Session, task_ids: list[int]) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {task_id: [] for task_id in task_ids}
    if not task_ids:
        return result
    latest = select(
        SourceSnapshot.source_item_id, func.max(SourceSnapshot.id).label("latest_id")
    ).group_by(SourceSnapshot.source_item_id).subquery()
    bindings = db.execute(
        select(TaskSourceLink.task_id, SourceItem, SourceSnapshot)
        .join(TaskSourceLink, TaskSourceLink.source_item_id == SourceItem.id)
        .join(Task, Task.id == TaskSourceLink.task_id)
        .join(latest, latest.c.source_item_id == SourceItem.id)
        .join(SourceSnapshot, SourceSnapshot.id == latest.c.latest_id)
        .where(
            TaskSourceLink.task_id.in_(task_ids), Task.course_id == SourceItem.course_id,
            SourceItem.source_type == "canvas", SourceItem.item_type == "assignment",
            SourceItem.is_deleted.is_(False),
        )
    ).all()
    requested: dict[tuple, set[int]] = {}
    for task_id, item, snapshot in bindings:
        ids = snapshot.structured_json.get("attachment_file_ids")
        if not isinstance(ids, list):
            continue
        for file_id in ids:
            if not isinstance(file_id, str) or not file_id.isascii() or not file_id.isdigit():
                continue
            key = (item.course_id, item.course_source_id, item.source_name, f"file:{file_id}")
            requested.setdefault(key, set()).add(task_id)
    if not requested:
        return result
    rows = db.execute(
        select(SourceItem, DownloadedFile)
        .join(DownloadedFile, DownloadedFile.source_item_id == SourceItem.id)
        .where(
            SourceItem.course_id.in_({key[0] for key in requested}),
            SourceItem.source_type == "canvas", SourceItem.item_type == "file",
            SourceItem.is_deleted.is_(False), DownloadedFile.course_id == SourceItem.course_id,
        )
        .order_by(DownloadedFile.version_number.desc(), DownloadedFile.id.desc())
    ).all()
    seen: set[tuple] = set()
    for item, file in rows:
        key = (item.course_id, item.course_source_id, item.source_name, item.external_id)
        if key not in requested or key in seen:
            continue
        seen.add(key)
        # Never offer an older version if the current download is invalid/missing.
        if file.integrity_status != "VALID" or not file.local_path or not Path(file.local_path).is_file():
            continue
        attachment = {"file_id": file.id, "filename": file.original_filename}
        for task_id in requested[key]:
            result[task_id].append(attachment)
    for attachments in result.values():
        attachments.sort(key=lambda value: (value["filename"].casefold(), value["file_id"]))
    return result
