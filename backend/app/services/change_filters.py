"""Declarative, course/source scoped suppression; never executable expressions."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.db import ChangeAnalysis, ChangeFilterAudit, ChangeFilterRule, utcnow


class FilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["SUPPRESS_FROM_AI_REVIEW"] = "SUPPRESS_FROM_AI_REVIEW"
    course_id: int = Field(gt=0)
    source_type: str = Field(min_length=1, max_length=40)
    source_name: str = Field(min_length=1, max_length=200)
    change_type: str = Field(min_length=1, max_length=80)
    field: Literal["title", "description", "body", "text", "filename", "display_name"]
    operator: Literal["equals", "contains", "prefix", "suffix"]
    pattern: str = Field(min_length=3, max_length=160)


class FilterMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["create", "update", "disable"]
    rule_id: int | None = Field(default=None, gt=0)
    rule: FilterSpec | None = None
    reason: str = Field(min_length=1, max_length=500)


def matches(spec, context):
    if any(getattr(spec, key) != context.get(key) for key in ("course_id", "source_type", "source_name", "change_type")):
        return False
    # A title noise rule must not suppress a simultaneous deadline/grade change.
    if context.get("fields") != [spec.field]:
        return False
    value = str(context.get("after", {}).get(spec.field, "")).strip().casefold()
    pattern = spec.pattern.strip().casefold()
    if len(pattern) < 3:
        return False
    return {"equals": lambda: value == pattern, "contains": lambda: pattern in value,
            "prefix": lambda: value.startswith(pattern), "suffix": lambda: value.endswith(pattern)}[spec.operator]()


def family(spec):
    value = spec.model_dump(exclude={"operator", "pattern"})
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def audit(db, row, actor, action, reason, before):
    db.add(ChangeFilterAudit(rule_id=row.id, version=row.version, actor=actor, action=action, reason=reason,
        before_json=before, after_json={"rule": row.rule_json, "enabled": row.enabled, "removed": row.removed}))


def set_rule_enabled(db, rule_id, enabled, *, actor="USER", remove=False, reason="User changed filter"):
    row = db.get(ChangeFilterRule, rule_id)
    if row is None:
        raise LookupError("Filter not found")
    before = {"rule": row.rule_json, "enabled": row.enabled, "removed": row.removed}
    row.enabled = bool(enabled and not remove)
    row.removed = bool(remove)
    row.version += 1
    audit(db, row, actor, "remove" if remove else "enable" if enabled else "disable", reason, before)
    return row


def mutate_from_review(db, payload, context):
    mutation = FilterMutation.model_validate(payload)
    row = db.get(ChangeFilterRule, mutation.rule_id) if mutation.rule_id else None
    if mutation.operation in {"update", "disable"} and row is None:
        raise ValueError("existing_rule_required")
    if row and (row.created_by != "AI" or row.removed or not row.enabled):
        raise ValueError("user_controlled_rule")
    if mutation.operation == "disable":
        spec = FilterSpec.model_validate(row.rule_json)
        if any(getattr(spec, key) != context.get(key) for key in ("course_id", "source_type", "source_name")):
            raise ValueError("rule_scope_mismatch")
        return set_rule_enabled(db, row.id, False, actor="AI", reason=mutation.reason)
    spec = mutation.rule
    if spec is None or not matches(spec, context):
        raise ValueError("rule_must_match_current_candidate")
    if row and family(FilterSpec.model_validate(row.rule_json)) != family(spec):
        raise ValueError("rule_scope_cannot_expand")
    rejected = db.scalars(select(ChangeAnalysis).where(ChangeAnalysis.analysis_status == "READY")
        .order_by(ChangeAnalysis.id.desc()).limit(200)).all()
    evidence = [a for a in rejected if (a.analysis_json or {}).get("review_version") == "0.7.1"
        and a.analysis_json.get("notify_user") is False and matches(spec, a.analysis_json.get("context", {}))]
    if len(evidence) < 3:
        raise ValueError("three_rejected_candidates_required")
    key = family(spec)
    existing = db.scalar(select(ChangeFilterRule).where(ChangeFilterRule.identity_key == key))
    if existing:
        if existing.removed or not existing.enabled or existing.created_by != "AI":
            raise ValueError("user_controlled_rule")
        row = existing
    before = {"rule": row.rule_json, "enabled": row.enabled, "removed": row.removed} if row else {}
    if row is None:
        if db.scalar(select(func.count()).select_from(ChangeFilterRule)) >= 100:
            raise ValueError("rule_capacity_reached")
        row = ChangeFilterRule(identity_key=key, rule_json=spec.model_dump(), reason=mutation.reason,
            created_by="AI", enabled=True, removed=False, version=1, match_count=0)
        db.add(row)
        db.flush()
    else:
        row.rule_json = spec.model_dump()
        row.reason = mutation.reason
        row.version += 1
    audit(db, row, "AI", "update" if before else "create", mutation.reason, before)
    return row


def matching_rule(db, context, event_id):
    for row in db.scalars(select(ChangeFilterRule).where(ChangeFilterRule.enabled.is_(True),
            ChangeFilterRule.removed.is_(False)).order_by(ChangeFilterRule.id).limit(100)):
        spec = FilterSpec.model_validate(row.rule_json)
        if matches(spec, context):
            previous = db.scalar(select(ChangeAnalysis).where(ChangeAnalysis.change_event_id == event_id))
            marker = {"id": row.id, "version": row.version, "created_by": row.created_by, "reason": row.reason}
            if not previous or (previous.analysis_json or {}).get("matched_rule") != marker:
                row.match_count += 1
                row.last_matched_at = utcnow()
            return marker
    return None
