import logging
import re
from typing import Any

import structlog

_SECRET_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "session_import",
    "canvas_session",
    "_csrf_token",
    "one_time_token",
    "control_token",
    "exchange_token",
    "access_token",
    "refresh_token",
    "oauth_code",
    "client_secret",
    "cookies",
}
_SECRET_TEXT = re.compile(
    r"(?i)(authorization\s*:|bearer\s+|cookie\s*:|set-cookie\s*:|"
    r"session_import|canvas_session|_csrf_token)"
)


def redact_secrets(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    def redact(value: Any, key: str | None = None) -> Any:
        normalized_key = (key or "").lower().replace("_", "-")
        if key and (key.lower() in _SECRET_KEYS or normalized_key in _SECRET_KEYS):
            return "<redacted>"
        if isinstance(value, dict):
            return {child_key: redact(child, child_key) for child_key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(child) for child in value]
        if isinstance(value, str) and _SECRET_TEXT.search(value):
            return "<redacted>"
        return value

    return {key: redact(value, key) for key, value in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    class AuthCallbackFilter(logging.Filter):
        def filter(self, record):
            return "/api/auth/canvas/oauth/callback" not in str(record.msg) + str(record.args)

    logging.getLogger("uvicorn.access").addFilter(AuthCallbackFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact_secrets,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )
