import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

WEEKDAY_OFFSETS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
}
HOMEWORK_EVENT = re.compile(r"\b(?:HW|Homework)\s*0*(\d+)\s+(assigned|due)\b", re.IGNORECASE)


@dataclass
class ScheduleHomework:
    number: int
    assigned_date: date | None = None
    due_date: date | None = None
    assignment_resource_url: str | None = None
    solution_resource_url: str | None = None
    assigned_text: str | None = None
    due_text: str | None = None

    @property
    def source_key(self) -> str:
        return f"homework:{self.number}"


def _link_for(cell: Tag, number: int, *, solutions: bool) -> str | None:
    marker = f"hw-{number:02d}"
    for link in cell.find_all("a", href=True):
        href = str(link["href"])
        lowered = href.lower()
        if marker not in lowered:
            continue
        is_solution = "solution" in lowered
        if is_solution == solutions:
            return href
    return None


def parse_phys225_schedule(
    html: bytes | str,
    *,
    week_1_monday: date | str,
    schedule_url: str,
) -> list[ScheduleHomework]:
    """Parse PHYS225 homework events from table structure without LLM/date guessing."""
    try:
        anchor = (
            date.fromisoformat(week_1_monday)
            if isinstance(week_1_monday, str)
            else week_1_monday
        )
    except ValueError:
        return []
    if not isinstance(anchor, date) or anchor.weekday() != 0:
        return []
    soup = BeautifulSoup(html, "lxml")
    table = next(
        (
            candidate
            for candidate in soup.find_all("table")
            if "homework" in candidate.get_text(" ", strip=True).lower()
        ),
        None,
    )
    if table is None:
        return []

    current_week: int | None = None
    homework: dict[int, ScheduleHomework] = {}
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells or all(cell.name == "th" for cell in cells):
            continue
        first_text = cells[0].get_text(" ", strip=True)
        first_number = re.fullmatch(r"\d+", first_text)
        offset = 0
        if first_number:
            current_week = int(first_number.group())
            offset = 1
        if current_week is None or not 1 <= current_week <= 53 or len(cells) <= offset + 3:
            continue
        weekday_text = cells[offset].get_text(" ", strip=True).lower()
        weekday = next((name for name in WEEKDAY_OFFSETS if name in weekday_text), None)
        if weekday is None:
            continue
        event_date = anchor + timedelta(
            days=(current_week - 1) * 7 + WEEKDAY_OFFSETS[weekday]
        )
        homework_cell = cells[offset + 3]
        homework_text = homework_cell.get_text(" ", strip=True)
        for match in HOMEWORK_EVENT.finditer(homework_text):
            number = int(match.group(1))
            action = match.group(2).lower()
            item = homework.setdefault(number, ScheduleHomework(number=number))
            event_text = f"Week {current_week}, {weekday.title()}: {match.group(0)}"
            if action == "assigned":
                item.assigned_date = event_date
                item.assigned_text = event_text
                href = _link_for(homework_cell, number, solutions=False)
                if href:
                    item.assignment_resource_url = urljoin(schedule_url, href)
            else:
                item.due_date = event_date
                item.due_text = event_text
                href = _link_for(homework_cell, number, solutions=True)
                if href:
                    item.solution_resource_url = urljoin(schedule_url, href)
    return [homework[number] for number in sorted(homework)]
