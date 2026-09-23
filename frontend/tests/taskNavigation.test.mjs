import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source = name => readFileSync(new URL('../src/' + name, import.meta.url), 'utf8');
const js = ts.transpileModule(source('taskNavigation.ts'), {compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {taskTarget, openTask, embeddedControl} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
test('exact stored targets preserved across all providers; missing or unsafe URLs are never guessed', () => {
  for (const source_url of ['https://canvas.example/courses/1/assignments/2','https://www.gradescope.com/courses/1/assignments/2','https://us.prairielearn.com/pl/course_instance/1/assessment/2','https://course.example/hw1.pdf']) assert.equal(taskTarget({source_url}),source_url);
  for (const source_url of [null,'','javascript:alert(1)','https://user:secret@x.test/a','https://x.test/a?token=secret','/courses/1']) assert.equal(taskTarget({source_url}),null);
});
test('navigation uses isolated new tab and no URL is a no-op', () => {
  const previous=globalThis.window, calls=[];
  globalThis.window={open:(...args)=>calls.push(args)};
  try { openTask({}); openTask({source_url:'https://course.example/hw1'}); assert.deepEqual(calls,[['https://course.example/hw1','_blank','noopener,noreferrer']]); }
  finally { globalThis.window=previous; }
});
test('embedded controls include provider details, sliders and menus', () => {
  const previous=globalThis.Element;
  globalThis.Element=class {closest(selector){return selector.includes(this.tag) ? this : null}};
  try { for (const tag of ['input','button','summary','details','[role="button"]','[data-task-control]']) {const el=new Element();el.tag=tag;assert.equal(embeddedControl(el),true);} assert.equal(embeddedControl(null),false); }
  finally {globalThis.Element=previous;}
});
test('Today and Timeline use shared resolver; timeline captures only after drag threshold', () => {
  const today=source('TodayView.tsx'), timeline=source('timeline.tsx'), css=source('v071.css');
  assert.match(today,/openTask\(item\)/); assert.match(today,/embeddedControl\(event.target\)/);
  assert.match(timeline,/openTask\(node.task\)/); assert.doesNotMatch(timeline,/onCourse\(node.task.course_id\)/);
  assert.match(timeline,/Math.hypot[\s\S]*?<= 6[\s\S]*?setPointerCapture/);
  assert.match(timeline,/onClickCapture/);
  assert.match(css,/prefers-reduced-motion/); assert.match(css,/focus-visible/);
  assert.match(css,/font-size: 1.25rem/); assert.match(css,/text-decoration: none/);
});
