"""Upgrade an isolated SQLite copy, asserting existing rows remain unchanged."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
if 'data' not in path.parts or path.name not in {'migration-copy.db', 'migration-fresh.db'}:
    raise SystemExit('Only named isolated rehearsal databases are accepted')


def rows():
    with sqlite3.connect(path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if r[0] not in {'alembic_version', 'sqlite_sequence', 'change_filter_rules', 'change_filter_audits'}]
        return {name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()
                for name in tables}


before = rows()
env = {**os.environ, 'DATABASE_URL': 'sqlite:///' + path.as_posix()}
subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], env=env, check=True)
after = rows()
assert all(after[name] == value for name, value in before.items()), 'Existing row content changed'
with sqlite3.connect(path) as db:
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0012_change_filter_rules'
print({'database': path.name, 'preserved_tables': len(before), 'preserved_rows': sum(map(len, before.values())), 'integrity': 'ok'})
