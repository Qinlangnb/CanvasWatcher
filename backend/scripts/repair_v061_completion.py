"""Read-only legacy completion audit by default; no schema change or secret loading.

Run from backend: python -m scripts.repair_v061_completion /absolute/database.db
To apply, stop writers, back up the DB and explicitly pass --apply --task-id IDs
matching the reviewed candidate list. Rebuild the active plan once afterward.
"""

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.completion_repair import apply_completion_repair, completion_repair_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--task-id", type=int, nargs="*", default=[])
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    mode = "rw" if args.apply else "ro"
    engine = create_engine(f"sqlite:///{path.as_uri()}?mode={mode}&uri=true")
    with Session(engine) as db:
        plan = completion_repair_plan(db)
        print(json.dumps(plan, indent=2))
        if args.apply:
            if set(args.task_id) != {row["task_id"] for row in plan["candidates"]}:
                raise SystemExit("Explicit task IDs must match the entire reviewed dry-run list")
            changed = apply_completion_repair(db, plan)
            db.commit()
            print(json.dumps({"repaired": changed, "plan_rebuild_required": bool(changed)}))


if __name__ == "__main__":
    main()
