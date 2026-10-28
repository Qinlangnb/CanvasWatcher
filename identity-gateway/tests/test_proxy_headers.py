from fastapi import Request

from aw_gateway.config import GatewayConfig
from aw_gateway.proxy import _headers, _url


def test_private_hop_strips_browser_identity_and_sets_gateway_identity(tmp_path):
    config = GatewayConfig(
        database_path=tmp_path / "identity.db", static_root=tmp_path / "frontend",
        socket_root=tmp_path / "sockets", token_root=tmp_path / "tokens",
        csrf_key_file=tmp_path / "csrf.key",
    )
    request = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/watcher/api/courses",
        "query_string": b"limit=2", "server": ("watcher.example.com", 443), "client": ("127.0.0.1", 1000),
        "headers": [
            (b"host", b"evil.example"), (b"cookie", b"aw_session=private"),
            (b"authorization", b"Bearer browser"), (b"x-forwarded-for", b"1.2.3.4"),
            (b"x-aw-internal-token", b"forged"), (b"x-aw-user", b"other"),
            (b"x-aw-csrf", b"csrf"), (b"origin", b"https://watcher.example.com"),
        ],
    })
    headers = _headers(request, config, b"gateway-secret" * 3)
    lowered = {key.lower(): value for key, value in headers}
    assert lowered[b"host"] == b"watcher.example.com"
    assert lowered[b"x-aw-internal-token"] == b"gateway-secret" * 3
    assert lowered[b"x-forwarded-proto"] == b"https"
    assert lowered[b"origin"] == b"https://watcher.example.com"
    for forbidden in (b"cookie", b"authorization", b"x-forwarded-for", b"x-aw-user", b"x-aw-csrf"):
        assert forbidden not in lowered
    assert _url(request, config) == "http://tenant/api/courses?limit=2"
