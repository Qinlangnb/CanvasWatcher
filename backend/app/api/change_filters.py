from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import ChangeAnalysis, ChangeFilterAudit, ChangeFilterRule, get_db
from app.services.change_filters import set_rule_enabled

router = APIRouter(prefix="/api/settings/change-filters", tags=["change-filter-diagnostics"])


class ToggleRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


@router.get("")
def filters(db: Session = Depends(get_db)):
    rows = db.scalars(select(ChangeFilterRule).order_by(ChangeFilterRule.id.desc()).limit(100)).all()
    recent = db.scalars(select(ChangeAnalysis).order_by(ChangeAnalysis.id.desc()).limit(30)).all()
    return {"rules": [{"id": r.id, "enabled": r.enabled, "removed": r.removed, "rule": r.rule_json,
        "reason": r.reason, "created_by": r.created_by, "created_at": r.created_at, "updated_at": r.updated_at,
        "version": r.version, "match_count": r.match_count, "last_matched_at": r.last_matched_at,
        "audit": [{"actor": a.actor, "action": a.action, "reason": a.reason, "version": a.version,
                   "created_at": a.created_at} for a in db.scalars(select(ChangeFilterAudit)
                   .where(ChangeFilterAudit.rule_id == r.id).order_by(ChangeFilterAudit.id.desc()).limit(10))]} for r in rows],
        "recent_reviews": [{"change_id": a.change_event_id, "status": a.analysis_json.get("review_status", a.analysis_status),
             "stage1": a.analysis_json.get("stage1"), "reason": a.reason or a.analysis_json.get("stage1_reason"),
             "matched_rule": a.analysis_json.get("matched_rule"), "error": a.analysis_json.get("error")}
             for a in recent if a.analysis_json.get("review_version") == "0.7.1"]}


@router.patch("/{rule_id}")
def toggle(rule_id: int, payload: ToggleRule, db: Session = Depends(get_db)):
    try:
        row = set_rule_enabled(db, rule_id, payload.enabled)
    except LookupError:
        raise HTTPException(404, "Filter not found") from None
    db.commit()
    return {"id": row.id, "enabled": row.enabled, "version": row.version}


@router.delete("/{rule_id}")
def remove(rule_id: int, db: Session = Depends(get_db)):
    try:
        row = set_rule_enabled(db, rule_id, False, remove=True)
    except LookupError:
        raise HTTPException(404, "Filter not found") from None
    db.commit()
    return {"id": row.id, "removed": True}
