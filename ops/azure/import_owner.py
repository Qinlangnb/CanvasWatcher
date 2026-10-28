#!/opt/academic-watcher/venv/bin/python
"""Import the previous single-user dataset into Azure tenant slot 1 (the owner).

Run as root with the backend venv (needs ``cryptography``):
    sudo /opt/academic-watcher/venv/bin/python import_owner.py /root/aw-import [--replace]

The import directory (root 0700) holds data.tar (previous ``data/`` with a
consistent SQLite copy), config/, htpasswd, master.key.old and owner.env. The
old vault is decrypted only in memory and re-encrypted with the new per-tenant
key; the old key and the import directory are deleted afterwards. Nothing
secret is printed.
"""

import argparse
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SLOT = 1
USERS = Path("/var/lib/academic-watcher/users")
DATA = USERS / "t1" / "data"
CONFIG = USERS / "t1" / "config"
EXPORTS = USERS / "t1" / "exports"
SECRETS = Path("/etc/academic-watcher/tenants/t1")
GATEWAY_STATE = Path("/var/lib/academic-watcher-gateway")
PROVISION = ["/usr/bin/python3", "/opt/academic-watcher/ops/aw_provision.py"]
# The provisioner holds the slot stopped while this marker exists (see aw_provision.IMPORT_LOCK).
IMPORT_LOCK = SECRETS / "import-incomplete"
OWNER_ENV_KEYS = {
    "CANVAS_BASE_URL", "CANVAS_ACCESS_TOKEN", "CANVAS_MAX_CONCURRENCY",
    # Canvas developer key; the redirect URIs stay the public ones from the base tenant.env.
    "CANVAS_OAUTH_CLIENT_ID", "CANVAS_OAUTH_CLIENT_SECRET",
    "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "NTFY_URL", "NTFY_TOPIC",
    # Google Calendar tokens in the DB are encrypted with this key; carry it with the data.
    "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_TOKEN_ENCRYPTION_KEY",
}


def step(message: str) -> None:
    print(message, flush=True)


def run(*command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def safe_members(archive: tarfile.TarFile):
    for member in archive.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or member.isdev():
            raise RuntimeError(f"unsafe archive member rejected: {member.name}")
        yield member


def rekey_vault(old_key_file: Path) -> dict:
    vault = DATA / "credentials" / "vault.enc"
    if not vault.exists():
        return {}
    old = Fernet(old_key_file.read_bytes().strip())
    new = Fernet((SECRETS / "master.key").read_bytes().strip())
    try:
        plaintext = old.decrypt(vault.read_bytes())
    except InvalidToken as error:
        raise RuntimeError("old master key does not open the previous credential vault") from error
    decoded = json.loads(plaintext)
    if set(decoded) != {"version", "namespaces"} or decoded["version"] != 1:
        raise RuntimeError("unexpected vault structure")
    counts = {name: len(value) for name, value in decoded["namespaces"].items()}
    temporary = vault.with_name(".vault-rekey")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(new.encrypt(plaintext))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, vault)
    del plaintext, decoded
    # Prove the swap: the new key opens it, the old key no longer does.
    new.decrypt(vault.read_bytes())
    try:
        old.decrypt(vault.read_bytes())
        raise RuntimeError("old key still opens the re-keyed vault")
    except InvalidToken:
        pass
    return counts


def database_summary() -> dict:
    db = sqlite3.connect(f"file:{DATA / 'academic_watcher.db'}?mode=ro", uri=True)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}
        files = dict(db.execute(
            "SELECT integrity_status, COUNT(*) FROM downloaded_files GROUP BY integrity_status"
        ).fetchall())
        outside = db.execute(
            "SELECT COUNT(*) FROM downloaded_files WHERE local_path IS NOT NULL AND local_path NOT LIKE '/data/%'"
        ).fetchone()[0]
    finally:
        db.close()
    if integrity != "ok":
        raise RuntimeError(f"integrity_check failed: {integrity}")
    if outside:
        raise RuntimeError(f"{outside} file paths are outside /data; rewrite required before start")
    return {"tables": len(tables), "rows": counts, "file_status": files}


def write_owner_env(owner_env: Path, scheduler: bool) -> dict:
    current = (SECRETS / "tenant.env").read_text().splitlines()
    extra = {}
    for line in owner_env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key.strip() in OWNER_ENV_KEYS:
                extra[key.strip()] = value
    extra["COURSES_CONFIG"] = "/config/courses.runtime.yaml" if (CONFIG / "courses.runtime.yaml").exists() \
        else "/config/courses.yaml"
    # A rehearsal copy must not sync Canvas or push ntfy alongside the live previous instance.
    extra["SCHEDULER_ENABLED"] = "true" if scheduler else "false"
    kept = [line for line in current if line.split("=", 1)[0] not in extra]
    temporary = SECRETS / ".tenant.env.new"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("\n".join(kept + [f"{key}={value}" for key, value in sorted(extra.items())]) + "\n")
    os.replace(temporary, SECRETS / "tenant.env")
    return extra


def verify_google_tokens(env: dict) -> int:
    """Stored Google refresh tokens must still decrypt with the carried key; returns the count."""
    db = sqlite3.connect(f"file:{DATA / 'academic_watcher.db'}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = [row[0] for row in db.execute(
            "SELECT refresh_token_ciphertext FROM google_calendar_connections "
            "WHERE refresh_token_ciphertext IS NOT NULL")] if "google_calendar_connections" in tables else []
    finally:
        db.close()
    if not rows:
        return 0
    key = env.get("GOOGLE_TOKEN_ENCRYPTION_KEY", "").strip().strip('"').strip("'")
    if not key:
        raise RuntimeError("Google tokens present but GOOGLE_TOKEN_ENCRYPTION_KEY was not imported")
    cipher = Fernet(key.encode())
    for value in rows:
        try:
            cipher.decrypt(value.encode())
        except InvalidToken as error:
            raise RuntimeError("an imported Google token does not decrypt with the imported key") from error
    return len(rows)


def seed_owner(source: Path, username: str, email: str) -> bool:
    gateway = pwd.getpwnam("aw-gateway")
    staged = GATEWAY_STATE / "import-htpasswd"
    shutil.copyfile(source / "htpasswd", staged)
    os.chown(staged, gateway.pw_uid, gateway.pw_gid)
    os.chmod(staged, 0o600)
    code_file = GATEWAY_STATE / "owner-bootstrap.code"
    try:
        result = subprocess.run(
            ["runuser", "-u", "aw-gateway", "--", "env",
             "AW_GATEWAY_DATABASE=" + str(GATEWAY_STATE / "identity.db"),
             "/opt/academic-watcher/gateway-venv/bin/python", "-m", "aw_gateway.admin", "seed-owner",
             "--username", username, "--email", email,
             "--htpasswd", str(staged), "--code-file", str(code_file)],
            capture_output=True, text=True, check=False,
        )
    finally:
        staged.unlink(missing_ok=True)
    if result.returncode:
        if "Owner already exists" in result.stderr:
            return False
        raise RuntimeError("seed-owner failed: " + result.stderr.strip()[-200:])
    step(result.stdout.strip())
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--replace", action="store_true", help="discard a previous rehearsal import")
    parser.add_argument("--enable-scheduler", action="store_true",
                        help="final cutover only, after the previous backend is stopped")
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2
    for name in ("data.tar", "htpasswd", "master.key.old", "owner.env", "config"):
        if not (arguments.source / name).exists():
            raise SystemExit(f"missing {name} in import directory")

    # The provisioner restarts inactive owner instances; keep it paused until the data is complete.
    run("systemctl", "stop", "aw-provision.timer", "aw-provision.service")
    try:
        return import_slot(arguments)
    except BaseException:
        # Leave the slot stopped and locked: a half-imported dataset must never start, or the
        # startup file audit would rewrite file states. Re-run the import to clear the lock.
        subprocess.run(["systemctl", "disable", "--now", f"aw-tenant@{SLOT}.service"], check=False)
        print(f"import failed; slot {SLOT} stays stopped until a successful re-import", file=sys.stderr)
        raise
    finally:
        run("systemctl", "start", "aw-provision.timer")


def import_slot(arguments) -> int:
    source = arguments.source
    run(*PROVISION, "prepare", str(SLOT), "--no-template-config")
    subprocess.run(["systemctl", "stop", f"aw-tenant@{SLOT}.service"], check=False)
    IMPORT_LOCK.touch(mode=0o600)
    # prepare_slot creates empty downloads/ and credentials/; only a database means real data.
    if (DATA / "academic_watcher.db").exists():
        if not arguments.replace:
            raise SystemExit("slot 1 already holds data; pass --replace to discard it")
        for child in DATA.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        step("previous slot-1 data removed")

    with tarfile.open(source / "data.tar") as archive:
        archive.extractall(DATA, members=safe_members(archive), filter="data")
    step("data extracted")
    for item in (source / "config").iterdir():
        if item.is_file():
            shutil.copyfile(item, CONFIG / item.name)
    (DATA / "credentials").mkdir(exist_ok=True)
    (DATA / "downloads").mkdir(exist_ok=True)

    counts = rekey_vault(source / "master.key.old")
    step("vault re-keyed; entries per namespace: " + json.dumps(counts, sort_keys=True))
    env = write_owner_env(source / "owner.env", arguments.enable_scheduler)
    step(f"google tokens verified with the imported key: {verify_google_tokens(env)}")

    owner = pwd.getpwnam("aw-t1")
    for base in (DATA, CONFIG, EXPORTS):
        run("chown", "-R", f"{owner.pw_uid}:{owner.pw_gid}", str(base))
        run("chmod", "-R", "u+rwX,go-rwx", str(base))
    summary = database_summary()
    step(f"sqlite ok: tables={summary['tables']} files={json.dumps(summary['file_status'], sort_keys=True)}")
    step("row_counts=" + json.dumps(summary["rows"], sort_keys=True))

    seeded = seed_owner(source, arguments.username, arguments.email)
    if not seeded:
        step("owner account already present; first-login state unchanged")

    # Old key and staging copies are no longer needed on Azure (the previous host keeps the original).
    run("shred", "-u", str(source / "master.key.old"))
    shutil.rmtree(source)
    step("import directory removed")

    IMPORT_LOCK.unlink()
    run("systemctl", "enable", "--now", f"aw-tenant@{SLOT}.service")
    health = subprocess.run([*PROVISION, "health", str(SLOT)], check=False)
    for _ in range(60):
        if health.returncode == 0:
            break
        time.sleep(3)
        health = subprocess.run([*PROVISION, "health", str(SLOT)], check=False)
    step("owner instance healthy" if health.returncode == 0 else "owner instance NOT healthy")
    return 0 if health.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
