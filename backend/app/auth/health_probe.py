"""Health probe that can reach a gateway-guarded backend privately (TCP or UDS)."""

import http.client
import os
import socket
from pathlib import Path

from app.auth.internal_gateway import load_internal_token


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def main() -> None:
    headers = {}
    if os.environ.get("INTERNAL_GATEWAY_AUTH_REQUIRED", "false").lower() in {
        "1", "true", "yes", "on",
    }:
        token_file = Path(
            os.environ.get(
                "INTERNAL_GATEWAY_TOKEN_FILE", "/run/secrets/academic_watcher_gateway_token"
            )
        )
        headers["X-AW-Internal-Token"] = load_internal_token(token_file).decode("ascii")
    uds = os.environ.get("AW_HEALTH_UDS", "")
    connection = (
        _UnixConnection(uds, timeout=4) if uds
        else http.client.HTTPConnection("127.0.0.1", 8000, timeout=4)
    )
    try:
        connection.request("GET", "/api/health", headers=headers)
        if connection.getresponse().status != 200:
            raise SystemExit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
