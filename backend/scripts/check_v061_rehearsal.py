"""Validate a disposable migration/repair copy, never the source database."""
import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.db import PlanBlock, Task
from app.services.planner import Planner

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("copy", type=Path)
args = parser.parse_args()
target = args.copy.resolve(strict=True)
if not target.name.startswith("v061-final-rehearsal-"):
    raise SystemExit("Only explicitly named final rehearsal copies are accepted")
engine = create_engine(f"sqlite:///{target.as_posix()}")
with Session(engine) as db:
    repaired = list(db.scalars(select(Task).where(Task.id.in_([1, 91]))))
    assert len(repaired) == 2 and all(task.local_completed for task in repaired)
    plan = Planner(Path("../config/availability.yaml")).rebuild(db)
    db.commit()
    assert not list(db.scalars(select(PlanBlock).where(
        PlanBlock.plan_id == plan.id, PlanBlock.task_id.in_([1, 91])
    )))
    counts = {name: db.scalar(text(f'SELECT count(*) FROM "{name}"')) for name in (
        "courses", "tasks", "source_items", "source_snapshots", "downloaded_files",
        "change_events", "calendar_events", "task_progress", "task_work_sessions",
    )}
    integrity = db.scalar(text("PRAGMA integrity_check"))
    violations = db.execute(text("PRAGMA foreign_key_check")).all()
    assert integrity == "ok" and not violations
    print(json.dumps({"plan_id": plan.id, "repaired_task_ids": [1, 91],
        "excluded_from_plan": True, "counts": counts, "integrity": integrity,
        "foreign_key_violations": len(violations)}, indent=2))
