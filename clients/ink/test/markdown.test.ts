import test from 'node:test';
import assert from 'node:assert/strict';
import {renderMarkdown, StreamMarkdown} from '../src/markdown.js';

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
