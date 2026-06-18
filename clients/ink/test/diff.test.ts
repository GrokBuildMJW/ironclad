import test from 'node:test';
import assert from 'node:assert/strict';
import {Surface} from '../src/render/surface.js';
import {diff} from '../src/render/diff.js';

/** prev rendered "hello", reset damage, then write the changes into next. */
function pair(prevText: string): {prev: Surface; next: Surface} {
  const prev = new Surface(12, 1);
  prev.setText(0, 0, prevText);
  prev.resetDamage();
  const next = new Surface(12, 1);
  next.setText(0, 0, prevText);
  next.resetDamage(); // identical so far, no damage
  return {prev, next};
}

test('identical surfaces (no damage) → no patches', () => {
  const {prev, next} = pair('hello');
  assert.deepEqual(diff(prev, next), []);
});

test('one changed cell → one single-cell patch at the right position', () => {
  const {prev, next} = pair('hello');
  next.setCell(1, 0, 0x61); // 'e' → 'a'
  const patches = diff(prev, next);
  assert.equal(patches.length, 1);
  assert.equal(patches[0]?.y, 0);
  assert.equal(patches[0]?.x, 1);
  assert.equal(patches[0]?.cells.length, 1);
  assert.equal(patches[0]?.cells[0]?.cp, 0x61);
});

test('contiguous changes become one run; a gap splits into two', () => {
  const {prev, next} = pair('abcdef');
  next.setCell(1, 0, 0x58); // X
  next.setCell(2, 0, 0x59); // Y   (contiguous with 1)
  next.setCell(4, 0, 0x5a); // Z   (gap at 3 → new run)
  const patches = diff(prev, next);
  assert.equal(patches.length, 2);
  assert.equal(patches[0]?.x, 1);
  assert.equal(patches[0]?.cells.length, 2);
  assert.equal(patches[1]?.x, 4);
  assert.equal(patches[1]?.cells.length, 1);
});

test('changes on multiple rows → one patch per row', () => {
  const prev = new Surface(6, 3);
  const next = new Surface(6, 3);
  next.resetDamage();
  next.setCell(0, 0, 0x41);
  next.setCell(2, 2, 0x42);
  const patches = diff(prev, next);
  const rows = patches.map((p) => p.y).sort();
  assert.deepEqual(rows, [0, 2]);
});

test('style-only change (same glyph) is still a patch', () => {
  const {prev, next} = pair('hello');
  next.setCell(0, 0, 0x68, 7); // same 'h', different style id
  const patches = diff(prev, next);
  assert.equal(patches.length, 1);
  assert.equal(patches[0]?.cells[0]?.style, 7);
});

test('dimension mismatch → full repaint of the damage box', () => {
  const prev = new Surface(4, 1);
  const next = new Surface(8, 1); // wider
  next.resetDamage();
  next.setText(0, 0, 'hi');
  const patches = diff(prev, next);
  assert.ok(patches.length >= 1);
  assert.equal(patches[0]?.x, 0);
});
