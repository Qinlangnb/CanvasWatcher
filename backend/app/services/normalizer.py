import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(url: str, base_url: str | None = None) -> str:
    absolute = urljoin(base_url or "", url)
    parts = urlsplit(absolute)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(structured: dict[str, Any], normalized_text: str = "") -> str:
    payload = {"structured": structured, "text": normalize_text(normalized_text)}
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()

