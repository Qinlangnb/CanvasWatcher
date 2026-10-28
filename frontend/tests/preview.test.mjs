import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (file, replacements = {}) => {
  let code = ts.transpileModule(readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ESNext },
  }).outputText;
  for (const [name, url] of Object.entries(replacements)) code = code.replaceAll(JSON.stringify(name), JSON.stringify(url));
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
};
const { apiFetch, isPreviewAccount } = await import(moduleUrl("api.ts", {
  "./publicPath": moduleUrl("publicPath.ts"),
  "./previewData": moduleUrl("previewData.ts"),
}));

test("preview logins read sample data and send nothing to the API", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  try {
    globalThis.fetch = async (...args) => { calls.push(args); throw new Error("network used"); };
    globalThis.document = { cookie: "", querySelector: (selector) => selector.includes("aw-preview") ? {} : null };
    assert.equal(isPreviewAccount(), true);
    const courses = await (await apiFetch("/api/courses")).json();
    assert.ok(courses.length >= 3);
    const tasks = await (await apiFetch(`/api/tasks?course_id=${courses[0].id}`)).json();
    assert.ok(tasks.length > 0 && tasks.every((task) => task.course_id === courses[0].id));
    assert.ok((await (await apiFetch("/api/files")).json()).length > 0);
    assert.ok((await (await apiFetch("/api/today")).json()).work.length > 0);
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      const response = await apiFetch("/api/sync/run", { method });
      assert.equal(response.status, 403);
    }
    assert.equal((await apiFetch("/api/unknown")).status, 404);
    assert.deepEqual(calls, []);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
});
