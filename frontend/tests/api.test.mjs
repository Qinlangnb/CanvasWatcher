import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const publicPathSource = readFileSync(new URL("../src/publicPath.ts", import.meta.url), "utf8");
const publicPathCode = ts.transpileModule(publicPathSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const publicPathUrl = `data:text/javascript;base64,${Buffer.from(publicPathCode).toString("base64")}`;
const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
const apiCode = ts.transpileModule(apiSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const code = apiCode.replace('"./publicPath"', JSON.stringify(publicPathUrl));
const { api, apiFetch, readCsrfCookie } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const prefixedPublicPathCode = ts.transpileModule(
  publicPathSource.replace('.env?.BASE_URL ?? "/"', '.env?.BASE_URL ?? "/watcher/"'),
  { compilerOptions: { module: ts.ModuleKind.ESNext } },
).outputText;
const prefixedPublicPathUrl = `data:text/javascript;base64,${Buffer.from(prefixedPublicPathCode).toString("base64")}`;
const prefixedApiUrl = `data:text/javascript;base64,${Buffer.from(
  apiCode.replace('"./publicPath"', JSON.stringify(prefixedPublicPathUrl)),
).toString("base64")}`;
const { apiFetch: prefixedApiFetch } = await import(prefixedApiUrl);

test("validation messages exclude submitted input and exception context", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: false, status: 422,
      json: async () => ({ detail: [{ msg: "Unknown timezone", input: "synthetic-private-input",
        ctx: { error: "hidden" } }] }) });
    await assert.rejects(api("/fixture"), error => error.message === "Unknown timezone");
  } finally { globalThis.fetch = original; }
});

for (const [name, body, expected] of [
  ["string detail", { detail: "Access denied" }, "Access denied"],
  ["object detail", { detail: { message: "Try again" } }, "Try again"],
  ["body fallback", { message: "Unavailable" }, "Unavailable"],
  ["empty detail list", { detail: [] }, "Request failed (422)"],
  ["multiple safe messages", { detail: [null, { msg: "First" }, { input: "hidden" },
    { msg: "Second", ctx: "hidden" }] }, "First; Second"],
]) {
  test(name, async () => {
    const original = globalThis.fetch;
    try {
      globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => body });
      await assert.rejects(api("/fixture"), error => error.message === expected);
    } finally { globalThis.fetch = original; }
  });
}

test("API fetch keeps the root development path and applies the configured public base", async () => {
  const previous = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (path) => {
      calls.push(path);
      return { ok: true, status: 200, json: async () => null };
    };
    await apiFetch("/api/courses");
    await prefixedApiFetch("/api/courses");
    assert.deepEqual(calls, ["/api/courses", "/watcher/api/courses"]);
  } finally {
    globalThis.fetch = previous;
  }
});

test("mutating same-origin API requests send the readable CSRF cookie while reads and external requests do not", async () => {
  const previous = { fetch: globalThis.fetch, document: globalThis.document };
  const calls = [];
  try {
    globalThis.document = { cookie: "session=fixture; aw_csrf=csrf%2Ftoken%3Dpart" };
    globalThis.fetch = async (path, init) => {
      calls.push({ path, method: init.method ?? "GET", headers: new Headers(init.headers) });
      return { ok: true, status: 200, json: async () => null };
    };
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      await apiFetch("/api/tasks/1", { method, headers: { "X-Existing": "preserved" } });
    }
    await apiFetch("/api/tasks");
    await apiFetch("https://provider.example/submit", { method: "POST" });
    await prefixedApiFetch("/api/tasks", { method: "POST" });
    assert.equal(readCsrfCookie("other=1; aw_csrf=csrf%2Ftoken%3Dpart"), "csrf/token=part");
    assert.equal(readCsrfCookie("other=1"), null);
    for (const call of calls.slice(0, 4)) {
      assert.equal(call.headers.get("X-AW-CSRF"), "csrf/token=part");
      assert.equal(call.headers.get("X-Existing"), "preserved");
    }
    assert.equal(calls[4].headers.has("X-AW-CSRF"), false);
    assert.equal(calls[5].headers.has("X-AW-CSRF"), false);
    assert.equal(calls[5].path, "https://provider.example/submit");
    assert.equal(calls[6].path, "/watcher/api/tasks");
    assert.equal(calls[6].headers.get("X-AW-CSRF"), "csrf/token=part");
  } finally {
    globalThis.fetch = previous.fetch;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
  }
});
