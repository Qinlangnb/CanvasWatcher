#!/usr/bin/python3
"""Root-only tenant provisioner for the Azure multi-user deployment.

The identity gateway holds no system privileges. It records registrations as
``pending``; this timer-driven job creates the isolated tenant (system user,
5 GiB loop filesystem, private key, gateway token, systemd instance), waits for
the instance to answer over its private socket and only then activates the
account. Disabled accounts have their instance stopped; data is kept.

Standard library only; runs with the system python3.
"""

import argparse
import base64
import grp
import http.client
import os
import pwd
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/var/lib/academic-watcher")
IMAGES = ROOT / "images"
USERS = ROOT / "users"
ETC = Path("/etc/academic-watcher")
TENANT_SECRETS = ETC / "tenants"
TOKENS = ETC / "tenant-tokens"
RUN = Path("/run/academic-watcher")
TEMPLATES = Path("/opt/academic-watcher/config-templates")
IDENTITY_DB = Path("/var/lib/academic-watcher-gateway/identity.db")
GATEWAY_USER = "aw-gateway"
QUOTA = "5G"
def public_origin() -> str:
    value = os.environ.get("AW_GATEWAY_PUBLIC_ORIGIN", "")
    if not value:
        config = ETC / "public.env"
        if config.is_file():
            for line in config.read_text(encoding="utf-8").splitlines():
                if line.startswith("AW_GATEWAY_PUBLIC_ORIGIN="):
                    value = line.partition("=")[2].strip().strip('"')
                    break
    return (value or "https://watcher.example.com").rstrip("/")


PUBLIC = public_origin()
# Written by import_owner.py while a slot holds a partial dataset; the provisioner never starts it.
IMPORT_LOCK = "import-incomplete"

BASE_ENV = f"""APP_ENV=production
DATABASE_URL=sqlite:////data/academic_watcher.db
DOWNLOAD_ROOT=/data/downloads
COURSES_CONFIG=/config/courses.yaml
FILE_RULES_CONFIG=/config/file_rules.yaml
AVAILABILITY_CONFIG=/config/availability.yaml
CREDENTIAL_PERSISTENCE_ENABLED=true
CREDENTIAL_VAULT_PATH=/data/credentials/vault.enc
INTERNAL_GATEWAY_AUTH_REQUIRED=true
SCHEDULER_ENABLED=true
LOG_LEVEL=INFO
CORS_ORIGINS={PUBLIC}
FRONTEND_URL={PUBLIC}/watcher
WATCHER_PUBLIC_BASE={PUBLIC}/watcher
CANVAS_OAUTH_REDIRECT_URI={PUBLIC}/watcher/api/auth/canvas/oauth/callback
GOOGLE_CALENDAR_REDIRECT_URI={PUBLIC}/watcher/api/integrations/google-calendar/oauth/callback
"""


def log(message: str) -> None:
    print(message, flush=True)


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def slot_name(slot: int) -> str:
    if not 1 <= slot <= 5:
        raise ValueError("invalid slot")
    return f"t{slot}"


def write_private(path: Path, data: bytes, *, uid: int = 0, gid: int = 0, mode: int = 0o600) -> None:
    """Create-once secret file; never overwrite an existing key or token."""
    if path.exists():
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def ensure_user(name: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        run("useradd", "--system", "--user-group", "--home-dir", "/nonexistent",
            "--no-create-home", "--shell", "/usr/sbin/nologin", name)
        return pwd.getpwnam(name)


def ensure_filesystem(slot: int) -> Path:
    name = slot_name(slot)
    image = IMAGES / f"{name}.img"
    mountpoint = USERS / name
    IMAGES.mkdir(mode=0o700, parents=True, exist_ok=True)
    USERS.mkdir(mode=0o711, parents=True, exist_ok=True)
    mountpoint.mkdir(mode=0o700, exist_ok=True)
    if not image.exists():
        run("truncate", "-s", QUOTA, str(image))
        os.chmod(image, 0o600)
        run("mkfs.ext4", "-q", "-m", "0", "-L", f"aw-{name}", str(image))
    entry = f"{image} {mountpoint} ext4 loop,nodev,nosuid,noexec 0 2"
    fstab = Path("/etc/fstab").read_text()
    if entry not in fstab.splitlines():
        with open("/etc/fstab", "a") as output:
            output.write(entry + "\n")
        run("systemctl", "daemon-reload")
    if run("mountpoint", "-q", str(mountpoint), check=False).returncode != 0:
        run("mount", str(mountpoint))
    return mountpoint


def prepare_slot(slot: int, *, template_config: bool = True) -> dict:
    """Idempotently create everything a tenant needs, without starting it."""
    name = slot_name(slot)
    account = ensure_user(f"aw-{name}")
    gateway = grp.getgrnam(GATEWAY_USER)
    uid, gid = account.pw_uid, account.pw_gid
    mountpoint = ensure_filesystem(slot)
    os.chown(mountpoint, uid, gid)
    os.chmod(mountpoint, 0o700)
    for relative in ("data", "data/downloads", "data/credentials", "config", "exports"):
        path = mountpoint / relative
        path.mkdir(mode=0o700, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, 0o700)
    config = mountpoint / "config"
    if template_config:
        for template in ("courses.yaml", "file_rules.yaml", "availability.yaml"):
            target = config / template
            if not target.exists():
                shutil.copyfile(TEMPLATES / template, target)
                os.chown(target, uid, gid)
                os.chmod(target, 0o600)

    TENANT_SECRETS.mkdir(mode=0o700, parents=True, exist_ok=True)
    secrets_dir = TENANT_SECRETS / name
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    # Fernet key for the per-instance vault; systemd hands it over via LoadCredential.
    write_private(secrets_dir / "master.key", base64.urlsafe_b64encode(secrets.token_bytes(32)))
    write_private(secrets_dir / "tenant.env", BASE_ENV.encode())

    TOKENS.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(TOKENS, 0, gateway.gr_gid)
    os.chmod(TOKENS, 0o750)
    write_private(TOKENS / f"tenant-{slot}.token", secrets.token_urlsafe(48).encode(),
                  uid=0, gid=gateway.gr_gid, mode=0o640)
    return {"uid": uid, "gid": gid, "mountpoint": mountpoint}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=5)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def tenant_healthy(slot: int) -> bool:
    token = (TOKENS / f"tenant-{slot}.token").read_bytes().strip().decode()
    connection = _UnixConnection(str(RUN / slot_name(slot) / "api.sock"))
    try:
        connection.request("GET", "/api/health", headers={
            "Host": PUBLIC.split("://", 1)[1], "X-AW-Internal-Token": token,
        })
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


def start_tenant(slot: int, *, timeout: int = 180) -> bool:
    if run("systemctl", "enable", "--now", f"aw-tenant@{slot}.service", check=False).returncode:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tenant_healthy(slot):
            return True
        time.sleep(2)
    return False


def stop_tenant(slot: int) -> None:
    run("systemctl", "disable", "--now", f"aw-tenant@{slot}.service", check=False)


def _as_gateway(callback):
    """Touch the identity DB as its owner so no root-owned journal is left behind."""
    account = pwd.getpwnam(GATEWAY_USER)
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            callback()
            os._exit(0)
        except BaseException as error:  # noqa: BLE001 - report and exit child
            print(f"identity db error: {type(error).__name__}", file=sys.stderr)
            os._exit(1)
    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status) == 0


def read_users() -> list[dict]:
    if not IDENTITY_DB.exists():
        return []
    reader, writer = os.pipe()
    rows: list[dict] = []

    def dump():
        os.close(reader)
        db = sqlite3.connect(f"file:{IDENTITY_DB}?mode=ro", uri=True, timeout=10)
        lines = [f"{row[0]}\t{row[1]}\t{row[2]}\t{row[3]}" for row in db.execute(
            """SELECT u.id, u.status, u.slot,
               EXISTS(SELECT 1 FROM preview_accounts p WHERE p.user_id=u.id)
               FROM users u"""
        )]
        os.write(writer, "\n".join(lines).encode())
        os.close(writer)

    child = os.fork()
    if child == 0:
        account = pwd.getpwnam(GATEWAY_USER)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        try:
            dump()
            os._exit(0)
        except BaseException:  # noqa: BLE001 - forked child must never return
            os._exit(1)
    os.close(writer)
    data = b""
    while chunk := os.read(reader, 65536):
        data += chunk
    os.close(reader)
    _, status = os.waitpid(child, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise RuntimeError("cannot read identity database")
    for line in data.decode().splitlines():
        user_id, status_value, slot, preview = line.split("\t")
        rows.append({"id": user_id, "status": status_value, "slot": int(slot), "preview": preview == "1"})
    return rows


def activate(user_id: str) -> bool:
    def update():
        db = sqlite3.connect(IDENTITY_DB, timeout=10, isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE users SET status='active' WHERE id=? AND status='pending'", (user_id,)
        ).rowcount
        if changed == 1:
            db.execute(
                "INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'user_activated', ?)",
                (user_id, int(time.time())),
            )
        db.execute("COMMIT")
        db.close()

    return _as_gateway(update)


def unit_active(slot: int) -> bool:
    return run("systemctl", "is-active", "--quiet", f"aw-tenant@{slot}.service", check=False).returncode == 0


def reconcile() -> int:
    failures = 0
    for user in read_users():
        try:
            failures += reconcile_user(user)
        except Exception as error:  # noqa: BLE001 - one tenant must not block the others
            failures += 1
            log(f"slot {user['slot']}: reconcile error {type(error).__name__}")
    return 1 if failures else 0


def reconcile_user(user: dict) -> int:
    slot = user["slot"]
    if user.get("preview"):
        # A preview login has no tenant process, storage image, credentials or upstream access.
        return 0
    if (TENANT_SECRETS / slot_name(slot) / IMPORT_LOCK).exists():
        if unit_active(slot):
            stop_tenant(slot)
        log(f"slot {slot}: import incomplete; instance held stopped")
        return 1
    if user["status"] == "disabled":
        if unit_active(slot):
            stop_tenant(slot)
            log(f"slot {slot}: stopped (account disabled)")
        return 0
    if user["status"] == "pending":
        prepare_slot(slot)
        if start_tenant(slot) and activate(user["id"]):
            log(f"slot {slot}: provisioned and activated")
        else:
            log(f"slot {slot}: provisioning failed; account stays pending")
            return 1
        return 0
    # Active accounts: keep the instance running (restart policy covers crashes).
    if (not unit_active(slot) and (TENANT_SECRETS / slot_name(slot) / "master.key").exists()
            and not start_tenant(slot)):
        log(f"slot {slot}: instance not healthy")
        return 1
    return 0


def purge(slot: int, username: str) -> int:
    """Free a disabled account's slot: delete the identity row, instance, data image and secrets."""
    if slot == 1:
        log("slot 1 belongs to the owner and is never purged")
        return 2
    name = slot_name(slot)

    def remove(*, commit: bool):
        db = sqlite3.connect(IDENTITY_DB, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id, status FROM users WHERE slot=? AND username=?", (slot, username)).fetchone()
        if row is None or row[1] != "disabled":
            db.execute("ROLLBACK")
            raise RuntimeError("account must exist in this slot and be disabled")
        if not commit:
            db.execute("ROLLBACK")
            return
        db.execute("DELETE FROM invitations WHERE redeemed_by=?", (row[0],))
        db.execute("DELETE FROM users WHERE id=?", (row[0],))
        db.execute("INSERT INTO audit(actor_id, action, occurred_at) VALUES (?, 'user_purged', ?)",
                   (row[0], int(time.time())))
        db.execute("COMMIT")
        db.close()

    if not _as_gateway(lambda: remove(commit=False)):
        log(f"slot {slot}: not purged (account must be disabled and the username must match)")
        return 1
    # Storage goes first: the slot is only released (identity row deleted) once nothing of the
    # previous tenant can still be mounted or running, so a new invitation never inherits it.
    stop_tenant(slot)
    if unit_active(slot):
        log(f"slot {slot}: instance still running; nothing removed")
        return 1
    mountpoint = USERS / name
    mounted = run("mountpoint", "-q", str(mountpoint), check=False).returncode == 0
    if mounted and run("umount", str(mountpoint), check=False).returncode != 0:
        log(f"slot {slot}: data filesystem busy; nothing removed")
        return 1
    image = IMAGES / f"{name}.img"
    fstab = Path("/etc/fstab")
    kept = [line for line in fstab.read_text().splitlines(keepends=True) if f"{image} " not in line]
    temporary = Path("/etc/fstab.aw-purge")
    temporary.write_text("".join(kept))
    os.chmod(temporary, 0o644)
    os.replace(temporary, fstab)
    run("systemctl", "daemon-reload")
    image.unlink(missing_ok=True)
    (TOKENS / f"tenant-{slot}.token").unlink(missing_ok=True)
    run("rm", "-rf", "--one-file-system", str(mountpoint), str(TENANT_SECRETS / name))
    run("userdel", f"aw-{name}", check=False)
    if not _as_gateway(lambda: remove(commit=True)):
        log(f"slot {slot}: storage removed but the identity row remains; re-run purge")
        return 1
    log(f"slot {slot}: purged and free for a new invitation")
    return 0


def main() -> int:
    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reconcile")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("slot", type=int)
    prepare.add_argument("--no-template-config", action="store_true")
    health = commands.add_parser("health")
    health.add_argument("slot", type=int)
    remove = commands.add_parser("purge", help="irreversibly delete a disabled account and its data")
    remove.add_argument("slot", type=int)
    remove.add_argument("--username", required=True, help="must match the disabled account in the slot")
    arguments = parser.parse_args()
    if arguments.command == "reconcile":
        return reconcile()
    if arguments.command == "purge":
        return purge(arguments.slot, arguments.username)
    if arguments.command == "prepare":
        info = prepare_slot(arguments.slot, template_config=not arguments.no_template_config)
        log(f"slot {arguments.slot}: prepared at {info['mountpoint']}")
        return 0
    return 0 if tenant_healthy(arguments.slot) else 1


if __name__ == "__main__":
    raise SystemExit(main())
