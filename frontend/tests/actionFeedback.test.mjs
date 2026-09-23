import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import postcss from 'postcss';

const read = name => readFileSync(new URL('../src/' + name, import.meta.url), 'utf8');
const code = ts.transpileModule(read('actionRunner.ts'), { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { createActionRunner } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('async action gate synchronously blocks repeat clicks and reports busy then success', async () => {
  const states = [];
  const run = createActionRunner(state => states.push(state));
  let finish;
  let calls = 0;
  const task = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
  const first = run('Save', task);
  assert.equal(await run('Save', task), false);
  assert.equal(calls, 1);
  assert.equal(states[0].phase, 'busy');
  finish();
  assert.equal(await first, true);
  assert.equal(states.at(-1).phase, 'success');
});
test('failed action exposes its reason, releases gate, and cancelled actions never claim success', async () => {
  const states = [];
  const run = createActionRunner(state => states.push(state));
  assert.equal(await run('Move file', async () => { throw new Error('Network unavailable'); }), false);
  assert.equal(states.at(-1).phase, 'error');
  assert.match(states.at(-1).message, /Network unavailable/);
  assert.equal(await run('Delete', async () => false), false);
  assert.equal(states.at(-1).phase, 'idle');
  assert.equal(await run('Retry', async () => undefined), true);
});
test('shared states cover all button families without changing geometry; reduced motion stops animation', () => {
  const css = postcss.parse(read('interaction.css'));
  const selectors = [];
  css.walkRules(rule => selectors.push(rule.selector));
  assert.ok(selectors.includes('button'));
  assert.ok(selectors.includes('button:not(:disabled):active'));
  assert.ok(selectors.includes('button:disabled'));
  for (const token of ['.secondary', '.ghost', '.icon-button', '.settings-tabs', '.tabs', '.change-main'])
    assert.ok(selectors.some(selector => selector.includes(token) && selector.includes(':hover')));
  let reduced = false;
  css.walkAtRules('media', rule => {
    if (rule.params.includes('prefers-reduced-motion')) {
      reduced = true;
      assert.match(rule.toString(), /animation: none !important/);
      assert.match(rule.toString(), /transition: none !important/);
    }
  });
  assert.equal(reduced, true);
  assert.match(read('AIChatDrawer.tsx'), /prefers-reduced-motion/);
});
test('reviewed asynchronous actions are wired to the shared runner and visible feedback', () => {
  const settings = read('SettingsView.tsx');
  for (const operation of ['Preview calendars', 'Import calendars', 'Re-import calendar', 'Remove calendar import',
    'Delete event', 'Save override', 'Remove override', 'Create source'])
    assert.ok(settings.includes(`.run("${operation}"`), operation);
  for (const [file, names] of [
    ['App.tsx', ['notificationAction', 'changeAction']],
    ['FilesView.tsx', ['moveAction']],
    ['VisualCalendar.tsx', ['deleteAction']],
  ]) for (const name of names) {
    assert.ok(read(file).includes(`${name}.run(`));
    assert.ok(read(file).includes(`<ActionFeedback action={${name}}`));
  }
  assert.match(read('CommitButton.tsx'), /<ActionFeedback/);
});
test('re-import is a real keyboard button and all reviewed close icons have names', () => {
  const picker = read('FilePickerButton.tsx');
  assert.match(picker, /<button type="button"/);
  assert.match(picker, /input.current\?\.click\(\)/);
  assert.match(picker, /event.currentTarget.value = ""/);
  const settings = read('SettingsView.tsx');
  assert.doesNotMatch(settings, /<label className="secondary reimport-button"/);
  for (const label of ['Close source wizard', 'Close source editor', 'Close event editor', 'Remove override for'])
    assert.ok(settings.includes(label));
  assert.match(read('VisualCalendar.tsx'), /className="secondary" onClick=\{\(\) => edit\(warning.event\)/);
});
