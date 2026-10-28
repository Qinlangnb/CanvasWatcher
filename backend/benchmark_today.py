"""Generate synthetic before/after evidence; never open production data or call AI."""
import argparse
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter

parser = argparse.ArgumentParser()
parser.add_argument("--backend", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--compare")
args = parser.parse_args()
sys.path.insert(0, str(Path(args.backend).resolve()))
sys.path.insert(1, str(Path(__file__).parent / "tests"))

from performance_fixture import NOW, seed_performance_data  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base  # noqa: E402
from app.services.assignment_summaries import enqueue_visible_summaries  # noqa: E402
from app.services.today import TodayEngine  # noqa: E402

results = {}
for count in (1, 30, 120):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_performance_data(db, count)
    for operation in ("today", "summary_discovery"):
        # Freeze Today time for the original discovery API too.
        original_build = TodayEngine.build
        if operation == "summary_discovery":
            TodayEngine.build = lambda self, db, now=None, _build=original_build, **kw: _build(self, db, NOW, **kw)
        samples, reads = [], []
        output = None
        for _ in range(3):
            queries = []
            def record(conn, cursor, statement, parameters, context, executemany, _queries=queries):
                if statement.lstrip().upper().startswith("SELECT"):
                    _queries.append(statement)
            event.listen(engine, "before_cursor_execute", record)
            with Session(engine) as db:
                started = perf_counter()
                if operation == "today":
                    output = TodayEngine().build(db, NOW)
                else:
                    enqueue_visible_summaries(db)
                samples.append((perf_counter() - started) * 1000)
            event.remove(engine, "before_cursor_execute", record)
            reads.append(len(queries))
        TodayEngine.build = original_build
        key = f"{operation}:{count}"
        results[key] = {"queries": reads[-1], "median_ms": round(statistics.median(samples), 2),
                        "result": json.loads(json.dumps(output, default=str))}
        print(json.dumps({"case": key, "queries": reads[-1], "median_ms": results[key]["median_ms"]}))
    engine.dispose()
if args.compare:
    before = json.loads(Path(args.compare).read_text(encoding="utf-8"))
    for key, value in results.items():
        assert value["result"] == before[key]["result"], f"Behavior changed for {key}"
    print("baseline_output_equivalence=passed")
Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
