import test from 'node:test';
import assert from 'node:assert/strict';
import {renderMarkdown, StreamMarkdown, splitBlocks} from '../src/markdown.js';

const strip = (s: string): string => s.replace(/\x1b\[[0-9;]*m/g, '');

test('preserves the model line breaks (single newlines render as hard breaks for chat)', () => {
  const out = strip(renderMarkdown('Model : x\nStreaming : on\nPlatform : linux', 80));
  const lines = out
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);
  assert.deepEqual(lines, ['Model : x', 'Streaming : on', 'Platform : linux'], 'each line kept separate');
});

test('still wraps a genuinely long line to the width', () => {
  const long = 'word '.repeat(40).trim();
  const out = strip(renderMarkdown(long, 30));
  for (const l of out.split('\n')) assert.ok(l.length <= 30, `line within width: "${l}"`);
});

test('still renders markdown structure (a bullet list stays multi-line)', () => {
  const s = strip(renderMarkdown('- one\n- two\n- three', 80));
  const itemLines = s.split('\n').filter((l) => /one|two|three/.test(l));
  assert.equal(itemLines.length, 3, 'each list item on its own line');
});

test('StreamMarkdown fast-paths plain prose verbatim (no parser)', () => {
  const sm = new StreamMarkdown(80);
  assert.equal(sm.render('hello world'), 'hello world', 'plain block passes through with no ANSI');
});

test('StreamMarkdown still renders markdown blocks', () => {
  const sm = new StreamMarkdown(80);
  const out = sm.render('**bold**');
  assert.notEqual(out, '**bold**', 'markdown block went through the renderer');
  assert.match(strip(out), /bold/);
});

test('StreamMarkdown caches completed blocks; the open tail is not cached', () => {
  const sm = new StreamMarkdown(80);
  sm.render('**a**\n\n**b**\n\n**c**'); // a, b complete (cached); c is the tail
  assert.equal(sm.cachedBlocks, 2);
  sm.render('**a**\n\n**b**\n\n**cc**'); // a, b cache hits; cc is the new tail
  assert.equal(sm.cachedBlocks, 2, 'no new cache entries — tail stays uncached, completed blocks reused');
  sm.reset();
  assert.equal(sm.cachedBlocks, 0);
});

test('StreamMarkdown keeps earlier blocks stable as the tail grows', () => {
  const sm = new StreamMarkdown(80);
  const r1 = sm.render('para one\n\npara t');
  const r2 = sm.render('para one\n\npara two');
  assert.ok(r1.startsWith('para one') && r2.startsWith('para one'), 'first block unchanged across updates');
})

// ── MEM-20: code display keeps its formatting ───────────────────────────────────────────────────

test('MEM-20: a long code line is NOT reflowed/wrapped, indentation preserved', () => {
  const longTail = 'x'.repeat(60);
  const md = '```python\ndef f():\n        return "' + longTail + '"\n```';
  const lines = strip(renderMarkdown(md, 40)).split('\n');
  const ret = lines.find((l) => l.includes('return'));
  const def = lines.find((l) => /def f/.test(l));
  assert.ok(ret && def, 'both code lines present');
  assert.ok(ret!.includes(longTail), 'the long code line stays on one line (no width wrap)');
  const lead = (l: string): number => l.length - l.trimStart().length;
  assert.ok(lead(ret!) > lead(def!), 'relative indentation preserved (return deeper than def)');
});

test('MEM-20: a fenced code block is styled (carries ANSI, visually distinct)', () => {
  const out = renderMarkdown('```js\nconst x = 1;\n```', 80);
  assert.match(out, /\x1b\[[0-9;]*m/, 'code block carries ANSI styling');
});

test('MEM-20: splitBlocks keeps a fenced block with a blank line as ONE block', () => {
  const body = 'intro\n\n```js\nconst a = 1;\n\nconst b = 2;\n```\n\nafter';
  assert.deepEqual(splitBlocks(body), [
    'intro',
    '```js\nconst a = 1;\n\nconst b = 2;\n```',
    'after',
  ]);
});

test('MEM-20: splitBlocks treats an unterminated fence as the open tail', () => {
  const body = 'note\n\n```js\nconst a = 1;\n\nstill typing';
  assert.deepEqual(splitBlocks(body), ['note', '```js\nconst a = 1;\n\nstill typing']);
});

test('MEM-20: prose without fences splits exactly as before (no regression)', () => {
  assert.deepEqual(splitBlocks('a\n\nb\n\nc'), ['a', 'b', 'c']);
});

test('MEM-20: StreamMarkdown renders both code lines across a blank line in the fence', () => {
  const sm = new StreamMarkdown(80);
  const out = strip(sm.render('intro\n\n```js\nconst a = 1;\n\nconst b = 2;\n```'));
  assert.match(out, /const a = 1;/);
  assert.match(out, /const b = 2;/);
  // the fenced block is one cached/non-tail block (intro complete, code block is the tail)
  assert.equal(sm.cachedBlocks, 1, 'intro cached; the whole fence is the single open tail');
});
