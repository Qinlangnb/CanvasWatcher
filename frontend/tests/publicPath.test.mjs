import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/publicPath.ts", import.meta.url), "utf8");
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const { normalizeBasePath, withBasePath, toInternalPath } = await import(
  `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`
);

test("root base preserves local development routes and URLs", () => {
  assert.equal(normalizeBasePath("/"), "");
  assert.equal(withBasePath("/api/courses", "/"), "/api/courses");
  assert.equal(withBasePath("/courses/42?tab=Files", "/"), "/courses/42?tab=Files");
  assert.equal(toInternalPath("/courses/42", "/"), "/courses/42");
});

test("configured base prefixes API and client routes without changing query or fragment", () => {
  assert.equal(normalizeBasePath("/watcher/"), "/watcher");
  assert.equal(withBasePath("/api/courses?term=all", "/watcher"), "/watcher/api/courses?term=all");
  assert.equal(withBasePath("/courses/42#files", "/watcher"), "/watcher/courses/42#files");
  assert.equal(withBasePath("/watcher/api/files/12/content", "/watcher"), "/watcher/api/files/12/content");
  assert.equal(toInternalPath("/watcher", "/watcher"), "/");
  assert.equal(toInternalPath("/watcher/", "/watcher"), "/");
  assert.equal(toInternalPath("/watcher/courses/42", "/watcher"), "/courses/42");
  assert.equal(toInternalPath("/watcher/settings/sources?edit=3", "/watcher"), "/settings/sources?edit=3");
});

test("external and non-path URLs are left alone", () => {
  assert.equal(withBasePath("https://canvas.example/courses/1", "/watcher"), "https://canvas.example/courses/1");
  assert.equal(withBasePath("//cdn.example/app.js", "/watcher"), "//cdn.example/app.js");
  assert.equal(withBasePath("academicwatcher://start", "/watcher"), "academicwatcher://start");
});

test("invalid configured bases fail instead of creating ambiguous routes", () => {
  for (const base of ["watcher", "/watcher?tab=Today", "/watcher//nested", "/watcher/../other"]) {
    assert.throws(() => normalizeBasePath(base), /public base/i);
  }
});
