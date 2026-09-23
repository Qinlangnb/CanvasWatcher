import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const code = ts.transpileModule(readFileSync(new URL("../src/api.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const { api } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);

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
