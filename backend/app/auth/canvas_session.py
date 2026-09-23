import re
import shlex


class CanvasSessionImportError(ValueError):
    """Safe validation error that never includes imported secret text."""


_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~$0-9A-Za-z]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _cmd_unescape(value: str) -> str:
    value = re.sub(r"\^[\r\n]+\s*", " ", value)
    return re.sub(r"\^(.)", r"\1", value)


def _curl_cookie_header(value: str) -> str | None:
    try:
        tokens = shlex.split(_cmd_unescape(value), posix=True)
    except ValueError as exc:
        raise CanvasSessionImportError("Malformed copied cURL request") from exc

    headers: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        argument: str | None = None
        kind: str | None = None
        if token in {"-H", "--header", "-b", "--cookie"}:
            if index + 1 >= len(tokens):
                raise CanvasSessionImportError("Malformed copied cURL request")
            kind = "header" if token in {"-H", "--header"} else "cookie"
            argument = tokens[index + 1]
            index += 2
        elif token.startswith("--header="):
            kind, argument = "header", token.split("=", 1)[1]
            index += 1
        elif token.startswith("--cookie="):
            kind, argument = "cookie", token.split("=", 1)[1]
            index += 1
        elif token.startswith("-H") and len(token) > 2:
            kind, argument = "header", token[2:]
            index += 1
        elif token.startswith("-b") and len(token) > 2:
            kind, argument = "cookie", token[2:]
            index += 1
        else:
            index += 1
        if not argument:
            continue
        if kind == "cookie":
            headers.append(argument)
        elif kind == "header" and argument.lower().startswith("cookie:"):
            headers.append(argument.split(":", 1)[1].strip())
    return "; ".join(part for part in headers if part.strip()) or None


def parse_canvas_session_import(session_import: str) -> dict[str, str]:
    """Parse raw Cookie or copied cURL text without ever executing it."""

    if not isinstance(session_import, str) or not session_import.strip():
        raise CanvasSessionImportError("Canvas Cookie data is required")
    if len(session_import) > 131_072:
        raise CanvasSessionImportError("Canvas session import is too large")

    value = session_import.strip()
    if re.match(r"^\s*curl(?:\.exe)?(?:\s|\^)", value, re.IGNORECASE):
        header = _curl_cookie_header(value)
    elif value.lower().startswith("cookie:"):
        header = value.split(":", 1)[1].strip()
    elif "=" in value and "\n" not in value and "\r" not in value:
        header = value
    else:
        header = None
    if not header:
        raise CanvasSessionImportError("No Cookie header found in session import")
    if _CONTROL.search(header):
        raise CanvasSessionImportError("Malformed Canvas Cookie data")

    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name or not _COOKIE_NAME.fullmatch(name):
            raise CanvasSessionImportError("Malformed Canvas Cookie data")
        normalized_value = cookie_value.strip()
        if len(normalized_value) >= 2 and normalized_value[0] == normalized_value[-1] == '"':
            normalized_value = normalized_value[1:-1]
        if _CONTROL.search(normalized_value):
            raise CanvasSessionImportError("Malformed Canvas Cookie data")
        cookies[name] = normalized_value
    if not cookies:
        raise CanvasSessionImportError("No Canvas cookies found in session import")
    return cookies
