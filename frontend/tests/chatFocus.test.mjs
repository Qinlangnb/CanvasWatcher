import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

test('chat polling renders preserve composer focus and Escape uses the latest callback', async () => {
  const refs = [], effects = [];
  let refIndex = 0, effectIndex = 0;
  const pending = [];
  const harness = {
    useRef(value) { return refs[refIndex++] ?? (refs[refIndex - 1] = { current: value }); },
    useEffect(fn, deps) {
      const index = effectIndex++;
      if (!effects[index] || deps.some((v, i) => v !== effects[index].deps[i])) {
        pending.push(() => { effects[index]?.cleanup?.(); effects[index] = { deps, cleanup: fn() }; });
      }
    },
  };
  const previousGlobals = Object.fromEntries(['window', 'document', 'HTMLElement', '__focusHarness'].map(k => [k, globalThis[k]]));
  const listeners = new Map();
  class Element { isConnected = true; calls = 0; focus() { this.calls++; } }
  const opener = new Element(), composer = new Element();
  globalThis.__focusHarness = harness;
  globalThis.HTMLElement = Element;
  globalThis.document = { activeElement: opener };
  globalThis.window = { addEventListener: (k, f) => listeners.set(k, f), removeEventListener: k => listeners.delete(k) };
  try {
    const fakeReact = 'data:text/javascript;base64,' + Buffer.from('export const {useRef,useEffect} = globalThis.__focusHarness;').toString('base64');
    const code = ts.transpileModule(readFileSync(new URL('../src/useDrawerFocus.ts', import.meta.url), 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.ESNext },
    }).outputText.replace('"react"', JSON.stringify(fakeReact));
    const { useDrawerFocus } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
    const target = { current: composer };
    const render = (open, close) => { refIndex = effectIndex = 0; useDrawerFocus(open, close, target); while (pending.length) pending.shift()(); };
    let oldCalls = 0, newCalls = 0;
    render(false, () => oldCalls++);
    assert.equal(composer.calls, 0);
    render(true, () => oldCalls++);
    assert.equal(composer.calls, 1);
    for (let i = 0; i < 20; i++) render(true, () => newCalls++);
    assert.equal(composer.calls, 1, 'background polling must not refocus');
    listeners.get('keydown')({ key: 'Escape', isComposing: true });
    assert.equal(newCalls, 0);
    listeners.get('keydown')({ key: 'Escape', isComposing: false });
    assert.equal(newCalls, 1);
    assert.equal(oldCalls, 0);
    render(false, () => newCalls++);
    assert.equal(opener.calls, 1);
    assert.equal(listeners.size, 0);
  } finally {
    for (const [key, value] of Object.entries(previousGlobals)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});

test('all non-Canvas providers appear in sidebar; Canvas broker login is not inside credential form', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /const otherConnections = [\s\S]*?row\.source_type !== "canvas"/);
  assert.match(app, /otherConnections\.map/);
  const settings = readFileSync(new URL('../src/SettingsView.tsx', import.meta.url), 'utf8');
  const card = settings.slice(settings.indexOf('function ConnectionCard('), settings.indexOf('function SourceWizard('));
  assert.match(card, /connection\.source_type === "canvas" && profile &&/);
  assert.match(card, /<BrowserSignIn credentialId=\{profile.credential_id\}/);
  const credentialForm = settings.slice(settings.indexOf('function CredentialForm('));
  assert.doesNotMatch(credentialForm, /<BrowserSignIn/);
});
