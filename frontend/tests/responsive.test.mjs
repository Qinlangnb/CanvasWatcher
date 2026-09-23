import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import postcss from 'postcss';

// Static cascade guards; live viewport acceptance is recorded separately.
const source = readFileSync(new URL('../src/responsive.css', import.meta.url), 'utf8');
const css = postcss.parse(source);
function declarations(selector, media) {
  const result = {};
  css.walkRules(selector, rule => {
    if (media && rule.parent.params !== media) return;
    rule.walkDecls(decl => { result[decl.prop] = decl.value; });
  });
  return result;
}
test('responsive layer follows historical shell and credential styles', () => {
  const main = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');
  const imports = [...main.matchAll(/import "(.+\.css)"/g)].map(match => match[1]);
  assert.equal(imports.at(-1), './responsive.css');
});
test('completed Today rows opt out of execution-card minimum height at every width', () => {
  const today = readFileSync(new URL('../src/TodayView.tsx', import.meta.url), 'utf8');
  assert.match(today, /className="work-card completed-work-card"/);
  assert.match(today, /className="cards completed-work-list"/);
  assert.equal(declarations('.work-card.completed-work-card')['min-height'], '0');
});
test('sidebar and main switch together at 900px, including 821–900 gap', () => {
  const sidebar = declarations('.app-shell > .app-sidebar', '(max-width: 900px)');
  const main = declarations('.app-shell > main', '(max-width: 900px)');
  assert.equal(sidebar.position, 'static');
  assert.equal(sidebar.height, 'auto');
  assert.equal(sidebar.width, '100%');
  assert.equal(main['margin-left'], '0');
  assert.equal(main.width, '100%');
});
test('narrow file titles and calendar overlays have explicit containment', () => {
  assert.equal(declarations('.file-copy strong')['overflow-wrap'], 'anywhere');
  assert.equal(declarations('.calendar-filters', '(max-width: 900px)').position, 'static');
  assert.equal(declarations('.calendar-options').position, 'relative');
  assert.equal(declarations('.calendar-options details').position, 'static');
  assert.equal(declarations('.visual-calendar .fc-daygrid-block-event .fc-event-main-frame', '(max-width: 480px)')['flex-direction'], 'column');
});
