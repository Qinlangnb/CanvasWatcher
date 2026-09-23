import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, json } from "./api";
import { ActionFeedback, useAsyncAction } from "./ActionFeedback";
import type { Course } from "./types";

type Discovered = { external_id: string; course_code: string; name: string; term: string | null };
const post = (body: unknown) => ({ ...json("POST", body), headers: { "Content-Type": "application/json", "X-AW-Broker": "1" } });

export function ProviderCourses({ connectionId }: { connectionId: number }) {
  const [rows, setRows] = useState<Discovered[] | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const courses = useQuery({ queryKey: ["courses"], queryFn: () => api<Course[]>("/api/courses") });
  const action = useAsyncAction();
  const client = useQueryClient();
  return <section aria-label="Provider course mapping">
    <div className="card-actions">
      <button className="secondary" disabled={action.busy} onClick={() => void action.run("Discover courses", async () => {
        setRows(await api<Discovered[]>(`/api/provider-sources/${connectionId}/courses`));
      })}>Discover courses</button>
      <button className="secondary" disabled={action.busy} onClick={() => void action.run("Sync mapped courses", async () => {
        const result = await api<{ state: string; errors: string[] }>(`/api/provider-sources/${connectionId}/sync`, post({}));
        await Promise.all(["source-connections", "tasks", "today", "changes"].map(key => client.invalidateQueries({ queryKey: [key] })));
        if (result.errors.length) throw new Error("Some provider rows could not be verified; existing data was preserved.");
      })}>Sync mapped courses</button>
    </div>
    {rows?.length === 0 && <p>No student courses were visible. Existing courses were not removed.</p>}
    {rows?.map(row => <div key={row.external_id} className="settings-panel">
      <p>{row.course_code} · {row.name}{row.term ? ` · ${row.term}` : ""}</p>
      <label>Link to existing course<select value={mapping[row.external_id] ?? ""} disabled={action.busy}
        onChange={event => setMapping(old => ({ ...old, [row.external_id]: event.target.value }))}>
        <option value="">Choose the matching course</option>
        {courses.data?.map(course => <option key={course.id} value={course.id}>{course.course_code} · {course.term ?? course.name}</option>)}
      </select></label>
      <button className="secondary" disabled={action.busy || !mapping[row.external_id]} onClick={() => void action.run("Save course mapping", async () => {
        await api(`/api/provider-sources/${connectionId}/course-mappings`, post({ provider_course_id: row.external_id, course_id: Number(mapping[row.external_id]) }));
      })}>Confirm course mapping</button>
    </div>)}
    <ActionFeedback action={action} />
  </section>;
}
