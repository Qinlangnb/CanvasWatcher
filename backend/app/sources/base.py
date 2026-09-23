from datetime import datetime
from typing import Protocol

from app.db import Course
from app.schemas import DiscoveredCourse, RawSourceItem


class SourceAdapter(Protocol):
    name: str

    async def discover_courses(self) -> list[DiscoveredCourse]: ...
    async def fetch_items(self, course: Course, since: datetime | None = None) -> list[RawSourceItem]: ...
    async def fetch_file(self, item: RawSourceItem) -> bytes: ...

