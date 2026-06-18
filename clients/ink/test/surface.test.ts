import test from 'node:test';
import assert from 'node:assert/strict';
import {Surface, charWidth, WIDE, WIDE_CONT} from '../src/render/surface.js';

test('charWidth — ascii 1, CJK 2, emoji 2, control/combining 0', () => {
  assert.equal(charWidth(0x41), 1); // 'A'
  assert.equal(charWidth(0x4e16), 2); // '世'
  assert.equal(charWidth(0x1f680), 2); // '🚀'
  assert.equal(charWidth(0x09), 0); // tab (control)
  assert.equal(charWidth(0), 0);
});

test('setCell / getChar roundtrip; out-of-bounds is a no-op', () => {
  const s = new Surface(10, 3);
  s.setCell(2, 1, 0x41, 5, 0);
  assert.equal(s.getChar(2, 1), 'A');
  assert.equal(s.getStyle(2, 1), 5);
  s.setCell(99, 99, 0x42); // out of bounds → ignored, no throw
  assert.equal(s.getChar(99, 99), '');
});

test('clear marks FULL damage; resetDamage clears it; setCell grows the box', () => {
  const s = new Surface(10, 4);
  // after construction clear() ran → full damage
  assert.deepEqual(s.damage, {minX: 0, minY: 0, maxX: 9, maxY: 3});
  s.resetDamage();
  assert.equal(s.damage, null);
  s.setCell(3, 1, 0x58);
  s.setCell(6, 2, 0x59);
  assert.deepEqual(s.damage, {minX: 3, minY: 1, maxX: 6, maxY: 2});
});

test('setText — ascii advances by 1', () => {
  const s = new Surface(20, 1);
  s.resetDamage();
  const end = s.setText(0, 0, 'hello', 7);
  assert.equal(end, 5);
  assert.equal(s.getChar(0, 0), 'h');
  assert.equal(s.getChar(4, 0), 'o');
  assert.equal(s.getStyle(0, 0), 7);
});

test('setText — wide glyph takes 2 cells with WIDE + WIDE_CONT flags', () => {
  const s = new Surface(10, 1);
  const end = s.setText(0, 0, '世', 0); // U+4E16, width 2
  assert.equal(end, 2);
  assert.equal(s.getChar(0, 0), '世');
  assert.equal(s.getFlag(0, 0), WIDE);
  assert.equal(s.getFlag(1, 0), WIDE_CONT);
  assert.equal(s.code[1], 0); // continuation cell carries no code point
});

test('setText — emoji (non-BMP) is width 2', () => {
  const s = new Surface(10, 1);
  const end = s.setText(0, 0, '🚀', 0);
  assert.equal(end, 2);
  assert.equal(s.getChar(0, 0), '🚀');
  assert.equal(s.getFlag(1, 0), WIDE_CONT);
});

test('setText — clips at the right edge', () => {
  const s = new Surface(3, 1);
  const end = s.setText(0, 0, 'abcdef', 0);
  assert.equal(end, 3); // only 'abc' fit
  assert.equal(s.getChar(2, 0), 'c');
});

test('resize — new dimensions, blank, full damage', () => {
  const s = new Surface(5, 2);
  s.setText(0, 0, 'xx');
  s.resize(8, 3);
  assert.equal(s.width, 8);
  assert.equal(s.height, 3);
  assert.equal(s.getChar(0, 0), ' '); // blanked
  assert.deepEqual(s.damage, {minX: 0, minY: 0, maxX: 7, maxY: 2});
});
