from typing import Final

NORMALIZED_FILE_TYPES: Final[tuple[str, ...]] = (
    "lecture",
    "homework",
    "discussion",
    "reading",
    "exam",
    "solution",
    "other",
)

_ALIASES: Final[dict[str, str]] = {
    "lecture_notes": "lecture",
    "lectures": "lecture",
    "assignments": "homework",
    "homework": "homework",
    "discussion": "discussion",
    "optional_reading": "reading",
    "reading": "reading",
    "exams": "exam",
    "exam": "exam",
    "solutions": "solution",
    "solution": "solution",
    "syllabus": "reading",
    "other": "other",
}


def normalize_file_type(value: str | None) -> str:
    normalized = (value or "other").strip().lower()
    if normalized in NORMALIZED_FILE_TYPES:
        return normalized
    return _ALIASES.get(normalized, "other")
