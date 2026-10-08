import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/brokerLaunch.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { brokerReady, launchBroker, BROKER_LAUNCH_URI, BROKER_START_TIMEOUT_MS } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('process-only health is not ready; callback must be ready too', () => {
  assert.equal(brokerReady(undefined), false);
  assert.equal(brokerReady({ protocol_version: 1 }), false);
  assert.equal(brokerReady({ protocol_version: 1, backend_ready: false }), false);
  assert.equal(brokerReady({ protocol_version: 2, backend_ready: true }), false);
  assert.equal(brokerReady({ protocol_version: 1, backend_ready: true }), true);
});

test('web launch uses a constant start-only URI with no page/token data', () => {
  const previous = globalThis.window;
  try {
    globalThis.window = { location: { href: '' } };
    launchBroker();
    assert.equal(window.location.href, 'academicwatcher://start');
    assert.equal(BROKER_LAUNCH_URI, 'academicwatcher://start');
  } finally { globalThis.window = previous; }
});

test('Start broker precedes Check broker; setup instructions removed and wait bounded', () => {
  const ui = readFileSync(new URL('../src/AuthBroker.tsx', import.meta.url), 'utf8');
  assert.ok(ui.indexOf('"Start broker"') < ui.indexOf('>Check broker<'));
  assert.doesNotMatch(ui, /Show setup instructions|See setup instructions|py -3\.12|ExecutionPolicy/);
  assert.equal(BROKER_START_TIMEOUT_MS, 60_000);
  assert.match(ui, /Date\.now\(\) < deadline/);
  assert.match(ui, /generation\.current !== attempt/);
  assert.match(ui, /wake\.current\?\.\(\)/);
  assert.match(ui, /disabled=\{!url \|\| launching \|\| ready\}/);
});
