import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

function compile(path, dependencies = {}) {
  let code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  for (const [specifier, url] of Object.entries({ "react/jsx-runtime": import.meta.resolve("react/jsx-runtime"), ...dependencies })) {
    code = code.replaceAll(JSON.stringify(specifier), JSON.stringify(url));
  }
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
}
const badgeUrl = compile("../src/SourceStatusBadge.tsx");
const { sourceStatusTone } = await import(badgeUrl);
const { CanvasCredentialSlots } = await import(compile("../src/CanvasCredentialSlots.tsx", { "./SourceStatusBadge": badgeUrl }));
const props = {
  busy: false, formatDate: () => "Sep 14, 18:46", onImport() {}, onRemove() {}, onClear() {},
  status: { connection_state: "ACTIVE", effective_method: "pat", backup_ready: true,
    credentials: { pat: { state: "VALID", present: true, generation: 5 },
      browser_session: { state: "VALID", present: true, generation: 9, last_verified_at: "2026-09-14T23:46:00Z" } } },
};

test("valid credentials use success tone, missing is not a failure", () => {
  for (const state of ["VALID", "valid", "ACTIVE", "HEALTHY"]) assert.equal(sourceStatusTone(state), "active");
  for (const state of ["MISSING", "DISABLED", "VERIFYING", "AUTH_REQUIRED", "PENDING_RECONCILIATION"]) assert.equal(sourceStatusTone(state), "warning");
  for (const state of ["READ_ONLY", "READ ONLY", "COMING SOON"]) assert.equal(sourceStatusTone(state), "neutral");
  for (const state of ["INVALID", "EXPIRED", "FAILED"]) assert.equal(sourceStatusTone(state), "danger");
});

test("sidebar uses the same status tone helper as credential badges", () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /import \{ sourceStatusTone as statusTone \} from "\.\/SourceStatusBadge"/);
  assert.doesNotMatch(app, /const statusTone\s*=/);
});

test("three compact slots keep distinct titles, states, metadata and themed actions", () => {
  const html = renderToStaticMarkup(createElement(CanvasCredentialSlots, props));
  assert.equal((html.match(/<article /g) ?? []).length, 3);
  assert.equal((html.match(/source-status-pill active/g) ?? []).length, 2);
  assert.match(html, /PAT credential/);
  assert.match(html, /OAuth credential/);
  assert.match(html, /Browser Session credential/);
  assert.match(html, /Expires: Unknown/);
  assert.match(html, /Verified: Sep 14, 18:46/);
  assert.equal((html.match(/class="ghost canvas-credential-action/g) ?? []).length, 5);
});

test("busy state disables all credential actions", () => {
  const html = renderToStaticMarkup(createElement(CanvasCredentialSlots, { ...props, busy: true }));
  assert.equal((html.match(/disabled=""/g) ?? []).length, 5);
});

test("missing credentials cannot be removed but remain importable", () => {
  const html = renderToStaticMarkup(createElement(CanvasCredentialSlots, { ...props, status: undefined }));
  assert.equal((html.match(/disabled=""/g) ?? []).length, 4);
  assert.match(html, /Unknown/);
});

test("remove callbacks retain per-slot generation guards and imports do not remove", () => {
  const removed = [], imported = [];
  const tree = CanvasCredentialSlots({ ...props, onRemove: (...args) => removed.push(args), onImport: method => imported.push(method) });
  const buttons = [];
  function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== "object") return;
    if (node.type === "button") buttons.push(node);
    walk(node.props?.children);
  }
  walk(tree);
  buttons.find(node => node.props["aria-label"] === "Import or replace PAT").props.onClick();
  assert.deepEqual(imported, ["pat"]);
  assert.deepEqual(removed, []);
  buttons.find(node => node.props["aria-label"] === "Remove PAT").props.onClick();
  buttons.find(node => node.props["aria-label"] === "Remove Browser Session").props.onClick();
  assert.deepEqual(removed, [["pat", 5], ["browser_session", 9]]);
});
