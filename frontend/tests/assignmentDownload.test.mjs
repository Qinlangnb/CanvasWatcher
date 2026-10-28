import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const source = name => readFileSync(new URL('../src/' + name, import.meta.url), 'utf8');
const publicPath = ts.transpileModule(source('publicPath.ts'), {compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const publicPathUrl = 'data:text/javascript;base64,' + Buffer.from(publicPath).toString('base64');
const apiSource = ts.transpileModule(source('api.ts'), {compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
  .replace('"./publicPath"', JSON.stringify(publicPathUrl));
const apiUrl = 'data:text/javascript;base64,' + Buffer.from(apiSource).toString('base64');
const js = ts.transpileModule(source('assignmentDownload.ts'), {compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
  .replace('"./api"', JSON.stringify(apiUrl));
const {downloadAssignmentFile} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

test('local download uses file ID, filename and releases browser object URL', async () => {
  const saved = {fetch:globalThis.fetch,document:globalThis.document,window:globalThis.window,
    create:URL.createObjectURL,revoke:URL.revokeObjectURL};
  const calls=[]; const anchor={click(){calls.push('click')},remove(){calls.push('remove')}};
  globalThis.fetch=async (path, options)=>{calls.push(path);return {ok:true,blob:async()=>new Blob(['fixture'])}};
  globalThis.document={createElement:()=>anchor,body:{appendChild:()=>calls.push('append')}};
  globalThis.window={setTimeout:callback=>callback()};
  URL.createObjectURL=()=> 'blob:fixture';URL.revokeObjectURL=url=>calls.push(url);
  try {
    await downloadAssignmentFile({file_id:12,filename:'hw.pdf'});
    assert.deepEqual(calls,['/api/files/12/content','append','click','remove','blob:fixture']);
    assert.equal(anchor.download,'hw.pdf');
  } finally {globalThis.fetch=saved.fetch;globalThis.document=saved.document;globalThis.window=saved.window;URL.createObjectURL=saved.create;URL.revokeObjectURL=saved.revoke;}
});
test('failed downloads report actionable errors and do not save response bodies', async () => {
  const previous=globalThis.fetch;
  try {for (const status of [404,401,500]) {
    globalThis.fetch=async()=>({ok:false,status,blob:()=>{throw new Error('Must not save error body')}});
    await assert.rejects(()=>downloadAssignmentFile({file_id:1,filename:'hw.pdf'}), /no longer available|download failed/);
  }} finally {globalThis.fetch=previous;}
});
test('both Today sections offer download; multiple files, cancellation and interaction states are explicit', () => {
  const today=source('TodayView.tsx'), control=source('AssignmentFiles.tsx'), css=source('v071.css');
  assert.equal((today.match(/<AssignmentFiles/g)||[]).length,2);
  assert.match(control,/files.length > 1/);assert.match(control,/event.stopPropagation/);
  assert.match(control,/data-task-control/);assert.match(control,/disabled=\{busy\}/);
  assert.match(control,/Downloading/);assert.match(control,/\.abort\(\)/);
  assert.match(css,/\.assignment-files[^}]*flex-wrap: wrap/);
  assert.match(css,/\.assignment-files select[^}]*min-width: 0/);
});
