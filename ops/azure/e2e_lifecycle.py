#!/opt/academic-watcher/gateway-venv/bin/python
"""End-to-end lifecycle check on the Azure host, with a throwaway invited user.

    sudo /opt/academic-watcher/gateway-venv/bin/python e2e_lifecycle.py

Covers: invite -> register (pending) -> provisioner creates the isolated slot ->
login -> empty private instance -> cross-user IDs denied -> 5 GiB quota enforced
without affecting the owner -> disable revokes sessions and stops the instance.
The test account and its slot are purged at the end. Identity DB writes run as
aw-gateway; the throwaway password and invite are never printed.
"""

import json
import os
import pwd
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, "/opt/academic-watcher/identity-gateway")
from aw_gateway.security import token_digest

GATEWAY = "http://127.0.0.1:18780"
ORIGIN = os.environ.get("AW_GATEWAY_PUBLIC_ORIGIN", "https://watcher.example.com")
PUBLIC_HOST = urlsplit(ORIGIN).netloc
HEADERS = {"Host": PUBLIC_HOST, "Origin": ORIGIN}
IDENTITY_DB = "/var/lib/academic-watcher-gateway/identity.db"
PROVISION = ["/usr/bin/python3", "/opt/academic-watcher/ops/aw_provision.py"]
USERNAME = "e2e-lifecycle"
EMAIL = "e2e-lifecycle@invalid.test"
results: list[tuple[str, bool]] = []
# Run under the tenant's IP policy: IMDS, the loopback gateway and the VNet must be unreachable.
OUTBOUND_PROBE = """
import socket
def reach(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close()
        return "open"
    except OSError:
        return "blocked"
print(reach("169.254.169.254", 80), reach("127.0.0.1", 18780), reach("10.0.0.4", 22),
      reach(socket.gethostbyname("pypi.org"), 443))
"""


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)


def as_gateway(sql: str, parameters=()) -> list:
    """Run one statement against the identity DB as its owner; returns rows."""
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        account = pwd.getpwnam("aw-gateway")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        db = sqlite3.connect(IDENTITY_DB, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        rows = db.execute(sql, parameters).fetchall()
        os.write(writer, json.dumps([list(row) for row in rows]).encode())
        os._exit(0)
    os.close(writer)
    data = b""
    while chunk := os.read(reader, 65536):
        data += chunk
    os.close(reader)
    os.waitpid(child, 0)
    return json.loads(data) if data else []


def raw_status(target: str, cookie: str) -> int:
    """Send the request target verbatim (httpx would normalize dot segments away)."""
    with socket.create_connection(("127.0.0.1", 18780), timeout=30) as connection:
        connection.sendall(f"GET {target} HTTP/1.1\r\nHost: {PUBLIC_HOST}\r\nCookie: {cookie}\r\n"
                           "Connection: close\r\n\r\n".encode())
        return int(connection.recv(64).split(b" ", 2)[1])


def owner_reference_ids() -> tuple[int, int]:
    db = sqlite3.connect("file:/var/lib/academic-watcher/users/t1/data/academic_watcher.db?mode=ro", uri=True)
    try:
        return (db.execute("SELECT id FROM courses ORDER BY id LIMIT 1").fetchone()[0],
                db.execute("SELECT id FROM tasks ORDER BY id LIMIT 1").fetchone()[0])
    finally:
        db.close()


def purge_slot(slot: int) -> None:
    name = f"t{slot}"
    subprocess.run(["systemctl", "disable", "--now", f"aw-tenant@{slot}.service"], check=False, capture_output=True)
    mountpoint = f"/var/lib/academic-watcher/users/{name}"
    subprocess.run(["umount", mountpoint], check=False, capture_output=True)
    with open("/etc/fstab") as source:
        lines = source.readlines()
    with open("/etc/fstab.aw-e2e", "w") as output:
        output.writelines(line for line in lines if f"/images/{name}.img " not in line)
    os.replace("/etc/fstab.aw-e2e", "/etc/fstab")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    for path in (f"/var/lib/academic-watcher/images/{name}.img", f"/etc/academic-watcher/tenant-tokens/tenant-{slot}.token"):
        if os.path.exists(path):
            os.unlink(path)
    subprocess.run(["rm", "-rf", "--one-file-system", mountpoint, f"/etc/academic-watcher/tenants/{name}"], check=True)
    subprocess.run(["userdel", f"aw-{name}"], check=False, capture_output=True)


def main() -> int:
    if os.geteuid() != 0:
        print("must run as root", file=sys.stderr)
        return 2
    owner = as_gateway("SELECT id FROM users WHERE slot=1")
    if not owner or as_gateway("SELECT 1 FROM users WHERE username=?", (USERNAME,)):
        print("owner missing or a previous e2e user still exists", file=sys.stderr)
        return 2
    owner_course, owner_task = owner_reference_ids()
    password = secrets.token_urlsafe(24)
    invite = secrets.token_urlsafe(32)
    now = int(time.time())
    as_gateway("INSERT INTO invitations VALUES (?, ?, ?, ?, ?, NULL)",
               (token_digest(invite), EMAIL, owner[0][0], now, now + 600))
    slot = None
    client = httpx.Client(base_url=GATEWAY, headers=HEADERS, follow_redirects=False, timeout=30)
    try:
        form = {"invite": invite, "username": USERNAME, "email": EMAIL, "password": password}
        check("register accepted (202)", client.post("/watcher/register", data=form).status_code == 202)
        check("invite cannot be reused", client.post("/watcher/register", data={**form, "username": USERNAME + "2"}).status_code == 400)
        row = as_gateway("SELECT id, status, slot FROM users WHERE username=?", (USERNAME,))[0]
        user_id, slot = row[0], row[2]
        check("new account pending", row[1] == "pending")
        check("pending account cannot log in",
              client.post("/watcher/login", data={"username": USERNAME, "password": password}).status_code == 401)

        subprocess.run([*PROVISION, "reconcile"], check=False)
        status = as_gateway("SELECT status FROM users WHERE id=?", (user_id,))[0][0]
        check(f"provisioner activated slot {slot}", status == "active")
        mount = subprocess.run(["findmnt", "-n", "-b", "-o", "SIZE", f"/var/lib/academic-watcher/users/t{slot}"],
                               capture_output=True, text=True, check=False).stdout.strip()
        # A 5 GiB ext4 image reports about 4.8 GiB usable after filesystem overhead.
        check(f"slot on its own 5G filesystem ({mount} bytes)", mount.isdigit() and 4.5 * 2**30 < int(mount) <= 5 * 2**30)

        anonymous = httpx.Client(base_url=GATEWAY, headers=HEADERS, timeout=30)
        check("anonymous API denied", anonymous.get("/watcher/api/courses").status_code in (401, 403))
        login = client.post("/watcher/login", data={"username": USERNAME, "password": password})
        check("login succeeds", login.status_code == 303)
        # Cookies are Secure (HTTPS via Caddy); pass them explicitly over the loopback hop.
        jar = {cookie.split("=", 1)[0]: cookie.split("=", 1)[1].split(";", 1)[0]
               for cookie in login.headers.get_list("set-cookie")}
        client.headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in jar.items())
        courses = client.get("/watcher/api/courses")
        check("new user sees an empty private instance", courses.status_code == 200 and courses.json() == [])
        check("owner course id not visible", client.get(f"/watcher/api/courses/{owner_course}").status_code == 404)
        check("owner task id not visible", client.get(f"/watcher/api/tasks/{owner_task}").status_code == 404)
        for target in ("/watcher/api/../api/courses", "/watcher/api/%2e%2e/api/courses", "/watcher/api/./courses"):
            check(f"dot-segment path refused ({target})", raw_status(target, client.headers["Cookie"]) in (400, 404))
        mutate = client.post("/watcher/api/sync/run", headers={"Origin": ORIGIN})
        check("mutation without CSRF refused", mutate.status_code == 403)

        # Same IP policy as the running instance, applied by systemd to a probe in its own cgroup.
        policy = subprocess.run(["systemctl", "show", f"aw-tenant@{slot}.service", "-p", "IPAddressAllow",
                                 "-p", "IPAddressDeny"], capture_output=True, text=True, check=True).stdout
        properties = [f"--property={line}" for line in policy.splitlines() if line.split("=", 1)[1]]
        probe = OUTBOUND_PROBE
        outbound = subprocess.run(["systemd-run", "--quiet", "--wait", "--pipe", "--collect", *properties,
                                   f"--uid=aw-t{slot}", "/usr/bin/python3", "-c", probe],
                                  capture_output=True, text=True, check=False).stdout.split()
        check(f"outbound guard: IMDS/loopback/VNet blocked, internet open ({' '.join(outbound)})",
              outbound == ["blocked", "blocked", "blocked", "open"])

        filler = f"/var/lib/academic-watcher/users/t{slot}/data/quota-filler"
        quota = subprocess.run(["runuser", "-u", f"aw-t{slot}", "--", "fallocate", "-l", "6G", filler],
                               capture_output=True, text=True, check=False)
        check("6G write refused by 5G quota", quota.returncode != 0 and "No space" in quota.stderr)
        subprocess.run(["rm", "-f", filler], check=False)
        check("owner instance still healthy", subprocess.run([*PROVISION, "health", "1"], check=False).returncode == 0)

        as_gateway("UPDATE users SET status='disabled' WHERE id=?", (user_id,))
        check("disabled user's session rejected", client.get("/watcher/api/courses").status_code in (401, 403, 303))
        subprocess.run([*PROVISION, "reconcile"], check=False)
        active = subprocess.run(["systemctl", "is-active", "--quiet", f"aw-tenant@{slot}.service"], check=False)
        check("disabled user's instance stopped", active.returncode != 0)
        check("disabled user cannot log in",
              client.post("/watcher/login", data={"username": USERNAME, "password": password}).status_code == 401)
        purged = subprocess.run([*PROVISION, "purge", str(slot), "--username", USERNAME], check=False)
        check("purge frees the disabled slot", purged.returncode == 0
              and not as_gateway("SELECT 1 FROM users WHERE slot=?", (slot,))
              and not os.path.exists(f"/var/lib/academic-watcher/images/t{slot}.img"))
    finally:
        client.close()
        user = as_gateway("SELECT id FROM users WHERE username=?", (USERNAME,))
        if user:
            as_gateway("DELETE FROM invitations WHERE redeemed_by=?", (user[0][0],))
            as_gateway("DELETE FROM users WHERE id=?", (user[0][0],))
        as_gateway("DELETE FROM invitations WHERE email=?", (EMAIL,))
        if slot and slot != 1:
            purge_slot(slot)
        print("test account and slot purged", flush=True)
    failed = [name for name, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
