"""Read-only source evidence audit. Never prints credentials or raw payloads."""
import argparse
import json
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
parser.add_argument("--course-id", type=int, required=True)
args = parser.parse_args()
db = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
sources = []
for row in db.execute("select id,course_id,state,last_sync_at,last_success_at,last_error from course_sources where course_id=?", (args.course_id,)):
    value = dict(row)
    error = value.pop("last_error") or ""
    value["error_codes"] = [code for code in ["auth_required", "invalid_token", "permission_denied", "timeout", "network_error", "rate_limited", "server_error", "invalid_response"] if code in error]
    sources.append(value)
snapshots = []
for row in db.execute("select i.id,i.external_id,i.item_type,i.title,s.captured_at,s.structured_json,length(s.normalized_text) as text_length from source_items i join source_snapshots s on s.source_item_id=i.id where i.course_id=? order by s.captured_at desc limit 30", (args.course_id,)):
    value = dict(row)
    payload = json.loads(value.pop("structured_json"))
    value.update(fields=list(payload), due_at_present="due_at" in payload,
                 due_at=payload.get("due_at"), description_length=len(payload.get("description") or ""),
                 body_present="body" in payload)
    snapshots.append(value)
tasks = [dict(row) for row in db.execute("select id,title,due_at,due_date_local,deadline_precision,status,length(description) as description_length from tasks where course_id=?", (args.course_id,))]
print(json.dumps({"sources": sources, "latest_snapshots": snapshots, "tasks": tasks}, indent=2))
