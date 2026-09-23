import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import ChangeAnalysis, ChangeEvent, Course, Notification, SourceItem, utcnow
from app.services.change_policy import (
    analysis_should_notify_user,
    is_user_facing_event,
)


class NtfyNotifier:
    def __init__(self, base_url: str, topic: str):
        self.base_url = base_url.rstrip("/")
        self.topic = topic

    async def notify_change(
        self, db: Session, event: ChangeEvent, title: str
    ) -> Notification | None:
        if not is_user_facing_event(db, event):
            return None
        lifecycle = db.scalar(
            select(Course.lifecycle_state)
            .join(SourceItem, SourceItem.course_id == Course.id)
            .where(SourceItem.id == event.source_item_id)
        )
        if lifecycle != "ACTIVE":
            return None
        analysis = db.scalar(select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event.id))
        if not analysis_should_notify_user(event, analysis):
            return None
        key = hashlib.sha256(f"change:{event.id}:{event.change_type}".encode()).hexdigest()
        existing = db.scalar(select(Notification).where(Notification.dedupe_key == key))
        if existing:
            return existing
        # notify_user is the AI's homepage decision. Banner levels are a
        # presentation contract, not another hidden importance threshold.
        level = "critical" if analysis.severity == "critical" else "important"
        notification = Notification(change_event_id=event.id, level=level, title=title, body=analysis.reason, dedupe_key=key)
        db.add(notification)
        db.flush()
        # Approval and local visibility survive external delivery failures.
        body = notification.body.encode()
        db.commit()
        if self.base_url and self.topic:
            notification.status = "failed"
            for _ in range(2):
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        response = await client.post(f"{self.base_url}/{self.topic}", content=body, headers={"Title": title.encode("utf-8"), "Priority": "urgent" if analysis.severity == "critical" else "default"})
                        response.raise_for_status()
                    notification.sent_at = utcnow()
                    notification.status = "sent"
                    break
                except (httpx.HTTPError, UnicodeError, ValueError):
                    pass  # Bounded delivery failure is not an AI review failure.
        else:
            notification.status = "disabled"
        db.commit()
        return notification
