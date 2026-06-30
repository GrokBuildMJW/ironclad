import test from 'node:test';
import assert from 'node:assert/strict';
import {textWidth, wrapText, stripAnsi, collectText, createConfig, applyStyle, attachYoga, calculate, freeYoga} from '../src/render/layout.js';
import {createVNode, createTextNode, appendChild} from '../src/render/vnode.js';
import Yoga from 'yoga-layout';

test('textWidth — ascii 1/col, CJK + emoji 2/col', () => {
  assert.equal(textWidth('hello'), 5);
  assert.equal(textWidth('世界'), 4);
  assert.equal(textWidth('a🚀b'), 4);
});

test('wrapText — word wrap, trailing space trimmed at the break', () => {
  assert.deepEqual(wrapText('hello world', 7), ['hello', 'world']);
  assert.deepEqual(wrapText('hello world', 20), ['hello world']);
});

test('wrapText — over-long word breaks char-by-char', () => {
  assert.deepEqual(wrapText('verylongword', 4), ['very', 'long', 'word']);
});

test('wrapText — CJK breaks per (2-col) glyph; honors newlines; Infinity = no wrap', () => {
  assert.deepEqual(wrapText('世界你好', 4), ['世界', '你好']);
  assert.deepEqual(wrapText('a\nb', 10), ['a', 'b']);
  assert.deepEqual(wrapText('hi there', Infinity), ['hi there']);
});

test('stripAnsi — removes SGR/CSI, keeps visible text + newlines (INK-R-1)', () => {
  assert.equal(stripAnsi('\x1b[31mred\x1b[0m'), 'red');
  assert.equal(stripAnsi('a\x1b[2Kb\nc'), 'ab\nc');     // non-SGR CSI dropped too; newline preserved
  assert.equal(stripAnsi('none'), 'none');               // no-escape input returned as-is
});

test('textWidth — ANSI SGR escapes count as zero width (INK-R-1)', () => {
  assert.equal(textWidth('\x1b[31mred\x1b[0m'), 3);          // only "red" is visible
  assert.equal(textWidth('\x1b[1;38;5;42mhi\x1b[0m'), 2);    // extended SGR params, still 2 cols
  assert.equal(textWidth('世\x1b[0m界'), 4);                  // escape between CJK glyphs: 2+2, escape 0
});

test('wrapText — wraps by visible width; ANSI neither consumes budget nor is split (INK-R-1)', () => {
  // colored "hello world" (visible width 11) wraps exactly like the plain string at width 7
  assert.deepEqual(wrapText('\x1b[31mhello\x1b[0m \x1b[32mworld\x1b[0m', 7), ['hello', 'world']);
  // escapes around a word must not push it over a generous width (no spurious break)
  assert.deepEqual(wrapText('\x1b[1mhello world\x1b[0m', 20), ['hello world']);
});

test('collectText — concatenates raw text + nested ink-text', () => {
  const t = createVNode('ink-text');
  appendChild(t, createTextNode('Hel'));
  const inner = createVNode('ink-text');
  appendChild(inner, createTextNode('lo'));
  appendChild(t, inner);
  assert.equal(collectText(t), 'Hello');
});

test('applyStyle + calculate — row box lays out two text children side by side', () => {
  const cfg = createConfig();
  const root = createVNode('ink-root', {width: 20, height: 1, flexDirection: 'row'});
  const a = createVNode('ink-text');
  appendChild(a, createTextNode('AB')); // width 2
  const b = createVNode('ink-text');
  appendChild(b, createTextNode('CD')); // width 2
  appendChild(root, a);
  appendChild(root, b);

  attachYoga(root, cfg);
  calculate(root, 20);

  const ry = root.yoga as ReturnType<typeof Yoga.Node.create>;
  const ay = a.yoga as ReturnType<typeof Yoga.Node.create>;
  const by = b.yoga as ReturnType<typeof Yoga.Node.create>;
  assert.equal(ry.getComputedWidth(), 20);
  assert.equal(ay.getComputedLeft(), 0);
  assert.equal(ay.getComputedWidth(), 2);
  assert.equal(by.getComputedLeft(), 2);
  assert.equal(by.getComputedWidth(), 2);
  freeYoga(root);
  assert.equal(root.yoga, null);
});

test('border reserves a cell on each active edge (affects inner layout)', () => {
  const cfg = createConfig();
  const root = createVNode('ink-root', {width: 10, height: 3, borderStyle: 'single'});
  const inner = createVNode('ink-box', {flexGrow: 1});
  appendChild(root, inner);
  attachYoga(root, cfg);
  calculate(root, 10);
  const iy = inner.yoga as ReturnType<typeof Yoga.Node.create>;
  // 10 wide, 3 tall, 1-cell border all around → inner at (1,1), 8x1
  assert.equal(iy.getComputedLeft(), 1);
  assert.equal(iy.getComputedTop(), 1);
  assert.equal(iy.getComputedWidth(), 8);
  freeYoga(root);
});

test('measure func — text node height grows with wrapping under a width constraint', () => {
  const cfg = createConfig();
  const root = createVNode('ink-root', {width: 5, flexDirection: 'column'});
  const t = createVNode('ink-text');
  appendChild(t, createTextNode('hello world')); // wraps to 2 lines at width 5
  appendChild(root, t);
  attachYoga(root, cfg);
  calculate(root, 5);
  const ty = t.yoga as ReturnType<typeof Yoga.Node.create>;
  assert.equal(ty.getComputedHeight(), 2);
  freeYoga(root);
});
