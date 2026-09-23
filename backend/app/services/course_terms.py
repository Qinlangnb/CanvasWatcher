import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.timezone import as_uiuc

_SEASON_RANK = {"winter": 1, "spring": 2, "summer": 3, "fall": 4}


@dataclass(frozen=True)
class NormalizedTerm:
    term_id: str | None
    display_name: str | None
    start_at: datetime | None
    end_at: datetime | None
    sort_key: str


@dataclass(frozen=True)
class CourseDisplay:
    course_code: str
    name: str
    section: str | None


def _aware(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def normalize_term(
    raw_name: str | None,
    *,
    term_id: str | int | None = None,
    start_at: datetime | str | None = None,
    end_at: datetime | str | None = None,
) -> NormalizedTerm:
    raw = re.sub(r"\s+", " ", (raw_name or "").strip()) or None
    start = _aware(start_at)
    end = _aware(end_at)
    display = raw
    year: int | None = None
    rank = 0
    if raw:
        match = re.search(
            r"(?i)\b(spring|summer|fall|winter)\b\s*[-,/]?\s*(20\d{2})|"
            r"\b(20\d{2})\b\s*[-,/]?\s*\b(spring|summer|fall|winter)\b",
            raw,
        )
        if match:
            season = (match.group(1) or match.group(4)).title()
            year = int(match.group(2) or match.group(3))
            rank = _SEASON_RANK[season.lower()]
            display = f"{season} {year}"
    date_basis = end or start
    if year is not None:
        sort_key = f"{year:04d}-{rank}"
    elif date_basis is not None:
        sort_key = f"{as_uiuc(date_basis).strftime('%Y%m%d')}-0"
    else:
        sort_key = f"0000-0-{(display or 'Unknown').casefold()}"
    stable_id = str(term_id) if term_id is not None else (display or raw)
    return NormalizedTerm(stable_id, display, start, end, sort_key)


def normalize_course_display(
    raw_code: str,
    raw_name: str,
    term_name: str | None = None,
) -> CourseDisplay:
    code_text = re.sub(r"\s+", " ", (raw_code or "").strip())
    # Canvas sometimes uses the entire term-prefixed title as course_code.
    # A season/year is metadata, not a department/course-number pair.
    code_match = next(
        (match for match in re.finditer(
            r"(?i)(?<![A-Z])([A-Z]{2,6})[\s_-]?(\d{2,4})(?!\d)", code_text
        ) if not (
            match.group(1).lower() in _SEASON_RANK
            and re.fullmatch(r"20\d{2}", match.group(2))
        )),
        None,
    )
    code = (
        f"{code_match.group(1).upper()} {code_match.group(2)}"
        if code_match
        else code_text
    )
    parts = [part.strip() for part in re.split(r"\s+-\s+|(?<=\d)-|-(?=[A-Za-z])", raw_name or "") if part.strip()]
    section = next((part for part in parts if re.match(r"(?i)^section\b", part)), None)
    ignored = {value.casefold() for value in (term_name, code) if value}
    descriptive = [
        part
        for part in parts
        if part.casefold() not in ignored
        and not re.fullmatch(r"(?i)(?:spring|summer|fall|winter)\s+20\d{2}", part)
        and not re.match(r"(?i)^section\b", part)
        and normalize_course_display_code(part) != normalize_course_display_code(code)
    ]
    name = " - ".join(descriptive).strip() if descriptive else (raw_name or code)
    return CourseDisplay(code or raw_code, name, section)


def normalize_course_display_code(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def choose_default_term(terms: list[NormalizedTerm], now: datetime | None = None) -> str | None:
    if not terms:
        return None
    current = as_uiuc(now or datetime.now(UTC))
    containing = [
        term
        for term in terms
        if term.start_at
        and term.end_at
        and as_uiuc(term.start_at) <= current <= as_uiuc(term.end_at)
    ]
    candidates = containing or terms
    return max(candidates, key=lambda term: term.sort_key).term_id


def should_archive_new_course(
    term: NormalizedTerm,
    *,
    course_end_at: datetime | str | None = None,
    now: datetime | None = None,
) -> bool:
    current = as_uiuc(now or datetime.now(UTC))
    end = term.end_at or _aware(course_end_at)
    return bool(end and as_uiuc(end) < current)
