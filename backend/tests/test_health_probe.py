import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from app.auth import health_probe

TOKEN = "t" * 48


def serve_unix(path, seen):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("X-AW-Internal-Token")))
            ok = self.headers.get("X-AW-Internal-Token") == TOKEN
            self.send_response(200 if ok else 401)
            self.end_headers()

        def log_message(self, *args):
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def get_request(self):
            request, _ = super().get_request()
            return request, ("uds", 0)

    server = Server(str(path), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.skipif(os.name != "posix", reason="Unix domain sockets")
def test_probe_reaches_guarded_backend_over_uds_with_token(tmp_path, monkeypatch):
    token_file = tmp_path / "gateway.token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o440)
    seen = []
    server = serve_unix(tmp_path / "api.sock", seen)
    try:
        monkeypatch.setenv("AW_HEALTH_UDS", str(tmp_path / "api.sock"))
        monkeypatch.setenv("INTERNAL_GATEWAY_AUTH_REQUIRED", "true")
        monkeypatch.setenv("INTERNAL_GATEWAY_TOKEN_FILE", str(token_file))
        health_probe.main()
        assert seen == [("/api/health", TOKEN)]

        monkeypatch.setenv("INTERNAL_GATEWAY_AUTH_REQUIRED", "false")
        with pytest.raises(SystemExit):
            health_probe.main()
    finally:
        server.shutdown()
        server.server_close()
