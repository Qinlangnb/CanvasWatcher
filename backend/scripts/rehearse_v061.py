"""Back up read-only SQLite input, upgrade only the new copy, compare all counts."""
import argparse
import json
import os
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
target = args.destination.resolve()
if target.exists() or target == args.source.resolve():
    raise SystemExit("Destination must be a new isolated database")
target.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True) as original:
    with sqlite3.connect(target) as copy:
        original.backup(copy)

def inspect():
    with sqlite3.connect(target) as db:
        tables = [r[0] for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
        counts = {name: db.execute('select count(*) from "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
        return {"head": db.execute("select version_num from alembic_version").fetchone()[0],
                "counts": counts, "integrity": db.execute("pragma integrity_check").fetchone()[0],
                "foreign_key_violations": len(db.execute("pragma foreign_key_check").fetchall())}

before = inspect()
os.environ["DATABASE_URL"] = "sqlite:///" + target.as_posix()
from alembic import command
from alembic.config import Config
command.upgrade(Config("alembic.ini"), "head")
after = inspect()
assert before["counts"] == after["counts"], "Migration changed row counts"
assert after["integrity"] == "ok" and after["foreign_key_violations"] == 0
print(json.dumps({"before": before, "after": after}, indent=2))
