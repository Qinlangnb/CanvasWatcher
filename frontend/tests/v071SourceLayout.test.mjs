import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import postcss from 'postcss';

const css = postcss.parse(readFileSync(new URL('../src/v071.css', import.meta.url), 'utf8'));
function declarations(selector) {
  const result = {};
  css.walkRules(selector, rule => rule.walkDecls(decl => { result[decl.prop] = decl.value; }));
  return result;
}

test('source action rows wrap and keep usable button height', () => {
  const row = declarations('.source-action-row, .source-connection-card > .card-actions');
  const button = declarations('.source-action-row button, .source-connection-card > .card-actions button');
  assert.equal(row.display, 'flex');
  assert.equal(row['flex-wrap'], 'wrap');
  assert.equal(button['min-height'], '2.75rem');
  assert.equal(button['overflow-wrap'], 'anywhere');
});

test('credential tabs wrap without forcing narrow-screen overflow', () => {
  const tabs = declarations('.credential-form .tabs');
  const button = declarations('.credential-form .tabs button');
  assert.equal(tabs['flex-wrap'], 'wrap');
  assert.equal(button['min-width'], '0');
  assert.equal(button['max-width'], '100%');
  assert.equal(button['white-space'], 'normal');
  const imports = [...readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8')
    .matchAll(/import "(.+\.css)"/g)].map(match => match[1]);
  assert.equal(imports.at(-1), './responsive.css');
});
