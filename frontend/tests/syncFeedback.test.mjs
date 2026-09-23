import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const code = ts.transpileModule(readFileSync(new URL('../src/syncFeedback.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const { syncFailureMessage, syncResultFeedback } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));

test('provider auth/mapping errors are actionable and unknown errors never echo', () => {
  assert.match(syncFailureMessage(new Error('AUTH_REQUIRED')), /reconnect/);
  assert.match(syncFailureMessage(new Error('PROVIDER_COURSE_MAPPING_REQUIRED')), /map at least one course/);
  assert.doesNotMatch(syncFailureMessage(new Error('private backend detail')), /private backend detail/);
});
test('200 responses with partial errors are not unconditional success', () => {
  const partial = syncResultFeedback({ state: 'completed', errors: ['private provider error'] });
  assert.equal(partial.failed, true);
  assert.match(partial.message, /with errors/);
  assert.doesNotMatch(partial.message, /private provider error|successfully/);
  assert.equal(syncResultFeedback({ state: 'FAILED', errors: [] }).failed, true);
  assert.equal(syncResultFeedback({ state: 'completed', errors: [] }).failed, false);
});
test('quick panel retains endpoint result and displays accessible failure feedback', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /const result = await api\(`\/api\/provider-sources/);
  assert.match(app, /syncResultFeedback\(await sync\(\)\)/);
  assert.match(app, /syncFailureMessage\(error\)/);
  assert.match(app, /role=\{syncFailed \? "alert" : "status"\}/);
});
