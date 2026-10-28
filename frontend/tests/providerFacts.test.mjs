import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
function compile(name, deps={}) {
  let code=ts.transpileModule(readFileSync(new URL('../src/'+name,import.meta.url),'utf8'),
    {compilerOptions:{module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  for (const [name,url] of Object.entries({'react/jsx-runtime':import.meta.resolve('react/jsx-runtime'),...deps}))
    code=code.replaceAll(JSON.stringify(name),JSON.stringify(url));
  return 'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
}
const timezone=compile('timezone.ts',{'luxon':import.meta.resolve('luxon')});
const submission=compile('SubmissionStatus.tsx');
const {ProviderFacts}=await import(compile('ProviderFacts.tsx',{'./timezone':timezone,'./SubmissionStatus':submission}));
const row={provider:'canvas',source_item_id:1,due_at:null,submission_state:'submitted',
  description_summary:'Solve questions 1–4 and upload a PDF.',description_summary_status:'ready'};
const render=facts=>renderToStaticMarkup(createElement(ProviderFacts,{facts}));
test('unconfigured facts produce no section, copy or link',()=>{
  assert.equal(render([]),'');assert.equal(render(undefined),'');
});
test('AI description replaces duplicate provider link and is escaped',()=>{
  const html=render([row]);assert.match(html,/Solve questions/);assert.match(html,/Source facts/);
  assert.doesNotMatch(html,/<a |Open provider assignment/);
  assert.match(render([{...row,description_summary:'<script>bad()</script>'}]),/&lt;script&gt;/);
});
test('missing, pending, failed, untested connection states are truthful',()=>{
  for (const [status,copy] of Object.entries({pending:'Generating assignment summary',unavailable:'No assignment description',
    failed:'background generation failed',needs_connection:'Test the selected AI connection'})) {
    assert.ok(render([{...row,description_summary:null,description_summary_status:status}]).includes(copy));
  }
});
test('long descriptions preserve existing narrow-screen wrapping',()=>{
  const css=readFileSync(new URL('../src/responsive.css',import.meta.url),'utf8');
  assert.match(css,/\.provider-facts[^}]*min-width: 0; overflow-wrap: anywhere/);
});
