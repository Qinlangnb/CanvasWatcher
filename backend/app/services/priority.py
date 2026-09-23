import math
from datetime import UTC, datetime

DEFAULT_WEIGHTS = {
    "grade_impact": 0.35,
    "urgency": 0.30,
    "dependency": 0.15,
    "failure_risk": 0.10,
    "academic_importance": 0.10,
}


def urgency_score(due_at: datetime | None, now: datetime | None = None) -> float:
    if due_at is None:
        return 0.15
    now = now or datetime.now(UTC)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    hours = (due_at - now).total_seconds() / 3600
    if hours <= 0:
        return 1.0
    return max(0.0, min(1.0, math.exp(-hours / 72)))


def calculate_priority(factors: dict[str, float], weights: dict[str, float] | None = None) -> float:
    weights = weights or DEFAULT_WEIGHTS
    return round(sum(weights[key] * max(0.0, min(1.0, factors.get(key, 0.0))) for key in weights), 4)

