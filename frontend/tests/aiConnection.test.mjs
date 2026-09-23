import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function moduleUrl(source) {
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
  return `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`;
}
const apiUrl = moduleUrl(readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8'));
const source = readFileSync(new URL('../src/aiConnection.ts', import.meta.url), 'utf8').replace('"./api"', JSON.stringify(apiUrl));
const { saveAIConnection, refreshAIConnection } = await import(moduleUrl(source));

test('provider is saved before importing its bound key, then key is cleared', async () => {
  const calls = [];
  await saveAIConnection({ provider: 'deepseek', model: ' model ', key: ' fixture-key ' },
    () => calls.push('cleared'), async (path, init) => calls.push([path, JSON.parse(init.body)]));
  assert.deepEqual(calls, [
    ['/api/settings/ai', { provider: 'deepseek', model_id: 'model' }],
    ['/api/settings/ai/credential', { provider: 'deepseek', api_key: 'fixture-key' }],
    'cleared',
  ]);
});
test('failed settings save never sends a key and failed key import retains draft', async () => {
  for (const stage of [1, 2]) {
    let calls = 0, cleared = false;
    await assert.rejects(saveAIConnection({ provider: 'deepseek', model: '', key: 'fixture' },
      () => { cleared = true; }, async () => { if (++calls === stage) throw new Error('fixture failure'); }));
    assert.equal(calls, stage);
    assert.equal(cleared, false);
  }
});
test('blank key preserves existing credential and status is fetched separately', async () => {
  const paths = [];
  await saveAIConnection({ provider: 'ollama', model: 'local', key: '' }, () => assert.fail(),
    async path => paths.push(path));
  assert.deepEqual(paths, ['/api/settings/ai']);
  assert.deepEqual(await refreshAIConnection(async path => ({ path, chat_ready: true })),
    { path: '/api/settings/ai', chat_ready: true });
});
test('UI has fixed provider selector, visible errors, busy guard and post-test refresh', () => {
  const ui = readFileSync(new URL('../src/AISettings.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(ui, /type="url"|setBase/);
  assert.match(ui, /Provider \/ region/);
  assert.match(ui, /role="alert" className="form-message error"/);
  assert.match(ui, /if \(inFlight.current\) return/);
  assert.match(ui, /fieldset disabled=\{busy/);
  assert.match(ui, /finally \{[\s\S]*refreshAIConnection\(\)/);
});
