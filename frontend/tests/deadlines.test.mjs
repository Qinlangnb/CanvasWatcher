import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const code = ts.transpileModule(readFileSync(new URL('../src/timezone.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext },
}).outputText;
const { formatDeadline, formatLocalDate, formatTodayDeadline } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('date-only deadlines retain their date and never invent a time', () => {
  assert.equal(formatDeadline({ due_at: null, due_date_local: '2026-09-17', deadline_precision: 'DATE_ONLY' }),
    `Due ${formatLocalDate('2026-09-17')} · Time not specified by source`);
});

test('timeline title and accessible label both use the full deadline contract', () => {
  const source = readFileSync(new URL('../src/timeline.tsx', import.meta.url), 'utf8');
  assert.equal((source.match(/formatDeadline\(node\.task\)/g) || []).length, 2);
  assert.doesNotMatch(source, /formatUiucDateTime\(node\.task\.due_at\)/);
});

test('Today never prints its planning cutoff as a source deadline', () => {
  const text = formatTodayDeadline({ deadline: '2026-09-18T04:59:00Z', due_date_local: '2026-09-17', deadline_precision: 'DATE_ONLY' });
  assert.equal(text, `Due ${formatLocalDate('2026-09-17')} · Time not specified by source`);
  const source = readFileSync(new URL('../src/TodayView.tsx', import.meta.url), 'utf8');
  assert.match(source, /formatTodayDeadline\(item\)/);
  assert.doesNotMatch(source, /formatUiucDateTime\(item\.deadline\)/);
});

test('both active and completed Today cards label a course-page fallback', () => {
  const source = readFileSync(new URL('../src/TodayView.tsx', import.meta.url), 'utf8');
  assert.equal((source.match(/item\.source_link_kind === "course_page"/g) || []).length, 2);
  assert.equal((source.match(/Original course page/g) || []).length, 2);
});
