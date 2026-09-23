import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
function compile(path) {
  let code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  code = code.replaceAll('"react/jsx-runtime"', JSON.stringify(import.meta.resolve("react/jsx-runtime")));
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
}
const { submissionStatus } = await import(compile("../src/SubmissionStatus.tsx"));
const { sourceStatusTone } = await import(compile("../src/SourceStatusBadge.tsx"));
test("submission facts use explicit positive versus unconfirmed labels", () => {
  for (const state of ["graded", "submitted", "GRADED"]) assert.equal(submissionStatus(state).tone, "positive");
  for (const state of [null, undefined, "unknown", "local_completed", "unsubmitted"]) {
    assert.equal(submissionStatus(state).tone, "unconfirmed");
    assert.equal(submissionStatus(state).label, "Submission not confirmed");
  }
  assert.equal(sourceStatusTone("DEGRADED"), "warning");
});
test("all task surfaces share submission semantics", () => {
  for (const name of ["App", "TodayView", "ProviderFacts", "timeline"]) {
    assert.match(readFileSync(new URL(`../src/${name}.tsx`, import.meta.url), "utf8"), /from "\.\/SubmissionStatus"/);
  }
});
