"""Create one read-only preview login; run as the gateway service user."""

import argparse
import secrets

from aw_gateway.config import GatewayConfig
from aw_gateway.store import IdentityStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="preview")
    parser.add_argument("--email", default="preview@demo.invalid")
    arguments = parser.parse_args()
    password = f"Preview{secrets.randbelow(1_000_000):06d}!"
    config = GatewayConfig.from_environment()
    store = IdentityStore(config.database_path, slots=config.slots)
    store.initialize()
    _, slot = store.create_preview_account(arguments.username, arguments.email, password)
    print(f"username={arguments.username}\npassword={password}\nslot={slot}")


if __name__ == "__main__":
    main()
