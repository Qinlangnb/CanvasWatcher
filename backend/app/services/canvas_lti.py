"""Optional GET-only Canvas launch; opaque launch URLs never become stored facts."""

import re
from urllib.parse import urlsplit


async def gradescope_launch(client, canvas_course_id: str, provider_base: str) -> str | None:
    if not re.fullmatch(r"\d+", str(canvas_course_id)):
        return None
    expected_host = (urlsplit(provider_base).hostname or "").removeprefix("www.")
    try:
        tools = await client.get_all_pages(f"/api/v1/courses/{canvas_course_id}/external_tools",
                                           [("include_parents", "true"), ("per_page", "100")])
        candidates = []
        for tool in tools:
            urls = [tool.get("url"), (tool.get("course_navigation") or {}).get("url")]
            hosts = [(urlsplit(value).hostname or "").removeprefix("www.") for value in urls if isinstance(value, str) and value.startswith("https://")]
            domain = str(tool.get("domain") or "").removeprefix("www.")
            if expected_host and (expected_host in hosts or domain == expected_host) and re.fullmatch(r"\d+", str(tool.get("id", ""))):
                candidates.append(tool)
        # Ambiguous tools/placements require explicit configuration, never pick first.
        if len(candidates) != 1:
            return None
        tool = candidates[0]
        placement = tool.get("course_navigation") or {}
        if placement.get("enabled") is False:
            return None
        result = await client.get_json(f"/api/v1/courses/{canvas_course_id}/external_tools/sessionless_launch",
            [("id", str(tool["id"])), ("launch_type", "course_navigation")])
        launch = result.get("url") if isinstance(result, dict) else None
        if not isinstance(launch, str):
            return None
        actual, expected = urlsplit(launch), urlsplit(client.base_url)
        if actual.scheme != "https" or actual.netloc != expected.netloc or actual.username or actual.password or actual.fragment:
            return None
        return launch
    except Exception:
        # Unsupported LTI or permission failures do not invalidate credentials.
        return None
