"""Conservative credential routing; discovery never grants authentication scope."""

from urllib.parse import unquote, urlsplit


def in_auth_scope(url: str, base_url: str, prefix: str) -> bool:
    try:
        target, base = urlsplit(url), urlsplit(base_url)
        if any(ord(char) < 32 or char == "\\" for char in url + base_url):
            return False
        if target.scheme != "https" or base.scheme != "https":
            return False
        if target.username or target.password or base.username or base.password:
            return False
        if not base.hostname or (target.hostname, target.port or 443) != (
            base.hostname, base.port or 443
        ):
            return False
        # Reject ambiguous encodings rather than trusting proxy/server decoding rules.
        path = target.path or "/"
        if unquote(path) != path or unquote(prefix) != prefix:
            return False
        if any(part in {".", ".."} for part in path.split("/") + prefix.split("/")):
            return False
        if "\\" in prefix or not prefix.startswith("/") or "//" in path:
            return False
        root = prefix.rstrip("/")
        return path == root or path.startswith(root + "/")
    except ValueError:
        return False
