import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
async function loadSource(name) {
  let code = ts.transpileModule(readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  code = code.replace(/from "([^"]+)"/g, (_, name) => `from ${JSON.stringify(pathToFileURL(require.resolve(name)).href)}`);
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
}
const { ChatMarkdown } = await loadSource('ChatMarkdown.tsx');
const { parseChatResponse, chatErrorMessage } = await loadSource('chatResponse.ts');
const render = content => renderToStaticMarkup(createElement(ChatMarkdown, { content }));

test('chat renders Markdown headings, lists, tables and code', () => {
  const html = render('# Title\n\n**bold**\n\n- one\n- two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```js\nconst x = 1;\n```');
  for (const tag of ['<h1>', '<strong>', '<ul>', '<table>', '<pre>']) assert.ok(html.includes(tag), tag);
  assert.match(html, /chat-table-scroll/);
});

test('inline and display LaTeX render and malformed math does not crash', () => {
  assert.match(render('Value $x^2$.'), /class="katex"/);
  assert.match(render('$$\n\\frac{1}{2}\n$$'), /katex-display/);
  assert.doesNotThrow(() => render('$\\notARealCommand{broken$'));
  assert.doesNotMatch(render('`$x^2$`'), /class="katex"/);
});

test('model HTML, scripts, images and trusted LaTeX commands cannot execute or fetch', () => {
  const html = render('<script>alert(1)</script>\n\n<img src="https://evil.test/pixel">\n\n![pixel](https://evil.test/pixel)\n\n[x](javascript:alert%281%29)\n\n$\\href{javascript:alert(1)}{x}$');
  assert.doesNotMatch(html, /<script|<img|href="javascript:|src="https:/);
  assert.match(html, /Image omitted/);
  assert.match(render('[safe](https://example.com)'), /rel="noopener noreferrer"/);
});

test('malformed API responses fail before a React state updater uses map', () => {
  for (const value of [null, {}, { tool_results: null }, { tool_results: {} }]) {
    assert.throws(() => parseChatResponse(value), /invalid response/);
  }
  const valid = { conversation_id: 'one', message: '**OK**', tool_results: [], confirmation: null, error: null };
  assert.deepEqual(parseChatResponse(valid), valid);
  assert.match(chatErrorMessage(new TypeError('Failed to fetch')), /connect to AI Chat/);
});

test('chat has local scrolling for wide content and multi-round proxy timeout', () => {
  const css = readFileSync(new URL('../src/v060.css', import.meta.url), 'utf8');
  assert.match(css, /chat-table-scroll[^}]*overflow-x: auto/);
  assert.match(css, /\.chat-bubble\s*\{\s*min-width: 0/);
  const proxy = readFileSync(new URL('../nginx.conf', import.meta.url), 'utf8');
  assert.match(proxy, /location \/api\/ai\/chat[^}]*proxy_read_timeout 1500s/);
});
