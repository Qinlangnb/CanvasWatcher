import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function compile(file, dependencies = {}) {
  let code = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext },
  }).outputText;
  for (const [name, url] of Object.entries(dependencies)) code = code.replaceAll(JSON.stringify(name), JSON.stringify(url));
  return `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`;
}
const { setTaskIgnored } = await import(compile('../src/taskActions.ts', { './api': compile('../src/api.ts') }));
test('ignore and undo use the backend POST contracts and return saved state', async () => {
  const previous = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, init) => {
      calls.push([url, init.method]);
      return { ok: true, json: async () => ({ ignored_at: url.endsWith('/unignore') ? null : 'test-time' }) };
    };
    assert.equal((await setTaskIgnored(42, true)).ignored_at, 'test-time');
    assert.equal((await setTaskIgnored(42, false)).ignored_at, null);
    assert.deepEqual(calls, [['/api/tasks/42/ignore', 'POST'], ['/api/tasks/42/unignore', 'POST']]);
  } finally { globalThis.fetch = previous; }
});
test('failed undo rejects so the UI can show the error instead of pretending success', async () => {
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: false, status: 404, json: async () => ({ detail: 'Task not found' }) });
    await assert.rejects(setTaskIgnored(42, false), /Task not found/);
  } finally { globalThis.fetch = previous; }
});
test('course actions wire undo to false and ignore to true with a busy guard', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /onClick=\{\(\) => changeIgnore\(task, false\)\}/);
  assert.match(app, /onClick=\{\(\) => changeIgnore\(task, true\)\}/);
  assert.equal((app.match(/disabled=\{changingIgnore !== null\}/g) ?? []).length, 2);
});
test('Today reopen reports a stale revision and releases the pending guard', () => {
  const today = readFileSync(new URL('../src/TodayView.tsx', import.meta.url), 'utf8');
  assert.match(today, /disabled=\{reopening !== null\}/);
  assert.match(today, /if \(!result.applied\) setMessage\("Task changed elsewhere; refreshed its saved state\."\)/);
  assert.match(today, /finally \{ setReopening\(null\); \}/);
});
