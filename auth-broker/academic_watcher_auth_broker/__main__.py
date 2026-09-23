import logging
import socket

import uvicorn

from .config import Config
from .server import create_app


def main():
    config = Config.from_env()
    # Avoid HTTP debug logs containing transport material. Never enable access logs.
    for name in ("httpx", "httpcore", "playwright"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", config.port))
        listener.listen(128)
    except OSError:
        raise SystemExit(f"Auth Broker cannot bind 127.0.0.1:{config.port}. Close the conflicting process or configure AW_BROKER_PORT.") from None
    print(f"Academic Watcher Auth Broker listening on 127.0.0.1:{config.port}")
    try:
        server = uvicorn.Server(uvicorn.Config(create_app(config), access_log=False, log_level="warning"))
        server.run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
