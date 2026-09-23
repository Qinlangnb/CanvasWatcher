import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const code = ts.transpileModule(readFileSync(new URL('../src/brokerMessages.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const { brokerMessage } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
test('broker terminal reasons have friendly actionable copy and unknown input is never echoed', () => {
  assert.match(brokerMessage('AUTH_LOGIN_TIMEOUT'), /timed out.*No credentials/);
  assert.match(brokerMessage('CANVAS_ACCOUNT_MISMATCH'), /different Canvas account/);
  assert.match(brokerMessage('AUTH_USER_CANCELLED'), /cancelled/);
  assert.doesNotMatch(brokerMessage('unexpected-secret'), /unexpected-secret/);
});
