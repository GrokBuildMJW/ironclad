import test from 'node:test';
import assert from 'node:assert/strict';
import {Palette, styleSeq, colorParams} from '../src/render/palette.js';

test('colorParams — hex truecolor, named, ansi256, unknown', () => {
  assert.equal(colorParams('#ff8040', false), '38;2;255;128;64');
  assert.equal(colorParams('#ff8040', true), '48;2;255;128;64');
  assert.equal(colorParams('red', false), '38;5;1');
  assert.equal(colorParams('blueBright', false), '38;5;12');
  assert.equal(colorParams('ansi256:208', false), '38;5;208');
  assert.equal(colorParams('chartreuse', false), ''); // unknown name
});

test('styleSeq — attributes + fg, default is empty', () => {
  assert.equal(styleSeq({}), '');
  assert.equal(styleSeq({bold: true, fg: '#ff8040'}), '\x1b[1;38;2;255;128;64m');
  assert.equal(styleSeq({dim: true, italic: true, underline: true}), '\x1b[2;3;4m');
});

test('intern — dedupes identical styles to the same id; id 0 is default', () => {
  const p = new Palette();
  const a = p.intern({bold: true, fg: 'red'});
  const b = p.intern({bold: true, fg: 'red'});
  const c = p.intern({bold: true, fg: 'green'});
  assert.equal(a, b, 'identical styles share an id');
  assert.notEqual(a, c);
  assert.equal(p.seq(0), '', 'id 0 = default, no sequence');
  assert.ok(a > 0);
});

test('transition — same=empty, to-default=reset, else reset+apply (cached)', () => {
  const p = new Palette();
  const red = p.intern({fg: 'red'});
  const green = p.intern({fg: 'green'});
  assert.equal(p.transition(red, red), '');
  assert.equal(p.transition(red, 0), '\x1b[0m');
  assert.equal(p.transition(red, green), '\x1b[0m' + p.seq(green));
  // cached: same result on a second call
  assert.equal(p.transition(red, green), '\x1b[0m' + p.seq(green));
});
