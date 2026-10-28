"""Operator commands; secrets go only to private files, never to stdout or argv."""

import argparse
import os
import secrets
import sys
from pathlib import Path

from aw_gateway.config import GatewayConfig
from aw_gateway.store import IdentityError, IdentityStore


def _legacy_hash(htpasswd: Path) -> str:
    lines = [line.strip() for line in htpasswd.read_text("utf-8").splitlines() if line.strip()]
    if len(lines) != 1 or ":" not in lines[0]:
        raise IdentityError("Expected exactly one htpasswd entry")
    value = lines[0].split(":", 1)[1]
    if not value.startswith("$6$"):
        raise IdentityError("Legacy verifier must be sha512-crypt")
    return value


def _write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(value + "\n")


def seed_owner(arguments) -> int:
    config = GatewayConfig.from_environment()
    store = IdentityStore(config.database_path, slots=config.slots)
    store.initialize()
    code = secrets.token_urlsafe(32)
    store.create_owner(arguments.username, arguments.email, _legacy_hash(arguments.htpasswd), code)
    _write_private(arguments.code_file, code)
    print("owner_seeded=1 slot=1 bootstrap_code_file=" + str(arguments.code_file))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aw-gateway-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    owner = commands.add_parser("seed-owner", help="Create the slot-1 owner awaiting first login")
    owner.add_argument("--username", required=True)
    owner.add_argument("--email", required=True)
    owner.add_argument("--htpasswd", type=Path, required=True)
    owner.add_argument("--code-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        return seed_owner(arguments)
    except (IdentityError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
