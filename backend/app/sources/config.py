from dataclasses import dataclass
from datetime import date
from typing import Any

import yaml

from app.config import Settings
from app.services.normalizer import canonicalize_url


@dataclass(frozen=True)
class WebsiteDefinition:
    course_code: str
    course_name: str
    timezone: str
    term_calendar: dict[str, Any]
    export: dict[str, Any]
    config: dict[str, Any]


def website_term_calendar(settings: Settings, source_url: str) -> dict[str, Any]:
    """Exact-source date anchors without seeding or duplicating website sources."""
    if not settings.courses_config.exists():
        return {}
    root = yaml.safe_load(settings.courses_config.read_text(encoding="utf-8")) or {}
    calendars = root.get("source_calendars", {})
    if not isinstance(calendars, dict):
        return {}
    calendar = calendars.get(canonicalize_url(source_url), {})
    if not isinstance(calendar, dict):
        return {}
    try:
        monday = date.fromisoformat(str(calendar.get("week_1_monday", "")))
    except ValueError:
        return {}
    if monday.weekday() != 0:
        return {}
    return {**calendar, "week_1_monday": monday.isoformat()}


def website_definitions(settings: Settings) -> list[WebsiteDefinition]:
    if not settings.courses_config.exists():
        return []
    root = yaml.safe_load(settings.courses_config.read_text(encoding="utf-8")) or {}
    definitions: list[WebsiteDefinition] = []
    for course in root.get("courses", []):
        code = course["course_code"]
        name = course.get("name") or code
        for website in course.get("websites", []):
            definitions.append(
                WebsiteDefinition(
                    course_code=code,
                    course_name=name,
                    timezone=course.get("timezone", "America/Chicago"),
                    term_calendar=course.get("term_calendar", {}),
                    export=course.get("export", {}),
                    config={
                        **website,
                        "base_url": website.get("url"),
                    },
                )
            )
        for source in course.get("sources", []):
            if source.get("type") == "website":
                definitions.append(
                    WebsiteDefinition(
                        course_code=code,
                        course_name=name,
                        timezone=course.get("timezone", "America/Chicago"),
                        term_calendar=course.get("term_calendar", {}),
                        export=course.get("export", {}),
                        config=source,
                    )
                )
    return definitions


def adapter_kwargs(definition: WebsiteDefinition) -> dict[str, Any]:
    row = definition.config
    return {
        "name": row.get("source_identity_name") or row["name"],
        "course_code": definition.course_code,
        "base_url": row.get("base_url") or row.get("url"),
        "mode": row.get("mode", "http"),
        "link_selectors": row.get("link_selectors"),
        "include_patterns": row.get("include_patterns"),
        "exclude_patterns": row.get("exclude_patterns"),
        "extensions": row.get("extensions"),
        "public_pages": row.get("public_pages"),
        "auth_rules": row.get("auth_rules"),
        "discovery": row.get("discovery"),
        "download": row.get("download"),
        "timezone": definition.timezone,
        "term_calendar": definition.term_calendar,
    }


def course_export_configs(settings: Settings) -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    for definition in website_definitions(settings):
        if definition.export.get("enabled"):
            configs[definition.course_code] = definition.export
    return configs


def configured_auth_profiles(settings: Settings) -> dict[str, dict[str, str]]:
    profiles: dict[str, dict[str, str]] = {
        settings.canvas_credential_id: {
            "credential_id": settings.canvas_credential_id,
            "auth_type": "canvas_token",
            "probe_url": f"{settings.canvas_base_url.rstrip('/')}/api/v1/users/self/profile",
            "display_name": "Canvas",
        }
    }
    if not settings.canvas_base_url:
        profiles.clear()
    for definition in website_definitions(settings):
        for rule in definition.config.get("auth_rules", []):
            auth = rule.get("auth", {})
            credential_id = auth.get("credential_id")
            if not credential_id:
                continue
            profiles[credential_id] = {
                "credential_id": credential_id,
                "auth_type": auth.get("type", "unknown"),
                "probe_url": auth.get("probe_url", ""),
                "display_name": auth.get("display_name") or definition.course_code,
            }
    return profiles


def canvas_aliases(settings: Settings) -> dict[str, str]:
    """Return explicit Canvas course ID -> canonical course code mappings."""
    if not settings.courses_config.exists():
        return {}
    root = yaml.safe_load(settings.courses_config.read_text(encoding="utf-8")) or {}
    aliases: dict[str, str] = {}
    for row in root.get("course_aliases", []):
        code = row.get("canonical_course_code")
        if not code:
            continue
        for canvas_id in row.get("canvas_course_ids", []):
            aliases[str(canvas_id)] = str(code)
    return aliases
