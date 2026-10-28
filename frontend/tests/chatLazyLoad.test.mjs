import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('AI chat is loaded on first open and stays mounted after closing', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const entry = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8');

  assert.doesNotMatch(app, /^import\s+\{\s*AIChatDrawer\s*\}\s+from/m);
  assert.match(app, /const LazyAIChatDrawer = lazy\(\(\) =>\s*import\(["']\.\/AIChatDrawer["']\)/);
  assert.match(app, /const \[hasOpenedChat, setHasOpenedChat\] = useState\(false\)/);
  assert.match(app, /setHasOpenedChat\(true\);\s*setChat\(true\);/);
  assert.match(app, /\{hasOpenedChat \? \(\s*<Suspense fallback=\{<ChatLoadingFallback \/>\}>\s*<LazyAIChatDrawer\s+open=\{chat\}/);
  assert.match(app, /close=\{\(\) => setChat\(false\)\}/);
  assert.doesNotMatch(app, /setHasOpenedChat\(false\)/);
  assert.match(app, /role="status" aria-live="polite"[\s\S]*?Loading AI Chat…/);
  assert.match(entry, /<ErrorBoundary>\s*<App \/>\s*<\/ErrorBoundary>/);
});
