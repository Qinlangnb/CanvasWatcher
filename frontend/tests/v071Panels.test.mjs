import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

function moduleUrl(path, dependencies = {}) {
  let code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  for (const [name, url] of Object.entries({ "react/jsx-runtime": import.meta.resolve("react/jsx-runtime"), ...dependencies }))
    code = code.replaceAll(JSON.stringify(name), JSON.stringify(url));
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
}
function find(node, predicate) {
  if (Array.isArray(node)) return node.flatMap(child => find(child, predicate));
  if (!node || typeof node !== "object") return [];
  return [...(predicate(node) ? [node] : []), ...find(node.props?.children, predicate)];
}

test("source health explains partial errors and marks historical HTTP status unknown", async () => {
  const { SourceHealth } = await import(moduleUrl("../src/SourceHealth.tsx"));
  const html = renderToStaticMarkup(SourceHealth({ source: {
    health_summary: "Some resources could not be read. Available course content remains usable.",
    health_diagnostics: [{ resource: "page", code: "invalid_response", http_status: null, historical: true }],
  } }));
  assert.match(html, /remains usable/);
  assert.match(html, /page: invalid_response/);
  assert.match(html, /HTTP unknown · historical record/);
  assert.doesNotMatch(html, /undefined|HTTP 404/);
});

test("rule panel shows scope/audit and handles disable, remove, and API error", async () => {
  const state = [], refs = [];
  let si = 0, ri = 0, reject = false, refreshes = 0;
  const calls = [];
  const query = { data: { rules: [{ id: 7, enabled: true, removed: false, created_by: "AI", version: 2,
    match_count: 4, reason: "Repeated cosmetic title", rule: { course_id: 3, source_name: "Canvas", change_type: "CONTENT_UPDATED", field: "title", operator: "contains", pattern: "cosmetic" },
    audit: [{ actor: "AI", action: "update", version: 2, reason: "Repeated noise", created_at: "2026-09-22" }] }],
    recent_reviews: [{ change_id: 9, status: "SUPPRESSED", reason: "Repeated noise", matched_rule: { id: 7, version: 2 } }] },
    isError: false, async refetch() { refreshes++; } };
  globalThis.__v071Panel = { state, refs, query, calls,
    useState(value) { const index = si++; if (!(index in state)) state[index] = value; return [state[index], next => { state[index] = next; }]; },
    useRef(value) { const index = ri++; return refs[index] ?? (refs[index] = { current: value }); },
    async api(path, options) { calls.push([path, options?.method]); if (reject) throw Error("private error"); return {}; },
  };
  const fakeReact = `data:text/javascript,export const useState=globalThis.__v071Panel.useState,useRef=globalThis.__v071Panel.useRef`;
  const fakeQuery = `data:text/javascript,export const useQuery=()=>globalThis.__v071Panel.query`;
  const fakeApi = `data:text/javascript,export const api=(...a)=>globalThis.__v071Panel.api(...a),json=(method,body)=>({method,body:JSON.stringify(body)})`;
  const { ChangeFilters } = await import(moduleUrl("../src/ChangeFilters.tsx", {
    react: fakeReact, "@tanstack/react-query": fakeQuery, "./api": fakeApi,
  }));
  const render = () => { si = ri = 0; return ChangeFilters(); };
  try {
    let tree = render();
    const html = renderToStaticMarkup(tree);
    assert.match(html, /Course #3 · Canvas · CONTENT_UPDATED/);
    assert.match(html, /AI update · v2/);
    assert.match(html, /rule #7 v2/);
    find(tree, n => n.type === "button" && n.props.children === "Disable")[0].props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls[0], ["/api/settings/change-filters/7", "PATCH"]);
    assert.equal(refreshes, 1);
    tree = render();
    find(tree, n => n.type === "button" && n.props.children === "Remove")[0].props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls[1], ["/api/settings/change-filters/7", "DELETE"]);
    reject = true;
    tree = render();
    find(tree, n => n.type === "button" && n.props.children === "Disable")[0].props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.match(renderToStaticMarkup(render()), /role="alert">Filter could not be updated/);
  } finally { delete globalThis.__v071Panel; }
});
