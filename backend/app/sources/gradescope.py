"""Read-only Gradescope adapter; HTML compatibility is isolated separately."""

from app.sources.gradescope_html import parse_assignments, parse_courses
from app.sources.provider_web import ProviderWeb


class GradescopeAdapter:
    def __init__(self, session, course_id: str, course_source_id: int, transport=None, bindings=None):
        self.client = ProviderWeb(session, transport)
        self.provider_course_id = course_id
        self.course_source_id = course_source_id
        self.name = f"gradescope:{course_id}"
        self.errors = {}
        self.bindings = bindings or {}

    async def discover_courses(self):
        return parse_courses(await self.client.probe(), self.client.base)

    async def fetch_items(self, course, since=None):
        result = parse_assignments(await self.client.get(f"/courses/{self.provider_course_id}"),
                                   self.client.base, self.provider_course_id)
        self.errors = {"unidentified_rows": str(result.unidentified_rows)} if result.unidentified_rows else {}
        for item in result.items:
            facts = item.structured["provider_facts"]
            choice = self.bindings.get(facts["provider_assignment_id"])
            if choice == "separate":
                item.structured["confirmed_separate"] = True
            elif isinstance(choice, int) and not isinstance(choice, bool):
                item.structured["confirmed_task_id"] = choice
            # Never use a late cutoff as the regular due date or invent an extension.
            item.structured.update(due_at=facts["due_at"], due_at_state="known" if facts["due_at"] else "unknown",
                deadline_source_rank=0, deadline_precision="EXACT_DATETIME" if facts["due_at"] else None,
                source_deadline_text=facts["due_at"], points_possible=facts["max_score"],
                submission={"workflow_state": facts["submission_state"]})
        return result.items

    async def fetch_file(self, item):
        raise NotImplementedError("Gradescope file ingestion is outside V0.7.0 scope")
